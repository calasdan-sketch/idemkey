"""Round 5: author pin, upgrade from 0.1.x, model-chosen keys, locked store,
golden CLI output, property-based and model-based tests."""

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

import idemkey
from idemkey import DedupStore, InvalidKeyError, StoreBusyError, guard
from idemkey import store as store_mod
from idemkey.cli import main

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
AUTHOR = "Calas Automations Inc."


def cli(*argv, env=None):
    p = subprocess.run([sys.executable, "-m", "idemkey", *argv], capture_output=True,
                       env={**os.environ, "TZ": "UTC", **(env or {})}, check=False)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


# -- author / owner label ----------------------------------------------------

def test_author_is_pinned_everywhere():
    assert idemkey.__author__ == AUTHOR
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'authors = [{{ name = "{AUTHOR}" }}]' in py
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").splitlines()[2] == f"Copyright (c) 2026 {AUTHOR}"
    assert "dan calas" not in py.lower()


def test_installed_metadata_names_the_author():
    from importlib import metadata
    try:
        md = metadata.metadata("idemkey")
    except metadata.PackageNotFoundError:  # pragma: no cover - run from a bare checkout
        pytest.skip("not installed")
    assert AUTHOR in (md.get("Author") or md.get("Author-email") or "")


# -- keys a model might send ---------------------------------------------------

@pytest.mark.parametrize("bad", ["null", "None", "NULL", "undefined", "nil", "N/A", "nan", " order-1", "order-1 ", "order-1\n"])
def test_placeholder_and_padded_keys_are_refused(tmp_path, bad):
    ran = []

    @guard(store=DedupStore(str(tmp_path / "s.db")))
    def f(x):
        ran.append(x)

    with pytest.raises(InvalidKeyError):
        f(1, idemkey=bad)
    assert ran == []


def test_ordinary_keys_still_work(tmp_path):
    @guard(store=DedupStore(str(tmp_path / "s.db")))
    def f(x):
        return x

    for k in ("order-1", "none-of-your-business", "nulled", "a b", "é", "x|y"):
        assert f(1, idemkey=k) == 1


# -- locked store ------------------------------------------------------------------

def test_locked_store_fails_closed_with_plain_error(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "WRITE_RETRIES", 2)
    monkeypatch.setattr(store_mod, "WRITE_RETRY_BASE", 0.001)
    path = str(tmp_path / "s.db")
    st = DedupStore(path)
    st._conn.execute("PRAGMA busy_timeout=50")
    ran = []

    @guard(store=st)
    def f(x):
        ran.append(x)
        return x

    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StoreBusyError) as e:
            f(1, idemkey="k1")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert isinstance(e.value, sqlite3.OperationalError)  # old except-clauses keep working
    assert "locked by another program" in str(e.value)
    assert ran == []                                     # the tool never started
    assert f(1, idemkey="k1") == 1 and ran == [1]        # and recovers


# -- upgrade from 0.1.x -----------------------------------------------------------

def _legacy_store(tmp_path):
    """Rows exactly as 0.1.1 wrote them: raw key, no scope|tool| prefix."""
    st = DedupStore(str(tmp_path / "old.db"))
    now = time.time()
    st._conn.executemany(
        "INSERT INTO idem_keys(key,tool,fingerprint,scope,status,result_json,replayable,error,attempts,"
        "owner,created_at,updated_at,heartbeat) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("order-1", "charge", "fp", "default", "completed", '{"id": "ch_1"}', 1, None, 1, "h:1", now, now, now),
         ("order-2", "charge", "fp", "default", "failed", None, 1, "boom", 1, "h:1", now, now, now),
         ("default|charge||order-3", "charge", "fp", "default", "completed", '1', 1, None, 1, "h:1", now, now, now)])
    st._conn.commit()
    return st


def test_migrate_makes_old_rows_replay_without_namespace_false(tmp_path):
    st = _legacy_store(tmp_path)
    calls = []

    @guard(store=st, tool="charge", fingerprint_policy="replay")
    def charge(c):
        calls.append(c)
        return {"id": "NEW"}

    assert st.migrate_legacy_keys(dry_run=True) == {"migrated": 2, "already_namespaced": 1, "skipped": 0}
    assert st.get("order-1") is not None                       # dry run changed nothing
    assert st.migrate_legacy_keys() == {"migrated": 2, "already_namespaced": 1, "skipped": 0}
    assert st.get("order-1") is None and st.get("default|charge||order-1")["status"] == "completed"
    assert charge("cus", idemkey="order-1") == {"id": "ch_1"} and calls == []   # replayed
    assert st.migrate_legacy_keys() == {"migrated": 0, "already_namespaced": 3, "skipped": 0}  # idempotent


