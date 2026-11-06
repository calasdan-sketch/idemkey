"""Regression tests for the synapse-1 panel review (findings 2-9).

Finding 1 (PEP 563 annotations) lives in test_future_annotations.py, since it
needs ``from __future__ import annotations`` at module level.
"""

import asyncio
import inspect
import sqlite3
import time
import warnings

import pytest
from conftest import one

from idemkey import (
    DedupStore,
    FingerprintMismatchError,
    InvalidKeyError,
    MissingKeyError,
    OutcomeUnknownError,
    fingerprint,
    guard,
    namespaced_key,
)
from idemkey.cli import main


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "r.db"))
    yield s
    s.close()


def _age(store, key, field="heartbeat", by=1000):
    with store._lock:
        store._conn.execute(f"UPDATE idem_keys SET {field} = {field} - ? WHERE key = ?", (by, key))
        store._conn.commit()


# -- 2. async cancellation must not lead to a second execution ---------------

def test_cancel_after_effect_then_retry_does_not_reexecute(store):
    effects = []

    @guard(store=store)
    async def charge(amt):
        effects.append(amt)  # request already sent downstream
        await asyncio.sleep(1)  # ...waiting for the response
        return "ok"

    async def go():
        t = asyncio.ensure_future(charge(5, idemkey="c1"))
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert one(store, "c1")["status"] == "unknown"
        with pytest.raises(OutcomeUnknownError):
            await charge(5, idemkey="c1")

    asyncio.run(go())
    assert effects == [5]


def test_unknown_also_blocks_sync_retry_until_forgotten(tmp_path, capsys):
    path = str(tmp_path / "u.db")
    store = DedupStore(path)
    calls = []

    @guard(store=store)
    def op():
        calls.append(1)
        return 1

    sk = op.store_key("k")
    store.claim(sk, "op", fingerprint("op"), "default", "dead", 300)
    store.mark_unknown(sk, "cancelled", owner="dead")
    with pytest.raises(OutcomeUnknownError):
        op(idemkey="k")
    main(["--store", path, "forget", "k"])
    assert op(idemkey="k") == 1
    assert calls == [1]


def test_cancel_during_commit_keeps_the_real_result(store):
    real_complete = store.complete

    def slow_complete(*a, **k):
        time.sleep(0.3)
        return real_complete(*a, **k)

    store.complete = slow_complete
    effects = []

    @guard(store=store)
    async def op():
        effects.append(1)
        return "done"

    async def go():
        t = asyncio.ensure_future(op(idemkey="z"))
        await asyncio.sleep(0.1)  # tool finished; commit in progress
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert one(store, "z")["status"] == "completed"
        return await op(idemkey="z")

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert asyncio.run(go()) == "done"  # replayed
    assert effects == [1]
    assert not [x for x in w if x.category.__name__ == "ClaimLostWarning"]


def test_cancel_before_execution_releases_claim(store):
    real_claim = store.claim

    def slow_claim(*a, **k):
        time.sleep(0.2)
        return real_claim(*a, **k)

    store.claim = slow_claim
    calls = []

    @guard(store=store)
    async def op():
        calls.append(1)
        return 1

    async def go():
        t = asyncio.ensure_future(op(idemkey="b"))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert one(store, "b")["status"] == "failed"  # never ran: safe to retry
        store.claim = real_claim
        return await op(idemkey="b")

    assert asyncio.run(go()) == 1
    assert calls == [1]


# -- 3. stale takeover must respect the fingerprint --------------------------

def test_stale_takeover_with_different_args_is_refused(store):
    calls = []

    @guard(store=store, tool="pay")
    def pay(to, amt):
        calls.append((to, amt))
        return {"to": to, "amt": amt}

    sk = pay.store_key("k")
    store.claim(sk, "pay", fingerprint("pay", ("alice", 10), {}), "default", "dead", 300)
    _age(store, sk)
    with pytest.raises(FingerprintMismatchError):
        pay("mallory", 999, idemkey="k")
    assert calls == []
    assert pay("alice", 10, idemkey="k") == {"to": "alice", "amt": 10}  # rightful takeover


