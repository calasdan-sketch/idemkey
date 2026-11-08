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
PyPI currently has 0.1.1 only. This README describes 0.3.0 (unreleased): until
it is released, install it from a checkout of this repo with `pip install .`,
and read *Upgrading from 0.1.1* in the CHANGELOG first.

**Already have a store written by 0.1.1?** Run `idemkey --store YOUR.db migrate`
once (stop any 0.1.1 process first). It renames the old keys to the 0.2+ layout
so retries of earlier calls keep replaying instead of running again. Add
`--dry-run` to see the count first. (`idemkey doctor` tells you when it finds
old-layout keys.) Without it, those old keys are simply not found and the call
runs once more.

Check the install worked:

```bash
idemkey --version     # idemkey 0.3.0
idemkey demo          # a retried "charge" runs once; a keyless call is refused
```

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
Also refused, because models produce them by accident: a key with spaces or a
line break at either end (`"order-1 "` would be a second, different key), and
the placeholder words a model writes when it has no value (`null`, `none`,
`nil`, `undefined`, `nan`, `n/a`, `na`, any capitalisation).

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

raise RetryableError("429", retry_after=7)   # wait 7 s (capped at backoff_cap), not the jittered default
```

A timeout is exactly the case where the write may have landed. If you give
`guard()` a `reconcile=` hook (see *Closing the exactly-once window*), it is
asked before **every** re-run, including the retries inside one call - without
it the retry loop would write a second time.

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

### Typed results

A replay returns what the first call returned, including `tuple`, `bytes`,
`set`/`frozenset`, `Decimal`, `datetime`/`date`/`time`, `UUID` and dicts with
non-string keys (`result_codec="typed"`, the default). Namedtuples come back
as plain tuples; arbitrary objects are stored as `repr()` text and come back as
that string. `result_codec="json"` restores the plain-JSON behaviour of 0.2.
Rows stored by earlier versions decode unchanged.

### Seeing what happened

```python
guard(fn, on_event=lambda e: metrics.incr(e["event"]))   # or logging.getLogger("idemkey") at DEBUG
# events: executing, executed, failed, replayed, retryable_error, reconciled, fingerprint_mismatch
```

The event carries the stored key (model-chosen text): escape it before it
reaches a terminal. A hook that raises is ignored.

### Test your own tool

```python
from idemkey.testing import check_tool

report = check_tool(create_order, args=("ord-1",), effects=lambda: len(fake_db))
print(report)            # same key twice / write landed + response lost / replay equal
report.raise_if_unsafe()
```

Pass the same `guard()` options you use in production (`reconcile=`,
`inject_key=` ...). It calls your tool for real, so aim it at a sandbox.
`lost_response(fn)` is the underlying fault injector.

### CLI

```bash
idemkey log --status failed --limit 20   # recent entries
idemkey log --json                       # same, machine-readable (no results)
idemkey export --no-results > audit.jsonl  # every row as JSON lines
idemkey doctor                           # unknown / stale-pending keys, loose file mode; exit 1 = look at it
idemkey doctor --json                    # same, machine-readable (same exit code)
idemkey stats --json
idemkey purge --older-than 24 --dry-run  # count what would go, delete nothing
idemkey migrate --dry-run             # old 0.1.x keys -> 0.2+ layout (drop --dry-run to do it)
idemkey demo                             # 30-second self-check in a temporary store
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

Exit codes: 0 ok; 1 error, key not found, or `doctor` found something; 2 bad
usage or a `pending` key refused by `forget`; 3 the key exists in several
namespaces. Data goes to stdout, messages and errors to stderr. A corrupt or
unopenable store prints `idemkey: <reason>`, never a traceback. On a console
that cannot show a character in a stored key (ASCII, Windows cp1252) it is
written as `\uXXXX`.

### API reference

| Name | Kind |
|---|---|
| `guard(fn, **opts)` | decorator / wrapper factory (also `@guard`, `@guard(...)`); sync or async |
| `mcp_tool(fn, *, name, description, **guard_opts)` | MCP-tool wrapper |
| `idemkey.testing.check_tool(fn, args, effects=, **guard_opts)` / `lost_response(fn)` | safety report / fault injector for tests |
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
`inflight_timeout=60.0`, `ttl=None`, `result_max_bytes=1_000_000`,
`result_codec="typed"`, `on_event=None`.

## Closing the exactly-once window (0.3.0)

A key store alone leaves one gap: the process dies *after* the write but
*before* the store records it, and the retry writes again. Three opt-in tools
close it, depending on what your tool talks to:

