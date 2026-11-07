"""0.3.0: exactly-once helpers (inject_key, reconcile, atomic), semantic
fingerprints, run-scoped sequence keys, redaction, store behaviour."""

import asyncio
import dataclasses
import decimal
import inspect
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import warnings
from collections import namedtuple

import pytest
from conftest import one

import idemkey
from idemkey import (
    NOT_FOUND,
    DedupStore,
    FingerprintMismatchError,
    MissingKeyError,
    OutcomeUnknownError,
    ReconcileError,
    ResultNotReplayableError,
    guard,
)
from idemkey.keys import fingerprint, fingerprint2


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "e.db"))
    yield s
    s.close()


def stale(store, user_key, tool, seconds=1000):
    """Age a pending row so another caller may take it over."""
    row = one(store, user_key)
    with store._lock:
        store._conn.execute("UPDATE idem_keys SET heartbeat = heartbeat - ? WHERE key = ?",
                            (seconds, row["key"]))
        store._conn.commit()


# -- semantic fingerprints ---------------------------------------------------------

def fp(*a, **k):
    return str(fingerprint2("t", a, k))


def test_numbers_unify_but_stay_exact():
    D = decimal.Decimal
    assert fp(10) == fp(10.0) == fp(D("10.00"))
    assert fp(1.5) == fp(D("1.50"))
    assert fp(0.1) != fp(D("0.1"))          # binary float != decimal: not merged
    assert fp(True) != fp(1) and fp("10") != fp(10) and fp(None) != fp("")
    assert fp(float("nan")) == fp(float("nan"))


def test_unicode_normalisation_only():
    assert fp("café") == fp(unicodedata.normalize("NFD", "café"))
    assert fp("a") != fp("A") and fp("a") != fp(" a")


def test_objects_compared_by_state_not_address():
    class P:
        def __init__(self, x):
            self.x = x

    @dataclasses.dataclass
    class DC:
        a: int

    NT = namedtuple("NT", "a b")
    assert fp(P(1)) == fp(P(1)) != fp(P(2))
    assert fp(DC(1)) == fp(DC(1)) != fp(DC(2))
    assert fp(NT(1, 2)) == fp(NT(1, 2)) != fp(NT(2, 1))
    assert fp({3, 1, 2}) == fp({1, 2, 3})
    assert fp([1, 2]) != fp([2, 1])


def test_cyclic_and_deep_structures_do_not_blow_up():
    a = []
    a.append(a)
    fp(a)
    deep = []
    cur = deep
    for _ in range(5000):
        nxt = []
        cur.append(nxt)
        cur = nxt
    try:
        fp(deep)                             # hashed whole, or refused - never truncated
    except ValueError:
        pass
    mid = [[[[[[[[1]]]]]]]]
    for _ in range(50):
        mid = [mid]
    other = [[[[[[[[2]]]]]]]]
    for _ in range(50):
        other = [other]
    assert fp(mid) != fp(other)              # deep values are never truncated into equality


def test_repr_with_address_is_scrubbed():
    class Opaque:
        __slots__ = ()

    assert fp(Opaque()) == fp(Opaque())


def test_legacy_fingerprint_still_matches_after_upgrade(store):
    """A row written by idemkey <= 0.2 (v1 digest) must replay, not mismatch."""
    calls = []

    @guard(store=store)
    def f(a, b=2):
        calls.append(a)
        return {"n": len(calls)}

    legacy = fingerprint("f", (1, 2), {})
    key = f.store_key("k")
    owner = "o"
    store.claim(key, "f", legacy, "default", owner, 300)
    store.complete(key, '{"n": 99}', True, owner=owner)
    assert f(1, idemkey="k") == {"n": 99} and calls == []


def test_ignore_volatile_args_and_normalize(store):
    calls = []

    @guard(store=store, ignore=("request_id",), normalize={"email": lambda s: s.strip().casefold()})
    def send(to, email="", request_id=None):
        calls.append(1)
        return {"n": len(calls)}

    a = send("x", email=" Bob@X.com ", request_id="r1", idemkey="k")
    b = send("x", email="bob@x.com", request_id="r2", idemkey="k")
    assert a == b and len(calls) == 1
    with pytest.raises(FingerprintMismatchError):
        send("y", email="bob@x.com", request_id="r2", idemkey="k")


def test_ignore_unknown_param_fails_at_wrap_time(store):
    def f(a):
        return a

    with pytest.raises(TypeError):
        guard(f, store=store, ignore=("nope",))
    with pytest.raises(TypeError):
        guard(f, store=store, normalize={"nope": str})


