"""Round 3: exact number fingerprints, huge-input safety, reconcile on in-call
retries, Retry-After, typed result replay, observability hook, ops CLI."""

import asyncio
import datetime
import decimal
import io
import json
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout

import pytest

from idemkey import (
    NOT_FOUND,
    DedupStore,
    FingerprintMismatchError,
    RetryableError,
    cli,
    codec,
    guard,
)
from idemkey.keys import fingerprint2


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "r3.db"))
    yield s
    s.close()


D = decimal.Decimal


# -- fingerprints --------------------------------------------------------

def test_decimals_differing_past_28_digits_are_not_merged():
    a = fingerprint2("t", (D("1.0000000000000000000000000000001"),))
    b = fingerprint2("t", (D("1.0000000000000000000000000000002"),))
    assert a != b
    big_a = fingerprint2("t", (D("123456789012345678901234567890.5"),))
    big_b = fingerprint2("t", (D("123456789012345678901234567890.6"),))
    assert big_a != big_b


def test_equal_numbers_still_merge():
    base = fingerprint2("t", (10,))
    for v in (10.0, D("10.00"), D("1E+1")):
        assert fingerprint2("t", (v,)) == base
    assert fingerprint2("t", (0.0,)) == fingerprint2("t", (-0.0,)) == fingerprint2("t", (D("0.000"),))
    assert fingerprint2("t", (0.5,)) == fingerprint2("t", (D("0.50"),))
    assert fingerprint2("t", (True,)) != fingerprint2("t", (1,))
    assert fingerprint2("t", (D("-0.005"),)) != fingerprint2("t", (D("0.005"),))


def test_huge_exponent_decimal_returns_immediately():
    t0 = time.perf_counter()
    a = fingerprint2("t", (D("1e999999999"),))
    b = fingerprint2("t", (D("1e-999999999"),))
    assert time.perf_counter() - t0 < 1.0
    assert a != b
    assert fingerprint2("t", (D("1e999999999"),)) == a


def test_huge_int_is_fingerprinted_not_rejected():
    n = 10 ** 100000
    assert fingerprint2("t", (n,)) == fingerprint2("t", (n,))
    assert fingerprint2("t", (n,)) != fingerprint2("t", (n + 1,))
    assert fingerprint2("t", (-n,)) != fingerprint2("t", (n,))


def test_long_text_and_bytes_differ_by_one_char_and_nfc_still_merges():
    s = "a" * 100_000
    assert fingerprint2("t", (s,)) != fingerprint2("t", (s[:-1] + "b",))
    assert fingerprint2("t", (s.encode(),)) != fingerprint2("t", (s[:-1].encode() + b"b",))
    nfc = "é" * 5000
    nfd = "é" * 5000
    assert fingerprint2("t", (nfc,)) == fingerprint2("t", (nfd,))
    assert fingerprint2("t", ("x" * 5000,)) != fingerprint2("t", (b"x" * 5000,))


def test_lone_surrogate_in_long_text_does_not_crash():
    fingerprint2("t", ("\ud800" * 5000,))


# -- reconcile consulted before an in-call retry ---------------------------

def test_reconcile_runs_before_in_call_retry_sync(store):
    world = []

    def lookup(order):
        return world[0] if world else NOT_FOUND

    n = []

    @guard(store=store, reconcile=lookup, sleep=lambda s: None)
    def create(order):
        n.append(1)
        world.append({"id": order})
        raise TimeoutError("response lost after the write landed")

    # Attempt 1 writes then times out; the retry must ask the world, find the
    # order and finish instead of writing a second time.
    assert create("o1", idemkey="k") == {"id": "o1"}
    assert len(n) == 1 and len(world) == 1


def test_reconcile_runs_before_in_call_retry_async(store):
    world = []
    n = []

    @guard(store=store, reconcile=lambda order: world[0] if world else NOT_FOUND,
           sleep=lambda s: None)
    async def create(order):
        n.append(1)
        world.append({"id": order})
        raise TimeoutError("lost")

    assert asyncio.run(create("o1", idemkey="k")) == {"id": "o1"}
    assert len(n) == 1


def test_reconcile_not_found_still_retries(store):
    n = []

    @guard(store=store, reconcile=lambda o: NOT_FOUND, sleep=lambda s: None)
    def create(o):
        n.append(1)
        if len(n) < 3:
            raise TimeoutError("never landed")
        return "ok"

    assert create("o", idemkey="k") == "ok"
    assert len(n) == 3


# -- Retry-After ------------------------------------------------------------

def test_retry_after_is_honoured_and_capped(store):
    waits = []
    n = []

    @guard(store=store, sleep=waits.append, backoff_cap=10.0, max_retries=3)
    def api():
        n.append(1)
        if len(n) == 1:
            raise RetryableError("429", retry_after=7)
        if len(n) == 2:
            raise RetryableError("429", retry_after=999)
        if len(n) == 3:
            raise RetryableError("429", retry_after=float("nan"))
        return "ok"

    assert api(idemkey="k") == "ok"
    assert waits[0] == 7 and waits[1] == 10.0
    assert 0 <= waits[2] <= 2.0  # NaN hint ignored -> normal jitter (base 0.5 * 2**2)


