# idemkey

**Stop AI agents from doing the same write twice.** Drop-in idempotency keys
for agent tool calls (sync and `async`): dedup, safe retry, and
partial-success verification. Zero dependencies, crash-safe SQLite store.

**The problem:** every agent failure mode in 2026 was an idempotency problem —
the order-capture agent retried with a fresh UUID each attempt (duplicate
orders), the webhook retried (inventory decremented twice), the triage agent
filed six duplicate tickets by morning. An agent can't tell "the call never
ran" from "the call ran but the response was lost," so it re-executes a
non-idempotent write.

**The fix:** wrap the tool. The agent passes a stable key per logical
operation; retries reuse the key; the wrapper deduplicates and replays the
stored result instead of re-executing.

```python
from idemkey import guard, new_key

def capture_order(order_id: str) -> dict: ...   # your real tool

capture = guard(capture_order, verify=order_exists_in_db)

key = new_key()                      # or "order-7f3a" — the agent owns the key
capture("ord-123", idemkey=key)       # executes
capture("ord-123", idemkey=key)       # timeout retry -> replays stored result, never re-charges
```

## Install

```bash
pip install idemkey
```

No runtime dependencies (standard library only). Python 3.9+.
(0.2.0, described here, is not on PyPI yet — install from this repo with
`pip install .` until it is released.)

## 30-second demo

Copy, paste, run. A "charge" that times out *after* the money moved — the
classic agent double-charge — retried with the same key:

```python
import tempfile, os
from idemkey import guard, DedupStore

store = DedupStore(os.path.join(tempfile.mkdtemp(), "demo.db"))
charges = []

@guard(store=store, require_key=True, sleep=lambda s: None)
def charge(customer: str, cents: int) -> dict:
    charges.append(cents)                 # the side effect
    return {"charge_id": f"ch_{len(charges)}", "cents": cents}

first = charge("cus_42", 5000, idemkey="order-7f3a")
again = charge("cus_42", 5000, idemkey="order-7f3a")   # agent retries
print(first == again, len(charges))      # True 1  -> charged exactly once

try:
    charge("cus_42", 5000)               # agent forgot the key
except Exception as e:
    print(type(e).__name__)              # MissingKeyError -> nothing charged
```

## Usage

### The one rule

> **No key → no retry, no dedup.** A wrapped call without a key executes
> exactly once and is never retried. Retries and dedup happen only when a key
> is present. This is deliberate: silently retrying a keyless write is how
> duplicate orders happen.

Want "no key" to be an error instead? `guard(fn, require_key=True)` raises
`MissingKeyError` before the tool runs — recommended for every tool that
moves money, sends messages or creates records.

Keys must be non-blank strings of at most 255 characters. A blank key
(`idemkey=""`, e.g. an unset template variable) raises `InvalidKeyError`
instead of silently running without dedup.

Three ways to supply the key:

```python
# 1. Per-call kwarg (recommended — the agent owns the key lifecycle)
capture(order, idemkey="order-7f3a")

# 2. Fixed key at wrap time (rare; every call shares it)
guard(fn, key="singleton-op")

# 3. Derived key, opt-in (only for by-construction-idempotent operations
#    like ensure_webhook_registered — NEVER for transfer()-style calls an
#    agent may legitimately invoke twice with identical args)
guard(fn, derive=True, scope="webhooks")
```

The kwarg name defaults to `idemkey` and is popped before your function is
called (configurable via `key_arg=`).

### Safe retry

Only *exceptions* you classify as retryable are retried — default
`(TimeoutError, RetryableError)`, exponential backoff with full jitter,
3 retries. A returned result is **never** retried: after a result comes back
you can't know whether the write landed, so it's verified and committed
instead. Non-retryable exceptions propagate immediately with no second
execution, and the key is marked `failed` so a later retry with the same key
re-executes cleanly.

```python
from idemkey import RetryableError

@guard(max_retries=5, retry_on=(TimeoutError, RetryableError))
def flaky_tool(...): ...
```

### Partial-success verification

The `verify` hook runs after the write and before success is reported —
this catches "response lost but write landed":

```python
capture = guard(raw_capture, verify=lambda result: order_exists(result["id"]))
# verify returns False (or raises) -> VerificationError, key marked failed.
# NOTE: the write may still have landed; outcome is "unknown", not "failed".
```

### Same key, different args → loud error

The key locks to a fingerprint of (tool, args) on first use. Reusing a key
with different arguments raises `FingerprintMismatchError` — that's two
different operations sharing a key, or arg drift between retries, and
silently replaying call A's result for call B is worse than crashing.
Override with `fingerprint_policy="warn"` or `"replay"` if you mean it.

### Async tools

`async def` tools work out of the box — same rules, and the wrapper never
blocks the event loop (store I/O runs in a worker thread; waits use
`asyncio.sleep`). `verify` may be async too.

Cancellation (an MCP client timing out cancels the handler) is handled
without guessing:

