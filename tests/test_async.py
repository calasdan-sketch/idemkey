"""async def tools: same semantics as the sync wrapper (no pytest-asyncio needed)."""

import asyncio
import inspect

import pytest

from idemkey import (
    DedupStore,
    FingerprintMismatchError,
    InflightTimeoutError,
    VerificationError,
    guard,
    mcp_tool,
)


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "a.db"))
    yield s
    s.close()


def run(coro):
    return asyncio.run(coro)


def test_async_wrapper_is_a_coroutine_function(store):
    @guard(store=store)
    async def tool(x):
        return x

    # Frameworks (FastMCP/MCPServer, LangChain) check this to decide to await.
    assert inspect.iscoroutinefunction(tool)
    assert tool.__name__ == "tool"


def test_async_duplicate_replays_real_result_not_coroutine_repr(store):
    calls = []

    @guard(store=store)
    async def charge(amount):
        calls.append(amount)
        await asyncio.sleep(0)
        return {"charged": amount}

    async def main():
        a = await charge(5, idemkey="k1")
        b = await charge(5, idemkey="k1")
        return a, b

    a, b = run(main())
    assert a == b == {"charged": 5}
    assert calls == [5]
    assert '"charged": 5' in store.get("k1")["result_json"]


def test_async_retry_with_async_sleep(store):
    calls, slept = [], []

    async def fake_sleep(d):
        slept.append(d)

    @guard(store=store, sleep=fake_sleep, max_retries=3)
    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("slow")
        return "ok"

    assert run(flaky(idemkey="k")) == "ok"
    assert len(calls) == 3 and len(slept) == 2


def test_async_non_retryable_marks_failed_then_reexecutes(store):
    calls = []

    @guard(store=store)
    async def tool():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        return "second"

    with pytest.raises(ValueError):
        run(tool(idemkey="k"))
    assert store.get("k")["status"] == "failed"
    assert run(tool(idemkey="k")) == "second"
    assert len(calls) == 2


def test_async_fingerprint_mismatch(store):
    @guard(store=store)
    async def tool(x):
        return x

    run(tool(1, idemkey="k"))
    with pytest.raises(FingerprintMismatchError):
        run(tool(2, idemkey="k"))


def test_async_verify_may_be_async(store):
    async def verify(result):
        return result == "bad-not"

    @guard(store=store, verify=verify)
    async def tool():
        return "bad"

    with pytest.raises(VerificationError):
        run(tool(idemkey="k"))
    assert store.get("k")["status"] == "failed"


def test_async_concurrent_tasks_single_execution(store):
    calls = []

    @guard(store=store, inflight_timeout=5)
    async def slow():
        calls.append(1)
        await asyncio.sleep(0.2)
        return {"n": 1}

    async def main():
        return await asyncio.gather(*(slow(idemkey="same") for _ in range(5)))

    results = run(main())
    assert results == [{"n": 1}] * 5
    assert len(calls) == 1


def test_async_waiting_does_not_block_event_loop(store):
    # A wedged pending owner: the waiter must poll with asyncio.sleep so other
    # tasks keep running while it waits.
    store.claim("k", "tool", "fp", "default", "other:1:x", 300)
    ticks = []

    @guard(store=store, inflight_timeout=0.3)
    async def tool():
        return 1

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.02)

    async def main():
        t = asyncio.ensure_future(ticker())
        with pytest.raises(InflightTimeoutError):
            await tool(idemkey="k")
        await t

    run(main())
    assert len(ticks) == 5


def test_async_cancellation_marks_failed_so_retry_reexecutes(store):
    calls = []

    @guard(store=store)
    async def tool():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(10)
        return "done"

    async def main():
        t = asyncio.ensure_future(tool(idemkey="k"))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert store.get("k")["status"] == "failed"
        return await tool(idemkey="k")

    assert run(main()) == "done"
    assert len(calls) == 2


def test_async_no_key_runs_once_without_store(store):
    calls = []

    @guard(store=store)
    async def tool():
        calls.append(1)
        raise TimeoutError

    with pytest.raises(TimeoutError):
        run(tool())
    assert len(calls) == 1 and store.stats()["total"] == 0


def test_async_mcp_tool(store):
    @mcp_tool(store=store, name="cap")
    async def capture(order_id: str) -> dict:
        """Capture."""
        return {"id": order_id}

    assert inspect.iscoroutinefunction(capture)
    assert capture.mcp_tool_name == "cap"
    assert "idemkey" in inspect.signature(capture).parameters
    assert run(capture("o1", idemkey="k")) == {"id": "o1"}
    assert store.get("k")["tool"] == "cap"


def test_async_callable_instance_detected(store):
    class Tool:
        def __init__(self):
            self.calls = 0

        async def __call__(self, x):
            self.calls += 1
            return x

    t = Tool()
    g = guard(t, store=store, tool="inst")
    assert inspect.iscoroutinefunction(g)
    assert run(g(3, idemkey="k")) == 3
    assert run(g(3, idemkey="k")) == 3
    assert t.calls == 1