# -- inject_key ---------------------------------------------------------------------

def test_inject_key_is_stable_across_retry_and_hidden(store):
    seen = []

    @guard(store=store, inject_key="idempotency_key", max_retries=0)
    def charge(cents, *, idempotency_key=None):
        seen.append(idempotency_key)
        return {"cents": cents}

    assert "idempotency_key" not in inspect.signature(charge).parameters
    charge(5, idemkey="order-1")
    charge(5, idemkey="order-1")          # replay: tool not called
    assert len(seen) == 1 and seen[0] == charge.downstream_key("order-1")
    assert seen[0].startswith("idk_") and "order-1" not in seen[0]
    with pytest.raises(TypeError):
        charge(5, idemkey="order-2", idempotency_key="spoof")


def test_inject_key_same_after_takeover(store):
    seen = []
    state = {"crash": True}

    @guard(store=store, inject_key="dk", claim_ttl=0.05, max_retries=0)
    def charge(cents, *, dk):
        seen.append(dk)
        if state["crash"]:
            raise KeyboardInterrupt  # simulates the process dying mid-call
        return {"cents": cents}

    with pytest.raises(KeyboardInterrupt):
        charge(5, idemkey="o")
    stale(store, "o", "charge")
    state["crash"] = False
    charge(5, idemkey="o")
    assert seen[0] == seen[1]


def test_inject_param_must_be_keyword_only(store):
    def f(a, dk=None):
        return a

    with pytest.raises(TypeError, match="keyword-only"):
        guard(f, store=store, inject_key="dk")
    with pytest.raises(TypeError):
        guard(lambda a, *, x=None: a, store=store, inject_key="dk")


def test_inject_param_excluded_from_fingerprint(store):
    @guard(store=store, inject_key="dk")
    def f(a, *, dk=None):
        return {"a": a}

    f(1, idemkey="k")
    assert f(1, idemkey="k") == {"a": 1}


# -- reconcile ------------------------------------------------------------------------

def test_reconcile_adopts_landed_write_after_crash(store):
    world = []

    def tool(order):
        world.append(order)
        raise KeyboardInterrupt  # crash after the write, before recording it

    g = guard(tool, store=store, claim_ttl=0.05,
              reconcile=lambda order: {"order": order, "via": "reconcile"} if order in world else NOT_FOUND)
    with pytest.raises(KeyboardInterrupt):
        g("o1", idemkey="k")
    assert one(store, "k")["status"] == "pending"
    stale(store, "k", "tool")
    assert g("o1", idemkey="k") == {"order": "o1", "via": "reconcile"}
    assert world == ["o1"]                       # no second write
    assert one(store, "k")["status"] == "completed"
    assert g("o1", idemkey="k") == {"order": "o1", "via": "reconcile"}   # replays now


def test_reconcile_not_found_executes_again(store):
    world = []
    state = {"crash": True}

    def tool(order):
        if state["crash"]:
            raise KeyboardInterrupt
        world.append(order)
        return {"order": order}

    g = guard(tool, store=store, claim_ttl=0.05, reconcile=lambda order: NOT_FOUND)
    with pytest.raises(KeyboardInterrupt):
        g("o1", idemkey="k")
    stale(store, "k", "tool")
    state["crash"] = False
    assert g("o1", idemkey="k") == {"order": "o1"} and world == ["o1"]


def test_reconcile_not_called_on_first_attempt(store):
    called = []
    g = guard(lambda a: a, store=store, reconcile=lambda a: called.append(1) or NOT_FOUND)
    g(1, idemkey="k")
    assert called == []


def test_reconcile_failure_fails_closed(store):
    def boom(order):
        raise RuntimeError("downstream down")

    ran = []

    def tool(order):
        ran.append(1)
        raise KeyboardInterrupt

    g = guard(tool, store=store, claim_ttl=0.05, reconcile=boom)
    with pytest.raises(KeyboardInterrupt):
        g("o", idemkey="k")
    stale(store, "k", "tool")
    with pytest.raises(ReconcileError):
        g("o", idemkey="k")
    assert one(store, "k")["status"] == "unknown" and ran == [1]


