# Changelog

## 0.3.0 - unreleased

Theme: close the "exactly-once illusion" window and make duplicates
measurable. Everything below is backward compatible (existing options,
stored rows and fingerprints keep working).

### Added
- **`atomic="tx"`** - the tool receives a sqlite3 connection in an open
  transaction on the store's database; its writes and the key's `completed`
  record commit in ONE `COMMIT`. Crash (SIGKILL) at any point = both or
  neither; a stale owner's transaction is rolled back. Sync tools, effects in
  the same SQLite file only.
- **`inject_key="idempotency_key"`** - hands the tool a stable downstream key
  (`idk_...`, a hash of the stored key; identical across retries and takeover)
  for Stripe-style `Idempotency-Key` APIs. The parameter is hidden from the
  tool schema.
- **`reconcile=fn(*args)`** - asked before any re-execution (stale takeover,
  retry after failure, `unknown` outcome): return the earlier attempt's result
  or `idemkey.NOT_FOUND`. Resolves `unknown` keys automatically; if the hook
  raises, the key is marked `unknown` (fail closed, `ReconcileError`).
- **Semantic fingerprints**: `10 == 10.0 == Decimal("10.00")`, NFC/NFD text,
  dataclasses / namedtuples / pydantic models / plain objects compared by
  content (not by a `repr()` with a memory address), cyclic arguments safe.
  `ignore=("request_id",)` and `normalize={"email": str.lower}` for volatile or
  cosmetic arguments. `derive="semantic"` derives a key from them. Stored
  v1 fingerprints still match (no re-execution after upgrading).
- **`derive="seq"` + `with idemkey.run(run_id):`** - run-scoped sequence keys:
  replaying a crashed run maps the Nth identical call to the same key (steps
  already done replay from the store) while two intentional identical calls in
  one run stay distinct. The counter advances only on success.
- **Privacy**: `redact=fn(result)`, `store_result=False`, `error_detail="type"`.
- `DedupStore(path, durability="normal")` - WAL-only fsync, ~4x more new-key
  throughput; survives process crashes, can lose the last records on power loss.
- `bench/bench.py` - committed fault-injection / 1000-racer / speed /
  semantic benchmark with competitors (see README), results in
  `bench/results.json`.

### Changed
- One shared heartbeat thread instead of one thread per call.
- Same-process waiters are woken on settle (no 50 ms poll); other processes
  poll with backoff from 2 ms.
- `claim()` retries transient `SQLITE_BUSY` like the settle writes already did.
- Fingerprinting refuses (ValueError) arguments nested too deeply to encode
  instead of crashing with `RecursionError`; v1 hit that on cyclic arguments.
- New exports: `NOT_FOUND`, `ClaimLostError`, `ReconcileError`, `run`,
  `downstream_key`, `fingerprint2`.

## 0.2.0 - unreleased

### Fixed
- **MCP / LangChain tools never received the key.** The wrapper's signature
  did not declare `idemkey`, so MCP servers (mcp 1.x `FastMCP` and 2.x
  `MCPServer`) built a schema without it and dropped the argument: every call
  ran with no dedup. The wrapper now exposes a keyword-only
  `idemkey: Optional[str] = None` (or your `key_arg`) in its signature.
- **`async def` tools were silently broken.** Guarding a coroutine function
  stored the *coroutine object's repr* as the result and replayed that string.
  Async tools are now supported natively (see Added).
- **Stale owners could overwrite their successor.** `complete`/`fail` are now
  fenced by a per-execution owner token (unique per call, not per process),
  so an execution whose claim was taken over can no longer clobber the new
  owner's outcome; it emits `ClaimLostWarning` instead.
- Heartbeats no longer touch rows that have already settled.
- A result too deeply nested to serialise is stored as not-replayable instead
  of raising `RecursionError` after the write succeeded.

### Added
- Native `async def` support: same semantics, store I/O off the event loop,
  async `verify` and async `sleep` accepted, cancellation marks the key
  `failed` (outcome unknown) so a same-key retry re-executes.
