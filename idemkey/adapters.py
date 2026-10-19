"""Framework adapters.

MCP tools (Python SDK) are plain functions under ``@mcp.tool()`` — so the
generic :func:`idemkey.guard` wrapper already covers them; :func:`mcp_tool`
just attaches the tool name/description metadata in one step. The same holds
for LangChain (``langchain_core.tools.tool`` wraps any callable), CrewAI
(``@tool``), and raw functions: wrap the callable with ``guard`` and register
the result with the framework as usual.
"""

from __future__ import annotations

from .guard import guard


def mcp_tool(_fn=None, *, name=None, description=None, **guard_opts):
    """Wrap a function as an idempotent MCP tool.

    Usage::

        from mcp.server.fastmcp import FastMCP
        from idemkey import mcp_tool, new_key

        mcp = FastMCP("orders")

        @mcp.tool()
        @mcp_tool(key_arg="idemkey", verify=order_exists)
        def capture_order(order_id: str) -> dict:
            '''Capture payment for an order.'''
            ...

    Then the agent calls it with a stable key per logical operation::

        capture_order("ord-123", idemkey="order-ord-123")
        # retry after a timeout reuses the key -> deduped, never double-captured

    ``name`` overrides the tool name used for fingerprinting; ``description``
    defaults to the function's docstring. All other kwargs go to
    :func:`idemkey.guard`.
    """

    def wrap(fn):
        tool_name = name or fn.__name__
        guarded = guard(fn, tool=tool_name, **guard_opts)
        guarded.mcp_tool_name = tool_name
        guarded.mcp_description = description if description is not None else fn.__doc__
        return guarded

    if _fn is not None:
        return wrap(_fn)
    return wrap


__all__ = ["mcp_tool"]