def test_reconcile_resolves_unknown_status(store):
    g = guard(lambda a: {"a": a}, store=store,
              reconcile=lambda a: {"a": a, "found": True})
    g(1, idemkey="k")
    row = one(store, "k")
    store.delete(row["key"])
    # fabricate an unknown row as a cancelled run would leave it
    fpv = fingerprint2("<lambda>", (1,), {})
    store.claim(row["key"], "<lambda>", str(fpv), "default", "o", 300)
    store.mark_unknown(row["key"], "cancelled", owner="o")
    assert g(1, idemkey="k") == {"a": 1, "found": True}
    plain = guard(lambda a: a, store=store)
    store.delete(row["key"])
    store.claim(row["key"], "<lambda>", str(fpv), "default", "o", 300)
    store.mark_unknown(row["key"], "cancelled", owner="o")
    with pytest.raises(OutcomeUnknownError):
        plain(1, idemkey="k")


def test_reconcile_async_after_cancellation(store):
    world = []

    async def main():
        async def tool(x):
            world.append(x)
            await asyncio.sleep(10)

        async def rec(x):
            return {"x": x, "via": "reconcile"} if x in world else NOT_FOUND

        g = guard(tool, store=store, reconcile=rec)
        t = asyncio.ensure_future(g("a", idemkey="k"))
        await asyncio.sleep(0.2)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert one(store, "k")["status"] == "unknown"
        assert await g("a", idemkey="k") == {"x": "a", "via": "reconcile"}
        assert world == ["a"]

    asyncio.run(main())


# -- atomic ----------------------------------------------------------------------------

def count_rows(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM fx").fetchone()[0]
    finally:
        c.close()


def mk_atomic(store, **kw):
    c = sqlite3.connect(store.path)
    c.execute("CREATE TABLE IF NOT EXISTS fx(op TEXT)")
    c.commit()
    c.close()

    @guard(store=store, atomic="tx", **kw)
    def tool(op, *, tx):
        tx.execute("INSERT INTO fx VALUES (?)", (op,))
        return {"op": op}

    return tool


def test_atomic_effect_and_key_commit_together(store):
    tool = mk_atomic(store)
    assert tool("a", idemkey="k") == {"op": "a"}
    assert tool("a", idemkey="k") == {"op": "a"}
    assert count_rows(store.path) == 1
    assert one(store, "k")["status"] == "completed"
    assert "tx" not in inspect.signature(tool).parameters


def test_atomic_exception_rolls_back_effect(store):
    c = sqlite3.connect(store.path)
    c.execute("CREATE TABLE fx(op TEXT)")
    c.commit()
    c.close()

    @guard(store=store, atomic="tx", max_retries=0)
    def tool(op, *, tx):
        tx.execute("INSERT INTO fx VALUES (?)", (op,))
        raise ValueError("boom")

    with pytest.raises(ValueError):
        tool("a", idemkey="k")
    assert count_rows(store.path) == 0
    assert one(store, "k")["status"] == "failed"


def test_atomic_retry_rolls_back_each_attempt(store):
    c = sqlite3.connect(store.path)
    c.execute("CREATE TABLE fx(op TEXT)")
    c.commit()
    c.close()
    n = []

    @guard(store=store, atomic="tx", sleep=lambda s: None)
    def tool(op, *, tx):
        tx.execute("INSERT INTO fx VALUES (?)", (op,))
        n.append(1)
        if len(n) < 3:
            raise TimeoutError
        return {"op": op}

    tool("a", idemkey="k")
    assert len(n) == 3 and count_rows(store.path) == 1


def test_atomic_verify_failure_rolls_back(store):
    c = sqlite3.connect(store.path)
    c.execute("CREATE TABLE fx(op TEXT)")
    c.commit()
    c.close()

    @guard(store=store, atomic="tx", verify=lambda r: False)
    def tool(op, *, tx):
        tx.execute("INSERT INTO fx VALUES (?)", (op,))

    with pytest.raises(idemkey.VerificationError):
        tool("a", idemkey="k")
    assert count_rows(store.path) == 0


def test_commit_atomic_fenced_rolls_back_stale_owner(store):
    c = sqlite3.connect(store.path)
    c.execute("CREATE TABLE fx(op TEXT)")
    c.commit()
    c.close()
    store.claim("K", "t", "fp", "default", "ownerA", 300)
    conn = store.begin_atomic()
    conn.execute("INSERT INTO fx VALUES ('x')")
    assert store.commit_atomic(conn, "K", "someone-else", '{}') is False
    assert count_rows(store.path) == 0
    assert store.get("K")["status"] == "pending"


def test_atomic_claim_lost_returns_winners_result(store):
    tool = mk_atomic(store)
    orig = store.commit_atomic

    def lose(conn, key, owner, payload, replayable=True):
        # another owner already completed this key while we were running
        conn.execute("ROLLBACK")
        conn.close()
        with store._lock:
            store._conn.execute(
                "UPDATE idem_keys SET status='completed', owner='z', result_json='{\"op\": \"won\"}' WHERE key=?",
                (key,))
            store._conn.commit()
        return False

    store.commit_atomic = lose
    try:
        assert tool("a", idemkey="k") == {"op": "won"}
    finally:
        store.commit_atomic = orig
    assert count_rows(store.path) == 0


def test_atomic_requires_key_file_store_sync_tool(tmp_path, store):
    tool = mk_atomic(store)
    with pytest.raises(MissingKeyError):
        tool("a")
    mem = DedupStore(":memory:")
    with pytest.raises(ValueError):
        mem.begin_atomic()

    async def a(x, *, tx):
        return x

    with pytest.raises(TypeError, match="sync tools only"):
        guard(a, atomic="tx", store=store)


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX SIGKILL")
def test_atomic_survives_sigkill(tmp_path):
    """Kill -9 between the INSERT and the COMMIT: neither the effect nor the
    key exists as completed; a retry runs the tool exactly once."""
    db = str(tmp_path / "k.db")
    script = textwrap.dedent(f"""
        import os, signal, sqlite3, sys
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
        from idemkey import guard, DedupStore
        st = DedupStore({db!r})
        c = sqlite3.connect({db!r}); c.execute("CREATE TABLE IF NOT EXISTS fx(op TEXT)"); c.commit(); c.close()
        @guard(store=st, atomic="tx", claim_ttl=0.2)
        def tool(op, *, tx):
            tx.execute("INSERT INTO fx VALUES (?)", (op,))
            if os.environ.get("CRASH"):
                os.kill(os.getpid(), signal.SIGKILL)
            return {{"op": op}}
        print(tool("a", idemkey="k"))
    """)
    env = dict(os.environ)
    env["CRASH"] = "1"
    p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, check=False)
    assert p.returncode == -signal.SIGKILL
    s = DedupStore(db)
    assert count_rows(db) == 0
    time.sleep(0.5)  # claim goes stale
    env.pop("CRASH")
    p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, check=False)
    assert p.returncode == 0, p.stderr.decode()
    assert count_rows(db) == 1
    assert one(s, "k")["status"] == "completed"
    s.close()