def test_store_takeover_requires_matching_fingerprint(store):
    store.claim("k", "t", "A", "default", "dead", 300)
    _age(store, "k")
    assert store.claim("k", "t", "B", "default", "new", 300)[0] == "inflight"
    assert store.claim("k", "t", "A", "default", "new", 300)[0] == "claimed"


def test_live_inflight_mismatch_fails_fast_not_after_timeout(store):
    @guard(store=store, inflight_timeout=30)
    def f(x):
        return x

    store.claim(f.store_key("k"), "f", fingerprint("f", (1,), {}), "default", "live", 300)
    t0 = time.monotonic()
    with pytest.raises(FingerprintMismatchError):
        f(2, idemkey="k")
    assert time.monotonic() - t0 < 5


# -- 4. namespacing -----------------------------------------------------------

def test_same_key_on_two_tools_does_not_collide(store):
    @guard(store=store)
    def refund(order):
        return {"refund": order}

    @guard(store=store)
    def charge(order):
        return {"charge": order}

    assert charge("o1", idemkey="order-o1") == {"charge": "o1"}
    # Before: FingerprintMismatchError (one tool squatting the other's key).
    assert refund("o1", idemkey="order-o1") == {"refund": "o1"}
    assert charge("o1", idemkey="order-o1") == {"charge": "o1"}  # own replay


def test_key_prefix_isolates_clients(store):
    import contextvars

    client = contextvars.ContextVar("client", default="")
    calls = []

    @guard(store=store, key_prefix=client.get)
    def charge(order):
        calls.append(order)
        return order

    client.set("attacker")
    charge("junk", idemkey="order-123")  # squat attempt
    client.set("victim")
    assert charge("real", idemkey="order-123") == "real"  # unaffected
    assert calls == ["junk", "real"]
    assert store.get(namespaced_key("order-123", "charge", "default", "victim"))


def test_key_prefix_and_scope_validation():
    with pytest.raises(InvalidKeyError):
        guard(lambda: 1, key_prefix="a|b")
    with pytest.raises(InvalidKeyError):
        guard(lambda: 1, scope="x|y")

    @guard(store=DedupStore(":memory:"), key_prefix=lambda: "bad|prefix")
    def f():
        return 1

    with pytest.raises(InvalidKeyError):
        f(idemkey="k")


def test_pipe_in_user_key_cannot_forge_another_namespace(store):
    @guard(store=store, key_prefix="victim")
    def v():
        return "victim"

    @guard(store=store, key_prefix="")
    def v2():
        return "attacker"

    v(idemkey="k")
    # attacker (empty prefix) tries key "victim|k" to land on scope|tool|victim|k
    assert v.store_key("k") != namespaced_key("victim|k", "v", "default", "")


def test_namespace_false_keeps_raw_layout(store):
    @guard(store=store, namespace=False)
    def f():
        return 1

    f(idemkey="raw")
    assert store.get("raw")["status"] == "completed"


# -- 5. terminal injection ------------------------------------------------------

@pytest.mark.parametrize("bad", ["a\x1b]0;PWNED\x07", "line\nfake row", "x\rY", "\u202eevil", "del\x7f"])
def test_control_characters_rejected_in_keys(store, bad):
    @guard(store=store)
    def f():
        return 1

    with pytest.raises(InvalidKeyError):
        f(idemkey=bad)


