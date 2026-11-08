# Round 5 - upgrade path, hostile model input, locked store, new test methods

Scope: what rounds 1-4 did not look at. Rounds 1-4 are in CHANGELOG.md and docs/ROUND4.md.

**How the review was done, honestly.** No sub-agent tool was available, so one author
played every specialist in turn and ran real commands for each; every finding names the
command that showed it. This is not an independent second opinion. Everything ran on a
2-vCPU VM shared with other jobs (load average 6-10), so all timings are noisy.

## Baseline (before any change)

| | result |
|---|---|
| tests | 232 passed, 3 skipped (the skips need `mcp` / `pydantic`, not installed in that venv) |
| `ruff check idemkey tests` | clean |
| `mypy idemkey` | **8 errors** (first time a type checker was run) |
| PyPI (`curl https://pypi.org/pypi/idemkey/json`, 2026-11-08) | 0.1.1 only |
| bench | duplicates 33% default/fast, 0% reconcile/inject/atomic; 0 keys with a duplicate in the 1000-racer test |

## Author line

Already correct from round 4 (`authors = [{ name = "Calas Automations Inc." }]`, LICENSE
`Copyright (c) 2026 Calas Automations Inc.`); re-grepped the tracked tree for
`calasdan`, `Dan Calas`, `author`: no other hit. Added `idemkey.__author__` and
`tests/test_round5.py::test_author_is_pinned_everywhere` (pyproject, LICENSE, `__author__`)
and `test_installed_metadata_names_the_author` (reads the installed package's metadata).
Proof from the freshly built wheel:
`unzip -p dist/*.whl 'idemkey-*.dist-info/METADATA' | head` ->
`Author: Calas Automations Inc.`; `licenses/LICENSE` is in the wheel. No `project.urls`
were added: whether the repository is public is not known, so no address is claimed.
The package has no SARIF/JUnit output, so there is no tool-metadata field to fix.

## Specialists and findings

1. **Payments-API engineer (Stripe-style idempotency semantics).**
   Compared behaviour with Stripe's documented rules (same key + different parameters is an
   error; key up to 255 characters; keys pruned after 24 h; a request still in flight gives
   a conflict). All present: `FingerprintMismatchError`, `MAX_KEY_LENGTH=255`, default 24 h
   purge TTL, `InflightTimeoutError`. Not present: Stripe replays a *failed* result
   too; idemkey re-runs a failed key by design (idea 5, NOT DONE). Found a real gap on the
   key itself: `"order-1 "` and `"order-1"` were two keys (probe: `f(1, idemkey=" order-1")`
   ran the tool again). Fixed (refused with a plain message).
2. **Distributed-systems engineer.**
   Read every `time.time()` use (`grep -n "time\.time\|monotonic" idemkey/*.py`): in-process
   waits use `monotonic`; cross-process heartbeats must use wall-clock, so a clock step
   forward or a long process freeze can make a live owner look dead (already in the README
   limits; owner fencing limits the damage to a `ClaimLostWarning`). Nothing new to fix;
   shared-host stores remain out of scope (idea 20).
3. **SQLite / concurrency expert.**
   `probe2.py`: hold `BEGIN EXCLUSIVE` from another connection, then call a guarded tool.
   The tool is **not** run (good) but the caller got a bare `sqlite3.OperationalError:
   database is locked` after 31.7 s. Fixed: `StoreBusyError` (subclass of
   `sqlite3.OperationalError`, so old except-clauses still work) with a plain sentence.
   Second probe: `os.remove(store.db)` while a store is open - the process keeps writing to
   the unlinked file and nothing notices (README limit added; detection is idea 9, NOT DONE).
   `PRAGMA quick_check` was not used anywhere; `doctor` now runs it.
4. **Agent-tool-calling developer (what models actually send).**
   `probe1.py` with 14 odd `idemkey=` values: ints, floats, bool, bytes, lists, control and
   bidi characters are refused already. Accepted but harmful: the strings `"None"` and
   `"null"` (what a model writes for an unset optional argument; two unrelated calls would
   share one key, or identical repeats would be merged), and keys padded with a space.
   Fixed: refused with `InvalidKeyError` (listed in the CHANGELOG as a deliberate behaviour
   change). Ran the MCP integration tests against `mcp` 2.3.0 and `pydantic` 2.14.3:
   268 passed, 0 skipped.