def test_retry_after_attribute_on_foreign_exception(store):
    waits = []

    class Rate(Exception):
        retry_after = 3

    n = []

    @guard(store=store, sleep=waits.append, retry_on=(Rate,))
    def api():
        n.append(1)
        if len(n) == 1:
            raise Rate()
        return 1

    api(idemkey="k")
    assert waits == [3.0]


# -- typed results ------------------------------------------------------------

RICH = {
    "pair": (1, 2),
    "raw": b"\x00\xff",
    "tags": {1, 2, 3},
    "amount": D("10.50"),
    "when": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc),
    "day": datetime.date(2026, 1, 2),
    "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    5: "int key",
    "$t": [1],
    "inner": {"$b": "not base64 tag"},
    (1, 2): "tuple key",
    "frozen": frozenset({"a"}),
}


def test_replay_equals_first_result_for_rich_types(store):
    @guard(store=store)
    def tool():
        return RICH

    first = tool(idemkey="k")
    again = tool(idemkey="k")
    assert again == first == RICH
    assert type(again["pair"]) is tuple and type(again["amount"]) is D


def test_json_codec_keeps_old_behaviour(store):
    @guard(store=store, result_codec="json")
    def tool():
        return {"pair": (1, 2), "raw": b"x"}

    tool(idemkey="k")
    assert tool(idemkey="k") == {"pair": [1, 2], "raw": "b'x'"}


def test_codec_reads_rows_written_as_plain_json():
    assert codec.loads('{"a": [1, 2], "n": null}') == {"a": [1, 2], "n": None}


def test_codec_malformed_tags_are_left_alone():
    assert codec.loads('{"$b": "!!"}') == {"$b": "!!"}
    assert codec.loads('{"$dt": "nope"}') == {"$dt": "nope"}
    assert codec.loads('{"$t": 5}') == {"$t": 5}


def test_codec_depth_limit_means_not_replayable_not_crash(store):
    deep = []
    cur = deep
    for _ in range(500):
        nxt = []
        cur.append(nxt)
        cur = nxt

    @guard(store=store)
    def tool():
        return deep

    tool(idemkey="k")
    from idemkey import ResultNotReplayableError
    with pytest.raises(ResultNotReplayableError):
        tool(idemkey="k")


# -- on_event ------------------------------------------------------------

def test_on_event_sequence_and_a_raising_hook_is_harmless(store):
    events = []

    def hook(ev):
        events.append(ev["event"])
        raise RuntimeError("hook bug")

    @guard(store=store, on_event=hook)
    def tool(x):
        return x

    assert tool(1, idemkey="k") == 1
    assert tool(1, idemkey="k") == 1
    with pytest.raises(FingerprintMismatchError):
        tool(2, idemkey="k")
    assert events == ["executing", "executed", "replayed", "fingerprint_mismatch"]


def test_on_event_reports_failure(store):
    events = []

    @guard(store=store, on_event=lambda e: events.append((e["event"], e.get("error"))))
    def tool():
        raise ValueError("x")

    with pytest.raises(ValueError):
        tool(idemkey="k")
    assert ("failed", "ValueError") in events


def test_on_event_must_be_callable():
    with pytest.raises(TypeError):
        guard(lambda: 1, on_event=5)


# -- CLI ------------------------------------------------------------------