- `require_key=True`: refuse keyless calls with `MissingKeyError`.
- `ttl=` seconds: settled keys expire and the key starts a new operation
  (default `None`, unchanged behaviour).
- Key validation: `InvalidKeyError` for blank, non-`str` or >255-char keys.
- `idemkey forget KEY [--force]` CLI command and `DedupStore.delete()`.
- `py.typed` marker, GitHub Actions test matrix (Linux/macOS/Windows,
  Python 3.9–3.13), `.gitignore`.

### Changed (possibly breaking)
- `idemkey=""` and non-string keys used to fall through to "no key" (run with
  no dedup). They now raise `InvalidKeyError`. Pass `None` or omit the
  argument for a keyless call.
- Keys longer than 255 characters are rejected.
- `inspect.signature(guarded_fn)` now includes the key argument.
- Owner strings in the store are now `host:pid:token` instead of `host:pid`.
- `DedupStore.complete()` / `fail()` return `bool` and accept `owner=`
  (callers that pass no owner get the old unfenced behaviour).
- Removed accidentally committed `__pycache__/` and `*.egg-info/` from git.

### Fixed after independent panel review (still 0.2.0, unreleased)
- **MCP registration broke under `from __future__ import annotations`**
  (regression from the signature fix): string annotations such as a pydantic
  model were resolved against idemkey's module. The added signature now
  carries annotations evaluated against the tool's own module.
- **Async cancellation could run a write twice.** A cancelled call was marked
  `failed`, so the client's timeout-retry re-executed. It is now marked with
  the new, non-reclaimable status **`unknown`**; a same-key retry raises
  `OutcomeUnknownError` until `idemkey forget` (or `ttl`). Cancellation
  during the result commit no longer loses the result: the commit is
  shielded and completes. Cancellation before the tool starts releases the
  claim (`failed`, safe to retry).
- **Stale-claim takeover skipped the fingerprint check**, letting a call with
  different arguments execute under another call's key. Takeover now requires
  a matching fingerprint, and a mismatching call on an in-flight key fails
  immediately with `FingerprintMismatchError` instead of after
  `inflight_timeout`.
- **Keys are namespaced** as `scope|tool|prefix|key`; new `key_prefix=`
  (str or per-call callable) isolates clients sharing a store. README
  documents the multi-client key-squatting risk. `namespace=False` keeps the
  raw 0.1.x layout.
- **Terminal injection via `idemkey log`:** keys containing control
  characters or bidi overrides are rejected (`InvalidKeyError`), and `log`
  / `stats` escape non-printable text from older rows.
- **`purge` deleted live `pending` rows** and accepted `--older-than 0` or
  negative values. Pending rows are now kept unless
  `--include-stale-pending` (heartbeat older than the cutoff); non-positive,
  NaN or infinite values are rejected.
- **`forget` check-then-act race:** the delete itself now refuses `pending`
  rows (`DELETE ... WHERE status != 'pending'`).
- **`require_key=True`** now makes the key a required `str` in the signature,
  so MCP schemas list it under `required`.
- **Settling writes retry on `SQLITE_BUSY`** (bounded exponential backoff), so
  a tool that succeeded is not reported as an error because another process
  held the write lock past `busy_timeout`.
- CI: new `mcp` job runs the MCP tests against `mcp<2` and `mcp>=2` and fails
  if any are skipped. README's "verified" claim reworded to what is tested.

### Changed (possibly breaking, review round)
- Stored keys are namespaced. A store written by 0.1.x is not found under the
  new layout — drain it first, or pass `namespace=False` while migrating.
- New status `unknown` in the store and in `idemkey log --status`.
- `DedupStore.purge()` keeps pending rows by default and raises `ValueError`
  for non-positive ages; `DedupStore.delete()` refuses pending rows unless
  `allow_pending=True`.
- New: `DedupStore.find()`, `DedupStore.mark_unknown()`, `namespaced_key()`,
  `guarded.store_key()`, `OutcomeUnknownError`.

## 0.1.1
- Initial public release.