| Cancelled while… | Key becomes | Same-key retry |
|---|---|---|
| claiming, before the tool started | `failed` | executes normally (nothing ran) |
| the tool or `verify` was running | `unknown` | raises `OutcomeUnknownError` — never re-runs a write that may have landed |
| the result was being saved | `completed` (save is shielded) | replays the real result |

Clear an `unknown` key with `idemkey forget KEY` after checking the
downstream system, or let a `ttl` expire it.

```python
@guard(verify=order_exists_async)
async def capture_order(order_id: str) -> dict: ...

await capture_order("ord-123", idemkey="order-ord-123")
```

### MCP adapter

The wrapper adds the key argument to the tool's signature, so MCP servers
(and LangChain, which build the tool schema from the signature) advertise an
optional `idemkey` string to the model and actually pass it through:

```python
from mcp.server.mcpserver import MCPServer   # mcp>=2; on mcp 1.x: from mcp.server.fastmcp import FastMCP
from idemkey import mcp_tool

mcp = MCPServer("orders")

@mcp.tool()
@mcp_tool(verify=order_exists, require_key=True)
def capture_order(order_id: str) -> dict: ...
# tool schema now includes: "idemkey": {"type": "string" | null}

# LangChain / CrewAI: guard() returns a plain callable with the same
# signature (+ idemkey), so langchain_core.tools.tool(guard(fn)) composes.
```

With `require_key=True` the key is a *required* string in the schema, so
the model is told up front that it must send one. Annotations written under
`from __future__ import annotations` (e.g. pydantic models) are resolved
against your module, so registration works either way.

> Upgrading from 0.1.x: before 0.2.0 the signature did **not** include
> `idemkey`, so MCP servers silently dropped the key and every call ran
> without dedup. Tested locally against mcp 1.30 and 2.3 (`tests/test_safety.py`,
> `tests/test_future_annotations.py`); the CI `mcp` job runs those tests on
> both SDK lines and fails if any of them is skipped.

### Namespaces and multiple clients

Putting `idemkey` in the tool schema means **the model chooses the key**.
idemkey stores every key as `scope|tool|prefix|key`, so:

- the same key on two tools (`charge` and `refund` both using
  `order-123`) never collides;
- a different call reusing a key never gets the other call's result (the
  argument fingerprint check, on by default);