# -- run-scoped sequence keys ---------------------------------------------------------

def test_seq_keys_same_run_same_args_distinct_calls(store):
    calls = []

    @guard(store=store, derive="seq")
    def transfer(to, cents):
        calls.append((to, cents))
        return {"n": len(calls)}

    with idemkey.run("r1"):
        a = transfer("bob", 100)
        b = transfer("bob", 100)       # intentional second transfer
    assert a != b and len(calls) == 2


def test_seq_replay_of_run_is_deduplicated(store):
    calls = []

    @guard(store=store, derive="seq")
    def transfer(to, cents):
        calls.append((to, cents))
        return {"n": len(calls)}

    with idemkey.run("r1"):
        first = [transfer("bob", 100), transfer("bob", 100), transfer("amy", 5)]
    with idemkey.run("r1"):                    # the run is replayed after a crash
        again = [transfer("bob", 100), transfer("bob", 100), transfer("amy", 5)]
    assert first == again and len(calls) == 3
    with idemkey.run("r2"):                    # a different run is a new run
        transfer("bob", 100)
    assert len(calls) == 4


def test_seq_failure_does_not_advance(store):
    calls = []

    @guard(store=store, derive="seq", max_retries=0)
    def flaky(x):
        calls.append(x)
        if len(calls) == 1:
            raise ValueError("no")
        return {"ok": len(calls)}

    with idemkey.run("r"):
        with pytest.raises(ValueError):
            flaky(1)
        assert flaky(1) == {"ok": 2}           # same key -> reclaimed failed row
        assert flaky(1) == {"ok": 3}           # success advanced: next call is new
    assert len(calls) == 3


def test_seq_needs_run_and_works_in_async_tasks(store):
    @guard(store=store, derive="seq")
    def f(x):
        return x

    with pytest.raises(MissingKeyError):
        f(1)

    @guard(store=store, derive="seq")
    async def g(x):
        return {"x": x}

    async def main():
        async with idemkey.run("ra"):
            assert await g(1) == {"x": 1}
            assert await g(1) == {"x": 1}

    asyncio.run(main())