5. **Release / upgrade-path engineer.**
   `pip download idemkey==0.1.1 --no-deps`, wrote a store with that real wheel
   (`old_write.py`), then opened it with this tree. With `namespace=False` all 9 sample
   calls replay (differential check of the old fingerprint formats: 9/9 incl. `10.0`,
   `-0.0`, `1e20`, non-ASCII text, nested dict). With the default `namespace=True` the old
   keys are *not found* and the call runs a second time - the README only said "pass
   `namespace=False`". Fixed: `idemkey migrate [--dry-run] [--json]` /
   `DedupStore.migrate_legacy_keys()`, plus a `doctor` note. Proved from the built wheel:
   `doctor` reports 9 old-layout keys, `migrate --dry-run` says 9, `migrate` renames them,
   `doctor` is quiet, and the built wheel then replays 3 of 3 probe calls. One caveat
   found while testing: copying only `store.db` and not its `-wal` file loses recent rows
   (SQLite behaviour, not specific to idemkey) - back up with `idemkey export` or copy all
   three files.
6. **QA / test-methodology expert.**
   New methods: (a) golden-file tests for all 9 CLI output formats (`tests/golden/`, TZ pinned,
   update with `IDEMKEY_UPDATE_GOLDEN=1`); (b) property-based tests with `hypothesis`
   (JSON-value codec round trip, dict-order independence of the fingerprint, "different values
   never share a fingerprint unless deliberately merged", injectivity of `namespaced_key`);
   ran once at 3,000 examples per property (354 s): no counter-example; (c) a model-based test:
   random call sequences against a plain-dict reference model, asserting exactly one real
   execution per key and mismatch errors on changed arguments; (d) `bench/mutation_check.py`:
   15 deliberate breakages (INSERT OR REPLACE, inverted takeover test, dropped owner fence,
   `forget` of pending rows, blank/padded/placeholder keys, namespace dropped, all numbers
   alike, ...): 15/15 killed. The first run found one mutant pattern that matched 3 places
   (a bug in the script, not the library); fixed, and made the owner-fence mutant keep the
   parameter count so it is caught by behaviour and not by a crash.
7. **Type-checking / maintainer reviewer.**
   `mypy idemkey`: 8 errors. 5 were one variable reused as two types in the number
   fingerprint (`keys.py`); behaviour kept identical (differential check above, golden and
   property tests green), 3 were `Optional` connection typing. All fixed; mypy and ruff now run in a
   new CI job together with the mutation check.
8. **Competitor's maintainer (read-only, 2026-11-08).**
   PyPI JSON: latch-idempotent 0.4.1, idempotency-kit 0.5.0, agent-ledger 0.2.1,
   idempotency 0.1.0 - same versions as the README table, so nothing in the comparison
   changed. Their feature pages were not re-read (only versions and summaries).

## Results

| | before | after |
|---|---|---|
| tests, repo checkout, Python 3.13, with `mcp`+`pydantic`+`hypothesis` | 232 passed + 3 skipped (without those) | **268 passed, 0 skipped** |
| tests, built wheel, run from outside the tree | not run this round | Python 3.13.18: 265 passed, 3 skipped; Python 3.9.25: 265 passed, 3 skipped (skips = no `mcp`/`pydantic`) |
| `ruff check idemkey tests bench` | clean | clean |
| `mypy idemkey` | 8 errors | clean |
| mutation check | did not exist | 15/15 killed |
| `twine check` sdist + wheel | passed | passed; sdist now carries `tests/golden` and `docs` |
| locked store | raw `OperationalError` | `StoreBusyError`, plain text |

Benchmark (`bench/bench.py all --impl ...`, raw `bench/results-round5.json`; "before" = the
previous commit run the same way): correctness numbers identical - duplicate effects 33%
default/fast, 0% reconcile/inject/atomic; 0 keys with a duplicate in the 1000-racer test, no
errors; semantic merges 9/11 default, 11/11 configured, 0 wrong; lost-response 60/60 plain,
0/60 `reconcile=`/`inject_key=`; fingerprint probes unchanged. Speed: the single batch
run read lower after (default 1054 -> 759 new keys/s) but three interleaved runs of
the same code pair gave before/after 247/311, 250/719, 1199/1193 new keys/s - noise at
load 6-10. **No speed change is claimed in either direction**; the only hot-path addition is
two string checks per call.

## Scored ideas (value / differentiation / effort / risk, 1-5)

| # | Idea | V | D | E | R | Status |
|---|---|---|---|---|---|---|
| 1 | `idemkey migrate` for 0.1.x stores | 5 | 2 | 2 | 2 | DONE |
| 2 | Refuse padded keys | 4 | 2 | 1 | 2 | DONE |
| 3 | Refuse placeholder keys (`null`, `none`, ...) | 4 | 4 | 1 | 2 | DONE |
| 4 | `StoreBusyError` with a plain message | 4 | 2 | 1 | 1 | DONE |
| 5 | Replay a deterministic *failure* under the same key (Stripe parity), opt-in | 3 | 3 | 3 | 3 | NOT DONE - changes the "failed = may re-run" rule; needs an owner decision |
| 6 | `doctor` runs SQLite `quick_check` | 3 | 2 | 1 | 1 | DONE |
| 7 | `doctor` notes old-layout keys | 4 | 2 | 1 | 1 | DONE |
| 8 | mypy clean + CI job | 3 | 1 | 2 | 1 | DONE |
| 9 | Detect the store file deleted/replaced under a running process (inode check) | 3 | 3 | 3 | 2 | NOT DONE - README limit added instead |
| 10 | Golden tests for every CLI format | 3 | 1 | 2 | 1 | DONE |
| 11 | Property-based tests (hypothesis) | 3 | 2 | 2 | 1 | DONE |
| 12 | Model-based test against a dict reference | 4 | 2 | 2 | 1 | DONE |
| 13 | Mutation check script + CI | 3 | 3 | 2 | 1 | DONE |
| 14 | Upgrade test against the real 0.1.1 wheel as a unit test | 3 | 2 | 3 | 2 | NOT DONE - needs network at test time; done by hand, steps in this file |
| 15 | Automatic read of a legacy raw key on a namespaced miss (no migrate step) | 4 | 3 | 3 | 4 | NOT DONE - guessing across layouts could replay the wrong row; migrate is explicit |
| 16 | `idemkey import` (restore from `export`) | 3 | 2 | 3 | 3 | NOT DONE |
| 17 | Postgres/Redis shared store | 5 | 4 | 5 | 4 | NOT DONE - breaks zero dependencies |
| 18 | Real Windows/macOS run | 5 | 1 | 2 | 2 | NOT DONE - needs a runner |
| 19 | Trusted-publisher release workflow | 4 | 2 | 3 | 3 | NOT DONE - PyPI-side setup by the owner |
| 20 | `project.urls` | 4 | 1 | 1 | 1 | NOT DONE - repository address/visibility unknown |
| 21 | Pin GitHub Actions by commit hash | 3 | 1 | 1 | 2 | NOT DONE - cannot look hashes up offline |
| 22 | pytest fixture around `check_tool` | 3 | 3 | 2 | 1 | NOT DONE |
| 23 | OpenTelemetry events | 3 | 2 | 3 | 2 | NOT DONE - `on_event` allows it |
| 24 | Encryption at rest | 3 | 3 | 4 | 4 | NOT DONE - key management is the hard part |
| 25 | Human approval gates | 2 | 2 | 5 | 3 | NOT DONE - out of scope |
| 26 | Fuzz the CLI with random bytes as store files | 3 | 1 | 2 | 1 | NOT DONE (round 4 covered corrupt/empty/folder) |
| 27 | Fencing token passed to the tool so a downstream can reject a stale owner | 4 | 4 | 4 | 3 | NOT DONE - needs downstream support; `inject_key` covers the common case |

## What could not be tested

- Windows and macOS (nothing ran there). `tests/test_round5.py` skips the two golden
  files with local-time columns on Windows.
- Python 3.10-3.12 from a built wheel (only 3.9 and 3.13 this round; round 4 did all five).
- MCP paths on Python 3.9 (SDK needs a newer Python); `mcp` 1.x (only 2.3.0 was run).
- A store on NFS / a network drive; a read-only mount.
- Anything on PyPI itself: nothing was uploaded, tagged or pushed.
- Whether `migrate` is fast on a very large store (1M+ rows): it is one transaction holding
  the write lock; not measured.
- Speed deltas: not measurable on this shared VM.