def test_migrate_never_overwrites_and_skips_pipes(tmp_path):
    st = _legacy_store(tmp_path)
    now = time.time()
    st._conn.execute(
        "INSERT INTO idem_keys VALUES ('default|charge||order-1','charge','fp','default','completed','2',1,NULL,1,'h',?,?,?)",
        (now, now, now))
    st._conn.execute(
        "INSERT INTO idem_keys VALUES ('k','a|b','fp','default','completed','2',1,NULL,1,'h',?,?,?)", (now, now, now))
    st._conn.commit()
    out = st.migrate_legacy_keys()
    assert out["skipped"] == 2 and out["migrated"] == 1
    assert st.get("order-1")["result_json"] == '{"id": "ch_1"}'          # old row kept, not merged
    assert st.get("default|charge||order-1")["result_json"] == "2"       # existing row untouched


def test_migrate_cli_and_doctor_hint(tmp_path, capsys):
    st = _legacy_store(tmp_path)
    path = st.path
    st.close()
    rc, out, _ = cli("--store", path, "doctor")
    assert rc == 0 and "0.1.x" in out and "idemkey migrate" in out
    rc, out, _ = cli("--store", path, "migrate", "--dry-run", "--json")
    assert rc == 0 and json.loads(out) == {"migrated": 2, "already_namespaced": 1, "skipped": 0, "dry_run": True}
    rc, out, _ = cli("--store", path, "migrate")
    assert rc == 0 and out.startswith("migrated 2 key(s)")
    rc, out, _ = cli("--store", path, "doctor")
    assert "0.1.x" not in out


def test_doctor_reports_sqlite_damage(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "s.db")
    DedupStore(path).close()
    monkeypatch.setattr(DedupStore, "quick_check", lambda self: ["row 3 missing from index idx_idem_status"])
    with pytest.raises(SystemExit) as e:
        main(["--store", path, "doctor", "--json"])
    assert e.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert not data["ok"] and "damage" in data["problems"][0]


def test_quick_check_clean_store(tmp_path):
    assert DedupStore(str(tmp_path / "s.db")).quick_check() == []


# -- golden CLI output -----------------------------------------------------------------

def _golden_store(tmp_path):
    st = DedupStore(str(tmp_path / "g.db"))
    rows = [
        ("default|charge||order-1", "charge", "completed", '{"id": "ch_1"}', 1, None, 1000.0),
        ("default|charge||order-2", "charge", "failed", None, 1, "RuntimeError: boom", 2000.0),
        ("default|refund||réf-3", "refund", "unknown", None, 1, "cancelled", 3000.0),
        ("default|send||m-4", "send", "pending", None, 1, None, 4000.0),
    ]
    for key, tool, status, res, rep, err, ts in rows:
        st._conn.execute(
            "INSERT INTO idem_keys VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, tool, "f" * 8, "default", status, res, rep, err, 1, "host:1:abc", ts, ts, ts))
    st._conn.commit()
    path = st.path
    st.close()
    return path


GOLDEN_CASES = {
    "log.txt": ["log"],
    "log.json": ["log", "--json"],
    "export.jsonl": ["export", "--no-results"],
    "stats.json": ["stats", "--json"],
    "stats.txt": ["stats"],
    "show.txt": ["show", "order-1"],
    "doctor.json": ["doctor", "--json"],
    "purge-dry.txt": ["purge", "--dry-run"],
    "migrate.json": ["migrate", "--dry-run", "--json"],
}


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_cli_output(tmp_path, name):
    if name in ("log.txt", "show.txt") and sys.platform == "win32":
        pytest.skip("local-time columns: the TZ variable is not honoured on Windows")
    path = _golden_store(tmp_path)
    rc, out, err = cli("--store", path, *GOLDEN_CASES[name], env={"PYTHONIOENCODING": "utf-8"})
    out = out.replace(path, "<STORE>")
    golden = HERE / "golden" / name
    if os.environ.get("IDEMKEY_UPDATE_GOLDEN"):
        golden.write_text(out, encoding="utf-8", newline="\n")
    assert out == golden.read_text(encoding="utf-8"), f"{name} changed (exit {rc}, stderr {err!r})"