def run_cli(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(["--store", store.path, *argv])
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue(), err.getvalue()


def test_cli_log_rejects_non_positive_limit(store):
    code, _, err = run_cli(store, "log", "--limit", "-5")
    assert code == 2 and "at least 1" in err


def test_cli_export_is_jsonl_and_can_omit_results(store):
    @guard(store=store)
    def tool(x):
        return {"secret": "s3"}

    tool("a", idemkey="k1")
    tool("b", idemkey="k\u26032")
    code, out, _ = run_cli(store, "export", "--no-results")
    assert code == 0
    rows = [json.loads(line) for line in out.splitlines()]
    assert len(rows) == 2 and all("result_json" not in r for r in rows)
    assert out.isascii()  # non-ASCII and control characters are escaped
    code, out, _ = run_cli(store, "export")
    assert all("s3" in json.loads(line)["result_json"] for line in out.splitlines())


def test_iter_rows_pages_through_everything(store):
    @guard(store=store)
    def tool(x):
        return x

    for i in range(23):
        tool(i, idemkey=f"k{i}")
    assert len(list(store.iter_rows(batch=5))) == 23
    assert len({r["key"] for r in store.iter_rows(batch=5)}) == 23
    assert len(list(store.iter_rows(status="failed"))) == 0


def test_cli_doctor_flags_unknown_and_stale_pending(store):
    code, out, _ = run_cli(store, "doctor")
    assert code == 0 and "ok" in out
    store.claim("default|t||a", "t", "fp", "default", "o1", 300)
    store.mark_unknown("default|t||a", "boom", owner="o1")
    store.claim("default|t||b", "t", "fp", "default", "o2", 300)
    code, out, _ = run_cli(store, "doctor", "--claim-ttl", "0.0001")
    time.sleep(0.01)
    code, out, _ = run_cli(store, "doctor", "--claim-ttl", "0.0001")
    assert code == 1 and "unknown" in out and "no heartbeat" in out


def test_cli_log_json_and_middle_ellipsis(store):
    @guard(store=store)
    def tool(x):
        return x

    tool(1, idemkey="x" * 100 + "END")
    _, out, _ = run_cli(store, "log")
    assert "END" in out
    _, out, _ = run_cli(store, "log", "--json")
    assert json.loads(out)[0]["key"].endswith("END")


# -- idemkey.testing -------------------------------------------------------

def test_check_tool_flags_unsafe_plain_write_and_passes_reconciled():
    from idemkey import testing

    world = []

    def create(order):
        world.append(order)
        return {"order": order}

    rep = testing.check_tool(create, args=("o1",), effects=lambda: len(world))
    assert rep.duplicate_call_effects == 1 and rep.replay_equal
    assert rep.lost_response_effects == 2 and not rep.ok
    assert "reconcile=" in str(rep)
    with pytest.raises(AssertionError):
        rep.raise_if_unsafe()

    world.clear()
    rep = testing.check_tool(create, args=("o1",), effects=lambda: len(world),
                             reconcile=lambda order: {"order": order} if order in world else NOT_FOUND)
    assert rep.ok, str(rep)
    rep.raise_if_unsafe()


def test_check_tool_counts_executions_without_effects():
    from idemkey import testing

    rep = testing.check_tool(lambda x: x, args=(1,))
    assert not rep.effects_measured and any("effects=" in n for n in rep.notes)


def test_lost_response_async():
    from idemkey import testing

    n = []

    @testing.lost_response
    async def f():
        n.append(1)
        return 1

    async def go():
        with pytest.raises(TimeoutError):
            await f()
        return await f()

    assert asyncio.run(go()) == 1 and len(n) == 2


# -- pass B: a landed write must never be left pending ---------------------

def test_unserialisable_or_unredactable_result_does_not_leave_key_pending(store):
    class Evil:
        def __repr__(self):
            raise RuntimeError("bad repr")

    n = []

    @guard(store=store)
    def tool():
        n.append(1)
        return Evil()

    with pytest.warns(RuntimeWarning):
        tool(idemkey="k1")
    assert one_status(store, "k1") == "completed"
    from idemkey import ResultNotReplayableError
    with pytest.raises(ResultNotReplayableError):
        tool(idemkey="k1")
    assert len(n) == 1

    @guard(store=store, redact=lambda r: 1 / 0)
    def tool2():
        return {"card": "4111"}

    with pytest.warns(RuntimeWarning):
        assert tool2(idemkey="k2") == {"card": "4111"}
    row = next(r for r in store.list() if r["key"].endswith("|k2"))
    assert row["status"] == "completed" and row["result_json"] is None  # never stored unredacted


def one_status(store, key):
    return next(r for r in store.list() if r["key"].endswith("|" + key))["status"]


def test_async_unserialisable_result_does_not_leave_key_pending(store):
    class Evil:
        def __repr__(self):
            raise RuntimeError("bad repr")

    @guard(store=store)
    async def tool():
        return Evil()

    with pytest.warns(RuntimeWarning):
        asyncio.run(tool(idemkey="k"))
    assert one_status(store, "k") == "completed"


def test_cli_export_survives_closed_pipe(store, monkeypatch):
    @guard(store=store)
    def tool(x):
        return x

    tool(1, idemkey="k")

    class Closed:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            pass

    monkeypatch.setattr("sys.stdout", Closed())
    with pytest.raises(SystemExit) as e:
        cli.main(["--store", store.path, "export"])
    assert e.value.code == 0


def test_check_tool_with_inject_key():
    from idemkey import testing

    world, seen = [], set()

    def charge(cents, *, idempotency_key):
        if idempotency_key not in seen:
            seen.add(idempotency_key)
            world.append(cents)
        return cents

    rep = testing.check_tool(charge, args=(5,), effects=lambda: len(world),
                             inject_key="idempotency_key")
    assert rep.ok, str(rep)


def test_cli_doctor_flags_loose_file_mode(store):
    import os
    import stat

    if os.name != "posix":
        pytest.skip("POSIX file modes")
    os.chmod(store.path, 0o644)
    code, out, _ = run_cli(store, "doctor")
    assert code == 1 and "readable by other users" in out
    os.chmod(store.path, stat.S_IRUSR | stat.S_IWUSR)
    code, out, _ = run_cli(store, "doctor")
    assert code == 0
