"""Key validation, require_key, owner fencing, TTL expiry, schema exposure."""

import inspect
import threading
import time
import warnings

import pytest
from conftest import one

from idemkey import (
    MAX_KEY_LENGTH,
    ClaimLostWarning,
    DedupStore,
    FingerprintMismatchError,
    InvalidKeyError,
    MissingKeyError,
    guard,
    mcp_tool,
)
from idemkey.cli import main


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "s.db"))
    yield s
    s.close()


# -- key validation ---------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_blank_key_rejected_not_silently_keyless(store, bad):
    calls = []

    @guard(store=store)
    def charge():
        calls.append(1)

    with pytest.raises(InvalidKeyError):
        charge(idemkey=bad)
    assert calls == []  # never executed without dedup


@pytest.mark.parametrize("bad", [123, b"bytes", ["k"], 1.5])
def test_non_str_key_rejected(store, bad):
    @guard(store=store)
    def tool():
        return 1

    with pytest.raises(InvalidKeyError):
        tool(idemkey=bad)


def test_overlong_key_rejected_and_max_accepted(store):
    @guard(store=store)
    def tool():
        return 1

    assert tool(idemkey="k" * MAX_KEY_LENGTH) == 1
    with pytest.raises(InvalidKeyError):
        tool(idemkey="k" * (MAX_KEY_LENGTH + 1))


def test_invalid_key_is_also_value_error(store):
    @guard(store=store)
    def tool():
        return 1

    with pytest.raises(ValueError):
        tool(idemkey="")


def test_explicit_none_key_means_keyless(store):
    @guard(store=store)
    def tool():
        return 1

    assert tool(idemkey=None) == 1
    assert store.stats()["total"] == 0


def test_bad_fixed_key_fails_at_wrap_time():
    with pytest.raises(InvalidKeyError):
        guard(lambda: 1, key="")


def test_unicode_and_windows_path_like_keys_roundtrip(store):
    @guard(store=store)
    def tool(x):
        return x

    for k in ["ορδερ-7f3a", r"C:\orders\7f3a", "a'; DROP TABLE idem_keys;--"]:
        assert tool(k, idemkey=k) == k
        assert tool(k, idemkey=k) == k
    assert store.stats()["total"] == 3


# -- require_key ------------------------------------------------------------

def test_require_key_refuses_keyless_call(store):
    calls = []

    @guard(store=store, require_key=True)
    def charge():
        calls.append(1)

    with pytest.raises(MissingKeyError):
        charge()
    assert calls == []
    charge(idemkey="k")
    assert calls == [1]


def test_require_key_satisfied_by_derive(store):
    @guard(store=store, require_key=True, derive=True)
    def ensure(x):
        return x

    assert ensure(1) == 1


# -- signature exposes the key arg (MCP / LangChain schemas) ----------------

def test_signature_includes_key_arg():
    @guard
    def charge(amount: int, *, note: str = "") -> dict:
        return {}

    params = inspect.signature(charge).parameters
    assert list(params) == ["amount", "note", "idemkey"]
    assert params["idemkey"].default is None
    assert params["idemkey"].kind is inspect.Parameter.KEYWORD_ONLY


def test_signature_key_arg_inserted_before_var_kwargs():
    @guard(key_arg="request_id")
    def tool(a, **extra):
        return a

    assert list(inspect.signature(tool).parameters) == ["a", "request_id", "extra"]


def test_signature_unchanged_if_fn_already_declares_key_arg(store):
    @guard(store=store)
    def tool(a, idemkey=None):
        return a

    assert list(inspect.signature(tool).parameters) == ["a", "idemkey"]


def test_mcp_server_schema_exposes_idemkey_and_dedups():
    mcp = pytest.importorskip("mcp")  # optional: only if the SDK is installed
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server
    import asyncio

    store = DedupStore(":memory:")
    server = Server("t")
    calls = []

    @server.tool()
    @mcp_tool(store=store)
    def charge(amount: int) -> dict:
        calls.append(amount)
        return {"c": amount}

    async def go():
        tools = await server.list_tools()
        schema = getattr(tools[0], "input_schema", None) or tools[0].inputSchema
        assert "idemkey" in schema["properties"]
        for _ in range(2):
            await server.call_tool("charge", {"amount": 5, "idemkey": "k1"})

    asyncio.run(go())
    assert calls == [5]  # before 0.2.0 the key was dropped and this was [5, 5]
    del mcp


# -- owner fencing ----------------------------------------------------------