def test_log_escapes_control_characters_from_old_rows(tmp_path, capsys):
    path = str(tmp_path / "t.db")
    s = DedupStore(path)
    evil = "\x1b]0;PWNED\x07\x1b[2J\x1b[31mfake\nrow"
    s.claim(evil, "to\x1bol", "fp", "default", "o", 300)  # written by 0.1.x
    main(["--store", path, "log"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert out.count("\n") == 2  # header + exactly one row
    assert "\\x1b" in out


# -- 6. purge ---------------------------------------------------------------------

def test_purge_never_deletes_live_pending(store):
    store.claim("live", "t", "fp", "default", "o", 300)
    store.claim("done", "t", "fp", "default", "o", 300)
    store.complete("done", "1", owner="o")
    _age(store, "done", "updated_at")
    _age(store, "live", "updated_at")  # old but heartbeating
    assert store.purge(older_than_seconds=10) == 1
    assert store.get("live") is not None


def test_purge_stale_pending_only_when_asked(store):
    store.claim("dead", "t", "fp", "default", "o", 300)
    _age(store, "dead")
    assert store.purge(older_than_seconds=10) == 0
    assert store.purge(older_than_seconds=10, include_stale_pending=True) == 1


@pytest.mark.parametrize("bad", [0, -1, float("nan")])
def test_purge_rejects_non_positive(store, bad):
    with pytest.raises(ValueError):
        store.purge(older_than_seconds=bad)


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf"])
def test_cli_purge_rejects_non_positive(tmp_path, bad):
    path = str(tmp_path / "p.db")
    DedupStore(path)
    with pytest.raises(SystemExit) as e:
        main(["--store", path, "purge", "--older-than", bad])
    assert e.value.code == 2  # argparse usage error


# -- 7. forget is atomic -----------------------------------------------------

def test_delete_refuses_pending_atomically(store):
    store.claim("k", "t", "fp", "default", "o", 300)
    store.fail("k", "e", owner="o")
    store.reclaim_failed("k", "o2")  # reclaimed after an operator looked at it
    assert store.delete("k") is False  # the guarded DELETE re-checks status
    assert store.get("k")["status"] == "pending"
    assert store.delete("k", allow_pending=True) is True


def test_cli_forget_race_reports_pending(tmp_path, monkeypatch):
    path = str(tmp_path / "f.db")
    s = DedupStore(path)
    s.claim("k", "t", "fp", "default", "o", 300)
    s.fail("k", "e", owner="o")
    real_find = DedupStore.find

    def find_then_reclaim(self, key):
        rows = real_find(self, key)
        self.reclaim_failed("k", "o2")  # race window
        return rows

    monkeypatch.setattr(DedupStore, "find", find_then_reclaim)
    with pytest.raises(SystemExit) as e:
        main(["--store", path, "forget", "k"])
    assert e.value.code == 2
    assert s.get("k")["status"] == "pending"


# -- 8. require_key => required in the schema ---------------------------------

def test_require_key_makes_param_required():
    @guard(require_key=True)
    def charge(amount: int) -> dict:
        return {}

    p = inspect.signature(charge).parameters["idemkey"]
    assert p.default is inspect.Parameter.empty
    assert p.annotation is str
    with pytest.raises(MissingKeyError):
        charge(1)


def test_require_key_required_in_mcp_schema():
    pytest.importorskip("mcp")
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server
    from idemkey import mcp_tool

    server = Server("t")

    @server.tool()
    @mcp_tool(store=DedupStore(":memory:"), require_key=True)
    def charge(amount: int) -> dict:
        return {}

    tools = asyncio.run(server.list_tools())
    schema = getattr(tools[0], "input_schema", None) or tools[0].inputSchema
    assert "idemkey" in schema["required"]


# -- 9. settling writes survive SQLITE_BUSY -------------------------------------

class FlakyStore(DedupStore):
    busy_left = 0

    def _exec(self, sql, params):
        if self.busy_left and sql.lstrip().upper().startswith("UPDATE"):
            self.busy_left -= 1
            raise sqlite3.OperationalError("database is locked")
        return super()._exec(sql, params)


def test_commit_retried_on_sqlite_busy(tmp_path, monkeypatch):
    monkeypatch.setattr("idemkey.store.WRITE_RETRY_BASE", 0.001)
    s = FlakyStore(str(tmp_path / "b.db"))
    calls = []

    @guard(store=s)
    def op():
        calls.append(1)
        return "ok"

    s.busy_left = 3
    assert op(idemkey="k") == "ok"
    assert one(s, "k")["status"] == "completed"
    assert op(idemkey="k") == "ok"
    assert calls == [1]


def test_commit_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("idemkey.store.WRITE_RETRY_BASE", 0.001)
    s = FlakyStore(str(tmp_path / "b2.db"))
    s.busy_left = 1000
    with pytest.raises(sqlite3.OperationalError):
        s.complete("missing", "1")


def test_non_busy_operational_error_not_retried(tmp_path):
    class Broken(DedupStore):
        n = 0

        def _exec(self, sql, params):
            Broken.n += 1
            raise sqlite3.OperationalError("disk I/O error")

    s = Broken(str(tmp_path / "x.db"))
    with pytest.raises(sqlite3.OperationalError):
        s.fail("k", "e")
    assert Broken.n == 1
