"""Finding 1: string annotations (PEP 563) must survive the added signature."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect

import pytest

from idemkey import DedupStore, guard, mcp_tool


@dataclasses.dataclass
class Order:
    id: str
    amount: int


def test_signature_annotations_are_resolved_not_strings():
    @guard
    def cap(order: Order, note: str = "") -> dict:
        return {}

    sig = inspect.signature(cap)
    assert sig.parameters["order"].annotation is Order
    assert sig.parameters["note"].annotation is str
    assert sig.return_annotation is dict


def test_async_signature_annotations_resolved():
    @guard
    async def cap(order: Order) -> dict:
        return {}

    assert inspect.signature(cap).parameters["order"].annotation is Order


def test_unresolvable_annotation_does_not_break_wrapping():
    @guard
    def cap(order: NotDefinedAnywhere) -> dict:  # noqa: F821
        return {"ok": 1}

    # Left as a string rather than crashing at wrap time.
    assert "idemkey" in inspect.signature(cap).parameters
    assert cap(None) == {"ok": 1}


def _server():
    pytest.importorskip("mcp")
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server
    return Server("t")


def test_mcp_registers_and_dedups_pydantic_model_under_future_annotations():
    pydantic = pytest.importorskip("pydantic")
    server = _server()
    store = DedupStore(":memory:")
    calls = []

    class Item(pydantic.BaseModel):
        id: str
        amount: int

    globals()["Item"] = Item  # module-level, as in real code
    try:

        @server.tool(name="cap_sync")
        @mcp_tool(store=store)
        def cap_sync(order: Item) -> dict:
            calls.append(order.id)
            return {"ok": order.id}

        @server.tool(name="cap_async")
        @mcp_tool(store=store)
        async def cap_async(order: Item) -> dict:
            calls.append(order.id)
            return {"ok": order.id}

        async def go():
            for name in ("cap_sync", "cap_async"):
                for _ in range(2):
                    await server.call_tool(
                        name, {"order": {"id": name, "amount": 1}, "idemkey": "k"}
                    )

        asyncio.run(go())
    finally:
        del globals()["Item"]
    assert calls == ["cap_sync", "cap_async"]  # registered, and each ran once