def test_semantic_derive_dedups_cosmetic_variants(store):
    calls = []

    @guard(store=store, derive="semantic", scope="s")
    def ensure(n):
        calls.append(n)
        return {"n": len(calls)}

    assert ensure(10) == ensure(10.0) and len(calls) == 1
    assert ensure(11) != ensure(10)


# -- redaction ---------------------------------------------------------------------------

def test_redact_and_store_result_false_and_error_detail(store):
    @guard(store=store, redact=lambda r: {**r, "card": "****"})
    def pay(n):
        return {"n": n, "card": "4111-secret"}

    pay(1, idemkey="a")
    row = one(store, "a")
    assert "4111" not in row["result_json"] and pay(1, idemkey="a")["card"] == "****"

    @guard(store=store, store_result=False)
    def pay2(n):
        return {"n": n}

    assert pay2(1, idemkey="b") == {"n": 1}
    assert one(store, "b")["result_json"] is None
    with pytest.raises(ResultNotReplayableError):
        pay2(1, idemkey="b")

    @guard(store=store, error_detail="type", max_retries=0)
    def bad(n):
        raise ValueError("ssn 123-45-6789")

    with pytest.raises(ValueError):
        bad(1, idemkey="c")
    assert one(store, "c")["error"] == "ValueError"


# -- store ----------------------------------------------------------------------------------

def test_one_shared_heartbeat_thread(store):
    @guard(store=store)
    def f(i):
        return i

    for i in range(50):
        f(i, idemkey=f"k{i}")
    beats = [t for t in threading.enumerate() if t.name == "idemkey-heartbeat"]
    assert len(beats) <= 1


def test_long_call_heartbeats_via_shared_thread(store):
    @guard(store=store, claim_ttl=0.3)
    def slow(i):
        time.sleep(0.9)
        return i

    t = threading.Thread(target=lambda: slow(1, idemkey="k"))
    t.start()
    time.sleep(0.6)
    out, _row = store.claim(one(store, "k")["key"], "slow", "x", "default", "thief", 0.3)
    assert out == "inflight"             # heartbeat kept the claim fresh
    t.join()


def test_inprocess_waiters_wake_immediately(store):
    store.claim("K", "t", "fp", "default", "o", 300)
    got = []

    def waiter():
        t0 = time.perf_counter()
        store.wait_until_settled("K", 5, poll_interval=1.0)
        got.append(time.perf_counter() - t0)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.1)
    store.complete("K", "{}", owner="o")
    th.join()
    assert got and got[0] < 0.4          # woke on notify, not after the 1 s poll


def test_claim_retries_transient_busy(store):
    real = store._claim
    n = {"c": 0}

    def flaky(*a, **k):
        n["c"] += 1
        if n["c"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    store._claim = flaky
    out, _ = store.claim("K", "t", "fp", "default", "o", 300)
    assert out == "claimed" and n["c"] == 3


def test_durability_option(tmp_path):
    with pytest.raises(ValueError):
        DedupStore(str(tmp_path / "x.db"), durability="sloppy")
    s = DedupStore(str(tmp_path / "y.db"), durability="normal")
    assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    s2 = DedupStore(str(tmp_path / "z.db"))
    assert s2._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    with pytest.raises(ValueError):
        s.reclaim("K", "o", "completed")


def test_no_warnings_on_normal_use(store):
    with warnings.catch_warnings():
        warnings.simplefilter("error")

        @guard(store=store)
        def f(a):
            return a

        f(1, idemkey="k")
        f(1, idemkey="k")


def test_atomic_begin_failure_releases_claim(store):
    tool = mk_atomic(store)

    def busy():
        raise sqlite3.OperationalError("database is locked")

    store.begin_atomic = busy
    with pytest.raises(sqlite3.OperationalError):
        tool("a", idemkey="k")
    assert one(store, "k")["status"] == "failed"      # retryable at once, not stuck pending


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_guard_still_works_in_forked_child(store):
    @guard(store=store, claim_ttl=0.3)
    def f(i):
        time.sleep(0.4)       # long enough to need the (re-created) heartbeater
        return {"i": i}

    f(0, idemkey="warm")      # starts the parent's heartbeat thread
    pid = os.fork()
    if pid == 0:
        try:
            ok = f(1, idemkey="child") == {"i": 1}
        finally:
            os._exit(0 if ok else 1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
