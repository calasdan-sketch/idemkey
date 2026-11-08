# Changelog

## 0.3.0 - unreleased

Versions on PyPI as of 2026-11-07: 0.1.1 only (0.2.0 was never released, so
0.3.0 is the next release and carries the 0.2.0 changes below). **Upgrading
from 0.1.1:** stored keys are now namespaced (`scope|tool|prefix|key`); to keep
reading a store written by 0.1.x pass `namespace=False`. Calls with a key and
the same arguments keep replaying; numeric/text fingerprints written by 0.1/0.2
still match.

### Round 5 (upgrade path, hostile model input, locked store) - fixed and added

- **Upgrading from 0.1.1 silently re-ran old calls.** 0.1.1 stored raw keys; 0.3
  looks them up namespaced, so a retry of a call made before the upgrade ran
  again unless `namespace=False` was passed (checked by writing a store with the
  real 0.1.1 wheel). New `idemkey migrate [--dry-run] [--json]` and
  `DedupStore.migrate_legacy_keys()` rename the old rows in one transaction
  (never overwrites, skips names with `|`); `idemkey doctor` now says when it
  sees old-layout keys. Stop 0.1.1 processes before migrating: they write raw
  keys again.
- **Behaviour change, on purpose:** keys with spaces/line breaks at either end
  and the placeholder words `null`, `none`, `nil`, `undefined`, `nan`, `n/a`,
  `na` now raise `InvalidKeyError`. Models fill an optional string argument
  with such text; before, two unrelated calls could end up sharing the key
  `"null"`, and `"order-1 "` / `"order-1"` were two keys for one order.
- **A locked store file gave a raw `sqlite3.OperationalError` after ~30 s.** It
  is now `StoreBusyError` (still a `sqlite3.OperationalError`) with a plain
  sentence; the tool is not started (as before).
- `idemkey doctor` runs SQLite's `PRAGMA quick_check` and reports damage.
- `mypy idemkey` is clean (was 8 errors; one was a variable reused as two types
  in the number fingerprint - behaviour unchanged, golden-checked against 0.1.1
  fingerprints). `idemkey.__author__` added.
- Tests: golden-file tests for every CLI output format (`tests/golden/`),
  property-based and model-based tests with `hypothesis` (`pip install hypothesis`;
  skipped without it), and `bench/mutation_check.py` (15 deliberate breakages,
  all caught).

### Round 4 (the first hour after `pip install`) - fixed

- **The sdist and wheel shipped without a LICENSE file** although the metadata
  says MIT. `LICENSE` (MIT, Calas Automations Inc.) is now in both, with the
  CHANGELOG in the sdist; Python 3.9-3.13 classifiers added.
- **A keyless call could be stopped by its own arguments.** The README promises
  "no key -> runs exactly once", but the arguments were fingerprinted anyway, so
  an argument whose `repr()` raised (or a huge one) blocked a call that needs no
  dedup. Keyless calls are no longer fingerprinted. A keyed call with such an
  argument now raises a plain `ValueError("cannot fingerprint an argument ...")`
  before the tool runs, instead of the argument's own exception.
- **The CLI printed Python tracebacks** for a corrupt store file, a store path
  that is a folder, or a folder that cannot be created. It now prints
  `idemkey: <plain reason>` on stderr and exits 1. `DedupStore(...)` raises
  `StoreCorruptedError` (not a bare `OSError`) when it cannot create the folder.
- **`idemkey log` crashed on a non-UTF-8 console** (ASCII / C locale / Windows
  cp1252) when a stored key held other characters. Output now falls back to
  `\uXXXX` escapes. `idemkey export` writes LF line ends on Windows too.
- **A forked child could hang forever.** `os.fork()` taken while the parent's
  heartbeat thread held the store lock left the child waiting on a lock no
  thread in the child would ever release (about half the runs of a repro; it
  also made the suite hang). Every store now gets fresh locks in the child.