def test_stale_owner_cannot_overwrite_successor(store):
    """A suspended owner whose claim was taken over must not clobber the result."""
    release = threading.Event()
    calls = []

    @guard(store=store, claim_ttl=0.05)
    def slow(x):
        calls.append(x)
        if len(calls) == 1:
            release.wait(5)  # first owner "freezes"
            return "stale"
        return "fresh"

    # Simulate the first owner's heartbeat dying: the stalled owner can't beat
    # because we stub heartbeat out for the duration.
    real_beat = store.heartbeat
    store.heartbeat = lambda *a, **k: None
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", slow(1, idemkey="k")))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        t.start()
        time.sleep(0.2)  # first owner's claim is now stale
        store.heartbeat = real_beat
        assert slow(1, idemkey="k") == "fresh"  # successor takes over
        release.set()
        t.join(5)
    assert out["a"] == "stale"  # caller still gets its own result...
    row = one(store, "k")
    assert row["status"] == "completed"
    assert '"fresh"' in row["result_json"]  # ...but the store keeps the successor's
    assert any(issubclass(w.category, ClaimLostWarning) for w in caught)


def test_store_complete_fenced_by_owner(store):
    store.claim("k", "t", "fp", "default", "o1", 300)
    assert store.complete("k", '"x"', owner="o2") is False
    assert store.get("k")["status"] == "pending"
    assert store.fail("k", "err", owner="o2") is False
    assert store.complete("k", '"x"', owner="o1") is True
    assert store.complete("k", '"y"', owner="o1") is False  # already settled


def test_owner_unique_per_execution_in_same_thread(store):
    owners = []
    real_claim = store.claim

    def spy(key, tool, fp, scope, owner, *a):
        owners.append(owner)
        return real_claim(key, tool, fp, scope, owner, *a)

    store.claim = spy

    @guard(store=store)
    def tool(x):
        return x

    tool(1, idemkey="a")
    tool(2, idemkey="b")
    assert len(set(owners)) == 2


# -- TTL expiry -------------------------------------------------------------

def test_ttl_expired_key_reexecutes_and_accepts_new_args(store):
    calls = []

    @guard(store=store, ttl=0.05)
    def tool(x):
        calls.append(x)
        return x

    assert tool(1, idemkey="k") == 1
    assert tool(1, idemkey="k") == 1  # within ttl: replay
    with pytest.raises(FingerprintMismatchError):
        tool(2, idemkey="k")
    time.sleep(0.1)
    assert tool(2, idemkey="k") == 2  # expired: brand-new operation
    assert calls == [1, 2]
    assert one(store, "k")["attempts"] == 1


def test_ttl_none_never_expires(store):
    calls = []

    @guard(store=store)
    def tool():
        calls.append(1)
        return 1

    tool(idemkey="k")
    with store._lock:
        store._conn.execute("UPDATE idem_keys SET updated_at = 0")
        store._conn.commit()
    tool(idemkey="k")
    assert calls == [1]


def test_ttl_expiry_race_has_one_winner(store):
    store.claim("k", "t", "fp", "default", "o0", 300)
    store.complete("k", '"old"', owner="o0")
    with store._lock:
        store._conn.execute("UPDATE idem_keys SET updated_at = 0")
        store._conn.commit()
    results = []
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        results.append(store.claim("k", "t", "fp", "default", f"o{i+1}", 300, ttl=10)[0])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count("claimed") == 1


@pytest.mark.parametrize("bad", [0, -1])
def test_ttl_must_be_positive(bad):
    with pytest.raises(ValueError):
        guard(lambda: 1, ttl=bad)


# -- CLI forget ---------------------------------------------------------------

def test_cli_forget(tmp_path, capsys):
    path = str(tmp_path / "f.db")
    s = DedupStore(path)
    calls = []

    @guard(store=s)
    def tool():
        calls.append(1)
        return 1

    tool(idemkey="k")
    main(["--store", path, "forget", "k"])
    assert "forgot 'k'" in capsys.readouterr().out
    tool(idemkey="k")
    assert calls == [1, 1]
    with pytest.raises(SystemExit) as e:
        main(["--store", path, "forget", "missing"])
    assert e.value.code == 1


def test_cli_forget_refuses_pending_without_force(tmp_path):
    path = str(tmp_path / "p.db")
    s = DedupStore(path)
    s.claim("k", "t", "fp", "default", "o", 300)
    with pytest.raises(SystemExit) as e:
        main(["--store", path, "forget", "k"])
    assert e.value.code == 2
    main(["--store", path, "forget", "k", "--force"])
    assert s.get("k") is None


# -- huge input --------------------------------------------------------------

def test_deeply_nested_result_is_not_replayable_not_a_crash(store):
    @guard(store=store)
    def tool():
        x = []
        for _ in range(100_000):
            x = [x]
        return x

    tool(idemkey="deep")  # must not raise RecursionError out of the wrapper
    assert one(store, "deep")["replayable"] == 0