```python
# 1. Your effect is in the same SQLite file: fully atomic.
@guard(atomic="tx")
def book(seat, *, tx):                 # tx = open transaction on the store DB
    tx.execute("INSERT INTO bookings(seat) VALUES (?)", (seat,))

# 2. Your API takes an idempotency key (Stripe etc.): pass a stable one.
@guard(inject_key="idempotency_key")
def charge(cents, *, idempotency_key): # same value on every retry/takeover
    stripe.PaymentIntent.create(amount=cents, idempotency_key=idempotency_key)

# 3. Neither: ask the world before re-running.
@guard(reconcile=lambda order: find_order(order) or NOT_FOUND)
def create_order(order): ...
```

Other 0.3.0 tools: `ignore=("request_id",)` / `normalize={"email": str.lower}`
for cosmetic duplicates (10 vs 10.0, NFC/NFD text, equal dataclasses are
merged by default), `with idemkey.run(run_id): ...` + `derive="seq"` so a
replayed agent run reuses its keys while two intentional identical calls stay
distinct, and `redact=` / `store_result=False` / `error_detail="type"` to keep
PII out of the store.

## Benchmark (re-run: `python bench/bench.py all --legacy-path DIR_OF_0.2 --prev-path DIR_OF_PREVIOUS_ROUND`)

Fault injection, 90 logical operations: attempt 1 is SIGKILLed (a third of
the time before the effect, a third after the effect but before the library
recorded it, a third after everything), attempt 2 is a fresh process retrying
with the same key. 2-vCPU sandbox VM, Python 3.13, SQLite 3.53, redis-server
for latch; one run, **on a VM shared with other jobs (load average 3-10 while it
ran), so the speed columns are noisy** - the same code measured 1721 new keys/s
in the previous round's run and 996 in this one. Raw numbers: `bench/results.json` (round 3, with competitors); a round-4 re-run of
the idemkey rows only is `bench/results-round4.json` - same correctness numbers, speed
columns not comparable (see `docs/ROUND4.md`). Round 5 re-ran the idemkey rows against the previous commit (`bench/results-round5.json`): every correctness number is identical; speed was again dominated by load noise (see `docs/ROUND5.md`).