- **on a server shared by several clients, give each client its own
  `key_prefix`.** Without it, client A can claim a predictable key such as
  `order-123` first with junk arguments, and client B's genuine call then
  fails with `FingerprintMismatchError` until the key expires ("key
  squatting"); the error also tells B that the key exists.

```python
import contextvars
current_client = contextvars.ContextVar("client", default="")

guard(fn, key_prefix=current_client.get)   # str, or a zero-arg callable per call
```

Scope, tool name and prefix may not contain `|`; the user key may. Pass
`namespace=False` only to keep reading a store written by 0.1.x, which stored
raw keys. The CLI accepts the plain key the agent used (`idemkey show
order-7f3a`) and lists the full stored keys if it exists in several
namespaces.

### Key expiry (optional)

By default a completed key replays forever (until `idemkey purge`). Pass
`ttl=` seconds to make a settled key expire, after which the same key starts
a brand-new operation — Stripe's behaviour, which prunes keys after 24 h:

```python
guard(fn, ttl=24 * 3600)
```

### CLI

```bash
idemkey log --status failed --limit 20   # recent entries
idemkey show order-7f3a                  # full row incl. replayed result
idemkey stats                            # counts by status
idemkey purge --older-than 24            # delete entries older than 24h (default TTL)
idemkey forget order-7f3a                # after checking by hand: let this key run again
```

`forget` refuses a `pending` key (an execution may still be running) unless
you add `--force`; the status check and the delete are a single atomic
statement. `purge` never deletes `pending` rows unless you pass
`--include-stale-pending` (then only those whose heartbeat is older than the
cutoff), and `--older-than` must be positive. Keys with control characters
are rejected at call time, and `log` escapes anything non-printable, so a
model-chosen key can't rewrite your terminal.

Store location: `--store PATH`, or `IDEMKEY_STORE` env, or
`~/.idemkey/store.db`. (`python -m idemkey` works too.)

### API reference

| Name | Kind |
|---|---|
| `guard(fn, **opts)` | decorator / wrapper factory (also `@guard`, `@guard(...)`); sync or async |
| `mcp_tool(fn, *, name, description, **guard_opts)` | MCP-tool wrapper |
| `new_key(prefix="idem_")` | random key |
| `derive_key(tool, args, kwargs, scope)` | deterministic key (opt-in) |
| `guarded.store_key(key)` / `namespaced_key(key, tool, scope, prefix)` | the key exactly as stored |
| `DedupStore(path)` | SQLite store (WAL, thread-safe) |
| Errors | `IdemKeyError`, `RetryableError`, `FingerprintMismatchError`, `VerificationError`, `ResultNotReplayableError`, `PreviousAttemptFailedError`, `InflightTimeoutError`, `InvalidKeyError`, `MissingKeyError`, `OutcomeUnknownError`, `StoreCorruptedError` |
| Warnings | `ClaimLostWarning` — this execution's claim was taken over before it could record its result |

Key `guard()` options: `key`, `key_arg="idemkey"`, `tool`, `derive=False`,
`require_key=False`, `scope="default"`, `key_prefix=None`, `namespace=True`, `store`, `max_retries=3`, `backoff_base=0.5`,
`backoff_cap=30.0`, `retry_on=(TimeoutError, RetryableError)`,
`verify=None`, `fingerprint_policy="error"`, `claim_ttl=300.0`,
`inflight_timeout=60.0`, `ttl=None`, `result_max_bytes=1_000_000`.

## How it compares

Checked 2026-11-06 against each project's own PyPI/docs page. "—" means the
page does not mention it; it may exist undocumented.

| | idemkey | [Powertools idempotency](https://docs.aws.amazon.com/powertools/python/1.26.1/utilities/idempotency/) | [idempotency](https://pypi.org/project/idempotency/) | [latch-idempotent](https://pypi.org/project/latch-idempotent/0.2.0/) | [agent-ledger](https://pypi.org/project/agent-ledger/0.1.1/) | [idempotency-kit](https://pypi.org/project/idempotency-kit/) |
|---|---|---|---|---|---|---|
| Built for agent tool calls | yes | no (Lambda) | no | yes | yes | no |
| Required runtime deps | none | DynamoDB table | — | none | — (Postgres for prod) | Redis 6+ |
| Persistent store with no server | SQLite | no | JSON file (Linux/macOS) | no (memory/Redis) | no (memory/Postgres) | no |
| Same key + different args rejected | yes | opt-in (`payload_validation_jmespath`) | — | — | — | yes |
| Retry built in (exceptions only) | yes | — | — | — | — | — |
| Post-write `verify` hook | yes | — | — | — | — | — |
| Refuse keyless calls | `require_key` | `raise_on_no_idempotency_key` | — | key always required | — | — |
| Async tools | yes | — | — | yes | yes (async only) | yes (async first) |
| Key in MCP tool schema | yes | — | — | — | — | — |
| Stale-claim takeover + owner fencing | yes | — | — | — | takeover | — |

What others do better (honestly): Powertools, latch-idempotent,
idempotency-kit and `idempotency` share state across machines through
DynamoDB or Redis — idemkey is single-host. agent-ledger adds human approval
gates and an audit query API; latch-idempotent bundles circuit breakers,
timeouts and budget caps.

## Limitations (honest)

- **At-least-once execution, exactly-once *effect*.** Nothing makes a network
  call truly exactly-once. The store turns the second execution into a replay;
  if the process dies between your write and the store commit, the next retry
  re-executes. Design your tools so re-execution after a *failed* key is safe.
- **Wall-clock TTL.** Stale-claim reclamation uses wall-clock heartbeats. An
  NTP jump forward can prematurely stale a live claim. Set `claim_ttl` above
  your max expected execution time; the wrapper heartbeats during execution to
  cover the normal case.
- **Single-file SQLite.** No cross-host dedup without a shared filesystem, and
  SQLite WAL on NFS is unsupported — keep the store on local disk.
- **`verify` can't un-land a write.** A false/lying verify hook reports
  success for a write that never landed. Make verify a real read-back check,
  keep it fast and bounded.
- **Replay returns the original result**, even if the world changed since
  (Stripe parity). Results go through a JSON round-trip: tuples come back as
  lists, arbitrary objects as `repr()` strings, payloads over
  `result_max_bytes` aren't replayable (`ResultNotReplayableError` on dup).
- **Derived keys dedup across processes** sharing a store when (scope, tool,
  args) collide — by design; scope accordingly, and never derive keys for
  intentionally-repeatable operations.
- **Keys are bearer tokens for replay — with the default fingerprint policy,
  only for an identical call.** A guessed key replays the stored result only
  if the tool and arguments match exactly; with `fingerprint_policy="warn"`
  or `"replay"` a guessed key returns another call's result, so don't use
  those on multi-client servers. See *Namespaces and multiple clients* for
  key squatting. The store file holds results in plaintext — idemkey creates
  the file `0600` (and directories it creates `0700`) automatically, but an
  operator can still loosen that afterwards, so treat the store as sensitive.
- **Fork-safe.** A `DedupStore` opened before `os.fork()` reconnects
  transparently in the child — SQLite connections are never shared across a
  fork. Threads share the one connection behind a lock, as before.
- **Async store I/O uses the default thread pool.** Fine for typical agent
  loads; very high-concurrency services sharing one SQLite file will be
  limited by SQLite's single writer.
- **A suspended owner can still double-execute.** If a call outlives
  `claim_ttl` *without* heartbeating (process frozen, laptop asleep), another
  caller takes the claim over and runs it again. Owner fencing guarantees the
  store keeps one consistent outcome and you get a `ClaimLostWarning`, but it
  cannot undo the second side effect.
- **Fingerprinting is of serialized arguments.** Objects with identical
  `repr()` share a fingerprint; keyword spelling is canonicalised via the
  function signature, but exotic callables without introspectable signatures
  fall back to raw args.
