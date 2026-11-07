"""Round 4: what a stranger who ran `pip install idemkey` actually meets."""

import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

import idemkey
from idemkey import DedupStore, MissingKeyError, guard
from idemkey.cli import main


def cli(*argv, env=None):
    p = subprocess.run([sys.executable, "-m", "idemkey", *argv], capture_output=True,
                       env={**os.environ, **(env or {})}, check=False)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"idemkey {idemkey.__version__}"


def test_demo_self_check(capsys):
    main(["demo"])
    out = capsys.readouterr().out
    assert "1 charge(s) made" in out and "idemkey works" in out


def test_version_is_consistent_everywhere():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = pathlib.Path(here, "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{idemkey.__version__}"' in py
    log = pathlib.Path(here, "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {idemkey.__version__}" in log
    assert os.path.exists(os.path.join(here, "LICENSE"))


@pytest.mark.parametrize("content", [b"garbage garbage garbage garbage"])
def test_corrupt_store_is_a_plain_message_not_a_traceback(tmp_path, content):
    bad = tmp_path / "bad.db"
    bad.write_bytes(content)
    code, out, err = cli("--store", str(bad), "stats")
    assert code == 1 and "Traceback" not in err and "corrupt" in err and out == ""


def test_store_path_that_is_a_folder(tmp_path):
    code, _out, err = cli("--store", str(tmp_path), "stats")
    assert code == 1 and "Traceback" not in err and err.startswith("idemkey:")


def test_store_in_unwritable_place_raises_the_documented_error():
    from idemkey import StoreCorruptedError
    blocker = __file__                     # a FILE used as a folder: cannot be created
    with pytest.raises(StoreCorruptedError, match="cannot create the folder"):
        DedupStore(os.path.join(blocker, "sub", "x.db"))


def test_non_ascii_key_does_not_crash_an_ascii_console(tmp_path):
    path = str(tmp_path / "u.db")
    s = DedupStore(path)
    guard(lambda x: x, store=s, tool="t")("a", idemkey="k-é日本")
    s.close()
    env = {"PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
    for argv in (("log",), ("stats",), ("show", "k-é日本")):
        code, out, err = cli("--store", path, *argv, env=env)
        assert code == 0 and "Traceback" not in err, (argv, err)
    code, out, _ = cli("--store", path, "log", env=env)
    assert "\\u65e5" in out or "\\xe9" in out    # escaped, not dropped


def test_long_key_is_bounded_in_error_messages(tmp_path):
    path = str(tmp_path / "l.db")
    DedupStore(path).close()
    code, _out, err = cli("--store", path, "show", "a" * 100000)
    assert code == 1 and len(err) < 400


def test_stats_and_doctor_json(tmp_path):
    path = str(tmp_path / "j.db")
    s = DedupStore(path)
    guard(lambda x: x, store=s, tool="t")(1, idemkey="k")
    s.close()
    code, out, _ = cli("--store", path, "stats", "--json")
    d = json.loads(out)
    assert code == 0 and d["total"] == 1 and d["by_status"] == {"completed": 1}
    code, out, _ = cli("--store", path, "doctor", "--json")
    d = json.loads(out)
    assert code == 0 and d["ok"] is True and d["keys"] == 1 and d["problems"] == []


def test_doctor_json_exit_code_matches(tmp_path):
    path = str(tmp_path / "d.db")
    s = DedupStore(path)
    s.claim("k", "t", "fp", "default", "o", 300)
    s._conn.execute("UPDATE idem_keys SET status='unknown'")
    s._conn.commit()
    s.close()
    code, out, _ = cli("--store", path, "doctor", "--json")
    d = json.loads(out)
    assert code == 1 and d["ok"] is False and "unknown" in d["problems"][0]


def test_purge_dry_run_deletes_nothing(tmp_path):
    path = str(tmp_path / "p.db")
    s = DedupStore(path)
    guard(lambda x: x, store=s, tool="t")(1, idemkey="k")
    s._conn.execute("UPDATE idem_keys SET updated_at = updated_at - 7200")
    s._conn.commit()
    s.close()
    code, out, _ = cli("--store", path, "purge", "--older-than", "1", "--dry-run")
    assert code == 0 and "1 entry" in out and "nothing deleted" in out
    assert json.loads(cli("--store", path, "stats", "--json")[1])["total"] == 1
    code, out, _ = cli("--store", path, "purge", "--older-than", "1")
    assert "purged 1 entry" in out
    assert json.loads(cli("--store", path, "stats", "--json")[1])["total"] == 0


def test_export_is_lf_only_json_lines(tmp_path):
    path = str(tmp_path / "e.db")
    s = DedupStore(path)
    guard(lambda x: x, store=s, tool="t")(1, idemkey="k")
    s.close()
    p = subprocess.run([sys.executable, "-m", "idemkey", "--store", path, "export"],
                       capture_output=True, check=False)
    assert b"\r" not in p.stdout and p.stdout.endswith(b"\n")
    assert json.loads(p.stdout.decode())["status"] == "completed"


# ---- keyless calls must really run once, whatever the arguments are --------

class BadRepr:
    def __repr__(self):
        raise RuntimeError("boom")


def test_keyless_call_with_unreprable_argument_still_runs_once(tmp_path):
    s = DedupStore(str(tmp_path / "k.db"))
    calls = []
    f = guard(lambda x: calls.append(1) or "ran", store=s, tool="t")
    assert f(BadRepr()) == "ran" and calls == [1]
    s.close()


def test_require_key_refuses_before_fingerprinting(tmp_path):
    s = DedupStore(str(tmp_path / "k2.db"))
    f = guard(lambda x: 1, store=s, tool="t", require_key=True)
    with pytest.raises(MissingKeyError):
        f(BadRepr())
    s.close()


def test_keyed_call_with_unreprable_argument_is_a_plain_error_and_does_not_run(tmp_path):
    s = DedupStore(str(tmp_path / "k3.db"))
    calls = []
    f = guard(lambda x: calls.append(1), store=s, tool="t")
    with pytest.raises(ValueError, match="cannot fingerprint"):
        f(BadRepr(), idemkey="k")
    assert calls == []
    s.close()


def test_async_keyless_with_unreprable_argument_runs():
    import asyncio

    async def go():
        async def tool(x):
            return "ran"
        return await guard(tool, tool="t")(BadRepr())

    assert asyncio.run(go()) == "ran"


def test_streams_without_reconfigure_are_tolerated(monkeypatch, tmp_path):
    path = str(tmp_path / "s.db")
    DedupStore(path).close()
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    main(["--store", path, "stats"])


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_child_forked_while_another_thread_holds_the_store_lock_does_not_hang(tmp_path):
    """Before round 4 the child inherited a lock held by the (parent-only)
    heartbeat thread and waited on it forever (about half of the runs of the
    old fork test)."""
    import signal
    import threading

    s = DedupStore(str(tmp_path / "fk.db"))
    holding, release = threading.Event(), threading.Event()

    def hold():
        with s._lock:
            holding.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(5)
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            signal.alarm(8)                      # a hang becomes a failure, not a stuck suite
            s.claim("k", "t", "fp", "default", "child", 60)
            code = 0
        finally:
            os._exit(code)
    release.set()
    t.join()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    s.close()
