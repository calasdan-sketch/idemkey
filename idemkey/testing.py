"""Test helpers: prove a tool survives the failures idempotency exists for.

    from idemkey.testing import lost_response, check_tool

    # 1. A fault injector: the write lands, then the response is "lost".
    flaky = lost_response(create_order, times=1)

    # 2. A one-call safety report for a tool (uses a throw-away in-memory store;
    #    never touches your real store or network beyond what the tool itself does):
    report = check_tool(create_order, args=("ord-1",), effects=lambda: len(db_rows()))
    print(report)
    report.raise_if_unsafe()

Both are for tests: they call your tool for real, so point them at a
sandbox / fake downstream, never production.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect

from .guard import guard
from .store import DedupStore

__all__ = ["Report", "check_tool", "lost_response"]


def lost_response(fn, times=1, exc=TimeoutError):
    """Wrap ``fn`` so its first ``times`` calls *run to completion* and then
    raise ``exc`` - the write landed but the caller never saw the response."""
    state = {"n": 0}

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a, **k):
            out = await fn(*a, **k)
            state["n"] += 1
            if state["n"] <= times:
                raise exc("injected: response lost after the write landed")
            return out
        return awrapper

    @functools.wraps(fn)
    def wrapper(*a, **k):
        out = fn(*a, **k)
        state["n"] += 1
        if state["n"] <= times:
            raise exc("injected: response lost after the write landed")
        return out
    return wrapper


@dataclasses.dataclass
class Report:
    duplicate_call_effects: int   # effects after calling twice with the same key
    lost_response_effects: int    # effects after "write landed, response lost" + retry
    replay_equal: bool            # the second call returned what the first returned
    effects_measured: bool        # False: counted tool executions, not real effects
    notes: list

    @property
    def ok(self):
        return self.duplicate_call_effects == 1 and self.lost_response_effects == 1 and self.replay_equal

    def raise_if_unsafe(self):
        if not self.ok:
            raise AssertionError(str(self))

    def __str__(self):
        unit = "effects" if self.effects_measured else "tool executions (pass effects= to count real effects)"
        lines = [
            f"same key called twice        -> {self.duplicate_call_effects} {unit} (want 1)",
            f"write landed, response lost  -> {self.lost_response_effects} {unit} (want 1)",
            f"replay equals first result   -> {self.replay_equal}",
            "verdict: " + ("SAFE for these scenarios" if self.ok else "NOT SAFE"),
        ]
        lines += [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


def check_tool(fn, args=(), kwargs=None, effects=None, **guard_opts):
    """Run ``fn`` under ``guard(**guard_opts)`` through two failure scenarios.

    ``effects`` is a zero-arg callable returning the *total* number of real side
    effects so far (e.g. rows in a fake database). Without it the tool's
    executions are counted, which over-reports when a downstream deduplicates
    (``inject_key=``), and says so in ``notes``. Use a fresh sandbox per call.
    """
    if inspect.iscoroutinefunction(fn):
        raise TypeError("check_tool supports sync tools; test async tools with lost_response() directly")
    kwargs = dict(kwargs or {})
    guard_opts.pop("store", None)
    guard_opts.setdefault("sleep", lambda s: None)
    notes = []
    execs = {"n": 0}

    def counted_factory():
        @functools.wraps(fn)
        def counted(*a, **k):
            execs["n"] += 1
            return fn(*a, **k)
        return counted

    def measure(before_effects, before_execs):
        if effects is not None:
            return effects() - before_effects
        return execs["n"] - before_execs

    def base():
        return (effects() if effects is not None else 0), execs["n"]

    # Scenario 1: the agent calls twice with the same key.
    store = DedupStore(":memory:")
    try:
        g = guard(counted_factory(), store=store, **guard_opts)
        e0, x0 = base()
        first = g(*args, idemkey="check-dup", **kwargs)
        second = g(*args, idemkey="check-dup", **kwargs)
        dup = measure(e0, x0)
        equal = first == second
    finally:
        store.close()

    # Scenario 2: the write lands but the response is lost (TimeoutError), retried.
    store = DedupStore(":memory:")
    try:
        g = guard(lost_response(counted_factory(), times=1), store=store, **guard_opts)
        e0, x0 = base()
        try:
            g(*args, idemkey="check-lost", **kwargs)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the check
            notes.append(f"call raised {type(exc).__name__} after retries")
        lost = measure(e0, x0)
    finally:
        store.close()

    if effects is None:
        notes.append("effects= not given: counted executions of the tool, not downstream effects")
    if lost > 1 and not any(guard_opts.get(k) for k in ("reconcile", "inject_key", "atomic")):
        notes.append("a retry after a lost response re-ran the write: add inject_key= (downstream "
                     "dedups), reconcile= (look it up) or atomic= (same SQLite file)")
    return Report(dup, lost, bool(equal), effects is not None, notes)
