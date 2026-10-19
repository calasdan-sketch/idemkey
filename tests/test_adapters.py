import pytest

from idemkey import DedupStore, FingerprintMismatchError, guard, mcp_tool


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def nosleep(_):
    pass


def test_mcp_tool_metadata(store):
    @mcp_tool(store=store, sleep=nosleep, name="capture_order")
    def raw_capture(order_id: str):
        """Capture payment for an order."""
        return {"order": order_id}

    assert raw_capture.mcp_tool_name == "capture_order"
    assert raw_capture.mcp_description == "Capture payment for an order."
    assert raw_capture.__name__ == "raw_capture"


def test_mcp_tool_dedups(store):
    calls = []

    @mcp_tool(store=store, sleep=nosleep)
    def charge(amount: int):
        """Charge a card."""
        calls.append(amount)
        return {"charged": amount}

    assert charge(50, idemkey="pay-1") == {"charged": 50}
    assert charge(50, idemkey="pay-1") == {"charged": 50}
    assert calls == [50]
    with pytest.raises(FingerprintMismatchError):
        charge(999, idemkey="pay-1")


def test_mcp_tool_bare_form(store):
    def ping():
        """Ping."""
        return "pong"

    wrapped = mcp_tool(ping, store=store, sleep=nosleep)
    assert wrapped(idemkey="p1") == "pong"
    assert wrapped.mcp_tool_name == "ping"


def test_mcp_tool_name_used_for_fingerprint(store):
    @mcp_tool(store=store, sleep=nosleep, name="shared")
    def a(x):
        return {"by": "a", "x": x}

    @mcp_tool(store=store, sleep=nosleep, name="shared")
    def b(x):
        raise AssertionError("should have been deduped")

    assert a(1, idemkey="k") == {"by": "a", "x": 1}
    # same tool name + same args -> duplicate replay, b never runs
    assert b(1, idemkey="k") == {"by": "a", "x": 1}


def test_guard_result_is_plain_callable_for_other_frameworks(store):
    # LangChain/CrewAI wrap any callable: guard's output must quack like one.
    @guard(store=store, sleep=nosleep)
    def add(a, b):
        """Add two numbers."""
        return a + b

    assert callable(add)
    assert add.__name__ == "add"
    assert add(2, 3, idemkey="sum") == 5