| | duplicate effects | lost effects | 1000 racers x 20 keys: keys with a duplicate | new keys/s | replays/s |
|---|---|---|---|---|---|
| no dedup (naive retry) | 67% | 0% | - | - | - |
| idemkey 0.2 | 33% | 0% | 0 | 548 | 15417 |
| idemkey 0.3 default | 33% | 0% | 0 | 996 | 19412 |
| idemkey 0.3 `durability="normal"` | 33% | 0% | 0 | 4725 | 12977 |
| idemkey 0.3 `reconcile=` | **0%** | 0% | 0 | 851 | 19643 |
| idemkey 0.3 `inject_key=` (downstream dedups) | **0%** | 0% | 0 | 1243 | 19954 |
| idemkey 0.3 `atomic=` | **0%** | 0% | 0 | 745 | 19046 |
| [idempotency](https://pypi.org/project/idempotency/) 0.1.0 FileStore | 33% | 0% | 0 (3 `KeyError`s) | 2871 | 56000 |
| [latch-idempotent](https://pypi.org/project/latch-idempotent/) 0.4.1 + Redis | 33% | 0% | 17 | 8438 | 13981 |

Read it honestly: the 33% that remains is the crash-after-write window, which
*no* key store can close alone - the three opt-in modes close it only because
the benchmark's downstream supports the matching technique (a same-file
transaction, a deduplicating API, a lookup). Semantic duplicates (11
cosmetic retries of the same call, 10 genuinely different calls): 0.2 merged
5/11 and 0 wrong; 0.3 merges 9/11 by default, 11/11 with `ignore=`/`normalize=`,
0 wrong. The competitors were run as released and not tuned; they were not
designed for this fault model, and the others' faster new-key rate comes from
weaker durability (no per-key fsync). latch + Redis showed duplicates under the
1000-racer test in this run and in one of two runs last round (0 in the other).
Temporal, Inngest, DBOS and Restate (durable-execution engines) were not run:
they are servers/runtimes, not drop-in wrappers; their documented model is
at-least-once with user-supplied idempotency keys for outside effects
(unverified here).

**Lost response inside one call** (round 3; the write lands, the tool then
raises `TimeoutError`, the library's retry loop runs it again; 60 operations,
in-process, `bench.py lost`). Operations with a duplicate effect:

| | plain retry | `reconcile=` | `inject_key=` |
|---|---|---|---|
| previous round | 60 / 60 | 60 / 60 | 0 / 60 |
| this tree | 60 / 60 | **0 / 60** | 0 / 60 |

Plain retry duplicates by design (nothing tells the wrapper the first attempt
landed); that is what `reconcile=` / `inject_key=` are for.

**Hostile or large arguments** (`bench.py fp`, each probe limited to 20 s):

| fingerprint of | previous round | this tree |
|---|---|---|
| `Decimal("1e999999999")` | hangs (>20 s) | 0.0 s |
| `10**100000` | `ValueError` | 0.004 s |
| `1.0000000000000000000000000000001` vs `...2` as `Decimal` | merged (wrong) | distinct |
| list of 300,000 ints | 0.35 s | 0.16 s |
| 20 MB string | 0.21 s | 0.13 s |

## How it compares

Checked 2026-11-06 against each project's own PyPI/docs page; on 2026-11-07 the
PyPI descriptions of agent-ledger 0.2.1, latch-idempotent 0.4.1 and
idempotency-kit 0.5.0 were re-read (the first part of each page only), which
is where the last three rows come from. "—" means the page does not mention it;
it may exist undocumented. Cells for features not re-read keep the earlier
versions' answers.

| | idemkey | [Powertools idempotency](https://docs.aws.amazon.com/powertools/python/1.26.1/utilities/idempotency/) | [idempotency](https://pypi.org/project/idempotency/) | [latch-idempotent](https://pypi.org/project/latch-idempotent/) 0.4.1 | [agent-ledger](https://pypi.org/project/agent-ledger/) 0.2.1 | [idempotency-kit](https://pypi.org/project/idempotency-kit/) 0.5.0 |
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
| Event / metrics hook | `on_event`, logger | — | — | `latch.tracing` | receipts / audit trail | built-in metrics |
| Fault-injection helper for your tests | `idemkey.testing` | — | — | `latch.chaos` | — | — |
| Human approval of a call | no | — | — | — | yes | — |

What others do better (honestly): Powertools, latch-idempotent,
idempotency-kit and `idempotency` share state across machines through
DynamoDB or Redis - idemkey is single-host. agent-ledger adds intent-bound
human approval gates and a queryable audit/receipt trail; latch-idempotent
bundles circuit breakers, timeouts, budget caps and saga/compensation for
multi-step work. idemkey is narrower on purpose: a dependency-free store plus
the three ways of closing the crash-after-write window.

## Limitations (honest)

- **At-least-once execution, exactly-once *effect*.** Nothing makes a network
  call truly exactly-once. The store turns the second execution into a replay;
  if the process dies between your write and the store commit, the next retry
  re-executes. Design your tools so re-execution after a *failed* key is safe.
- **`atomic=` only covers effects in the store's own SQLite file**, sync tools
  only, and holds the database write lock for the tool's duration (keep it
  short). `reconcile` and `inject_key` are only as good as the downstream's
  lookup / dedup. `derive="seq"` assumes a deterministic replay: an LLM that
  re-issues a *successful* call itself gets a new key, so keep passing explicit
  keys in live agent loops.
- **Benchmark scope**: single 2-vCPU VM, one run per cell, POSIX only
  (`bench/bench.py` uses `fcntl`).
- **A locked store stops the call, loudly.** If another program holds the
  store file locked for about half a minute (a DB browser with an open
  transaction, a stuck backup), `StoreBusyError` (a `sqlite3.OperationalError`)
  is raised and the tool is **not** started. Nothing is retried forever.
- **Deleting the store file under a running process** is not noticed: that
  process keeps writing to the now-unlinked file, and other processes start a
  new, empty store. Don't delete or replace the file while agents run.
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
  (Stripe parity). Results are stored as tagged JSON: the common types round-trip
  (see *Typed results*), arbitrary objects come back as `repr()` strings, and
  payloads over `result_max_bytes` - or a result that cannot be serialised or
  redacted - are not replayable (`ResultNotReplayableError` on a duplicate; the
  call itself still succeeds, with a `RuntimeWarning`).
- **Long text and bytes are fingerprinted by digest** (above 4096 characters),
  numbers exactly, but other exotic types by `repr()` with addresses scrubbed:
  two different lambdas or generators share a fingerprint.
- **Tested on Linux only.** Round 4 ran the test suite from a freshly built
  wheel on CPython 3.9, 3.10, 3.11, 3.12 and 3.13 (see `docs/ROUND4.md` for
  the numbers). macOS and Windows have never been run: CI is configured for
  them, and Windows was reviewed by reading the code only (file modes are
  POSIX-only and skipped there, the CLI no longer crashes on a cp1252 console),
  but nobody has watched it work.
- **Windows: file privacy is not enforced.** The `0600` / `0700` modes are
  POSIX-only; on Windows the store file gets whatever access the folder it lives
  in gives, so keep it in your user profile. `idemkey doctor` skips the mode check
  there.
- **`idemkey export` includes the claim owner (host name and pid)** and, unless you
  pass `--no-results`, the stored results.
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
  fork, and (since round 4) the store's locks are replaced in the child, so a
  fork taken while the heartbeat thread held one no longer hangs the child. Threads share the one connection behind a lock, as before.
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