- Error messages that echo a key are escaped and cut to 80 characters.
- A flaky timing test (`test_inprocess_waiters_wake_immediately`) failed on a
  loaded machine; its margin is now wide enough. POSIX-mode tests are skipped on
  Windows.
- Removed a stray binary wheel of another project from the repo root.

### Round 4 - added

- `idemkey --version`, `idemkey demo` (a 30-second self-check in a temporary
  store: the retried charge runs once), `idemkey stats --json`,
  `idemkey doctor --json` (exit code unchanged), `idemkey purge --dry-run`
  (`DedupStore.purge(dry_run=True)`).
- `docs/ROUND4.md` (review notes, scored ideas, untested items) and
  `PUBLISH-CHECKLIST.md`.

### Round 3 (review pass A + B) - fixes

- **A write that landed could be re-run after a lost response, even with
  `reconcile=`.** Reconcile was consulted only on a stale takeover / later
  retry, not between the attempts of one call: `TimeoutError` -> backoff ->
  the tool ran again. It is now asked before every in-call retry (sync and
  async). Benchmark: 60/60 duplicate effects before, 0/60 after.
- **Two different amounts could be treated as the same call.** Numeric
  fingerprints went through the global `decimal` context (28 digits), so
  `Decimal("1.0000000000000000000000000000001")` and `...2` shared a
  fingerprint and the second call replayed the first call's result. Numbers
  are now encoded exactly.
- **A hostile or accidental `Decimal("1e999999999")` argument hung the
  caller** (the encoder expanded it to an integer). It is now encoded as
  digits + exponent in microseconds. Integers above the interpreter's
  4300-digit limit no longer raise from the fingerprint.
- **A result that could not be serialised (or a `redact=` that raised) left
  the key `pending` after the write had landed**, so a later retry re-ran the
  write. It is now recorded as `completed`, not replayable, with a
  `RuntimeWarning`; the unredacted value is never stored.
- `idemkey log --limit -5` listed everything; it now requires a positive
  number. `idemkey export | head` no longer prints a traceback.
- `idemkey log` shortens long keys in the middle so the end of the key stays
  visible.

### Round 3 - added

- **Typed replay** (`result_codec="typed"`, the default): tuples, `bytes`,
  `set`/`frozenset`, `Decimal`, `datetime`/`date`/`time`, `UUID` and dicts
  with non-string keys replay as themselves, so a replay equals the first
  result. `result_codec="json"` keeps the old behaviour (tuples as lists,
  other types as `repr()` strings). Stored rows from earlier builds still
  decode. Arbitrary objects are still stored as `repr()` text.
- **`RetryableError(retry_after=seconds)`** (or any exception with a numeric
  `retry_after` attribute): used as the wait before the next attempt, capped
  at `backoff_cap`.
- **`on_event=callable`** and the `idemkey` logger (DEBUG): events
  `executing`, `executed`, `failed`, `replayed`, `retryable_error`,
  `reconciled`, `fingerprint_mismatch`. A hook that raises never affects the
  call.
- **`idemkey.testing`**: `lost_response(fn)` fault injector and
  `check_tool(fn, ...)`, which runs a tool through "called twice" and "write
  landed, response lost" and prints a SAFE / NOT SAFE report.
- **CLI**: `idemkey export` (JSON lines, `--no-results`), `idemkey doctor`
  (unknown / stale-pending keys, loose file mode read before the store's own
  open tightens it to 0600; exit 1 when a human is needed), `idemkey log --json`. `DedupStore.iter_rows()`.
- `bench/bench.py`: `lost` (write lands, response lost, inside one call) and
  `fp` (large / hostile argument probes) scenarios with a before/after row.

### Round 3 - changed

- v2 fingerprints of text or bytes longer than 4096 characters are a digest of
  the content, and some numbers are spelled differently (exactness fix above).
  v2 fingerprints have not been released; rows written by 0.1/0.2 still match
  through the v1 digest.
- ~35% faster fingerprints for large integer lists.

### Original round 2 notes

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
