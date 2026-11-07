# Round 4 - the stranger's first hour

Scope: what a person gets who types `pip install idemkey` and follows the README.
Rounds 1-3 are in CHANGELOG.md; this round only adds what was missing.

**How the review was done, honestly.** No sub-agent tool was available in this
run, so one author played every role below, one after another. Each finding lists
the command that showed it. Nothing here is an independent second opinion.
Everything ran on one Linux VM shared with other jobs (load average 12-17 for
most of the run), so timings are noisy.

## Baseline (before any change)

| | result |
|---|---|
| tests | 217 passed (one earlier run failed `test_inprocess_waiters_wake_immediately` on a loaded machine; two other runs **hung** on the forked-child test, see finding 5) |
| lint | `ruff check idemkey tests` clean |
| build | `python -m build` ok, `twine check` PASSED for sdist and wheel |
| PyPI | `pip index versions idemkey` -> only **0.1.1**. Repo is 0.3.0 (0.2.0 never shipped) |
| bench (`bench/bench.py all`, idemkey rows only) | duplicate effects 33% (default, fast) / 0% (reconcile, inject, atomic); 1000 racers x 20 keys: 0 duplicates; lost-response-in-call 60/60 plain, 0/60 reconcile and inject_key; speed numbers below |

## Specialists and what each found

1. **First-time user (README only, clean venv, folder with spaces).**
   `python -m venv fresh && fresh/bin/pip install dist/*.whl`, then the README
   30-second demo with `python -I`: prints `True 1` and `MissingKeyError` - works.
   Found: `idemkey --version` -> "error: the following arguments are required: cmd";
   no way to check an install in one command. Fixed: `--version`, `idemkey demo`.
2. **Release / packaging engineer.** `unzip -l dist/*.whl`, `tar tzf dist/*.tar.gz`:
   **no LICENSE file in either artifact** although the metadata says MIT; the
   sdist had no CHANGELOG; build printed a setuptools deprecation
   (`project.license` as a table; the message says builds stop being supported
   after 2027-02-18); a 16 KB binary wheel of a *different* project
   (`idempotency-0.1.0`) was committed in the repo root; classifiers had no
   Python versions. Fixed all (LICENSE, MANIFEST.in, SPDX `license = "MIT"` +
   `setuptools>=77`, classifiers 3.9-3.13, `git rm` of the wheel). Not fixed: no
   `project.urls` - the public repo address is not known to me and I will not
   invent one (decision for the owner).
3. **Supply-chain reviewer.** `pip list` in the clean venv shows only `idemkey` and
   pip: **zero runtime dependencies**. Grep of both artifacts for home paths,
   `/tmp`, `sk_live`, `AKIA`, `password` -> nothing. No publish workflow exists
   (good). Weak points left as they are: CI actions pinned by tag (`@v4`, `@v5`),
   not by commit hash; `setuptools>=77` is the only build requirement and is
   unpinned above.
4. **Hostile-input fuzzer.** 26 odd arguments through `guard()` (NaN, inf, lone
   surrogate, NUL, 5 MB string, 5000-deep list, `sNaN`, a module, a lock,
   `range(10**12)`, objects with a raising `__repr__` / `__getattr__`) and the CLI
   with a corrupt file, an empty file, a folder, a 100,000-character key,
   non-ASCII keys on `PYTHONIOENCODING=ascii`. Found:
   - a **keyless** call with an argument whose `repr()` raises was blocked by the
     fingerprint, contradicting the README rule "no key -> runs exactly once";
   - `idemkey stats` on a corrupt file or a folder printed a **Python traceback**;
   - `idemkey log` on an ASCII/cp1252 console with a key containing `e-acute` or
     CJK text died with `UnicodeEncodeError`;
   - error messages echoed a 100,000-character key in full.
   All fixed, each with a test in `tests/test_round4.py`.
5. **Performance / concurrency engineer.** Two full-suite runs hung at test 64.
   Repro: `/tmp/.../forkrepro.py` (guarded call, `os.fork()`, guarded call in the
   child) hung in `DedupStore._claim` in about half of the runs: the child
   inherited the store's lock while the parent's heartbeat thread held it. This
   falsified the README's "fork-safe" line. Fix: every store gets fresh locks in
   the child (`os.register_at_fork`). After: 25/25 runs exit 0. The regression
   test forks while another thread holds the lock deterministically and fails on
   the old code. (`DeprecationWarning: fork() in a multi-threaded process` from
   Python 3.12+ remains; it is a warning about the test, not the library.)
6. **Docs / DX writer.** README said 0.3.0 "is not on PyPI yet" (true) but not what
   *is* there; the CLI exit codes and the stdout/stderr split were undocumented.
   Added the install check, upgrade note for 0.1.1 users (namespaced keys), exit
   code table, `--json` where missing.
7. **Windows reviewer (static, NOT run on Windows).** Read every `os.`/`open`/
   `signal`/`subprocess` use (`grep -n "os.replace\|shell=True\|signal\." idemkey`
   -> none in the library). Findings: cp1252 console crash (fixed); `export` would
   write CRLF (fixed: LF); `os.makedirs(mode=0o700)` and `chmod 0600` do nothing
   on Windows (documented in README limits); `doctor` already skips the mode check
   off POSIX; two tests asserted POSIX modes without a skip (added
   `skipif win32`); SIGKILL/fork tests were already skipped; `bench/bench.py`
   uses `fcntl` (POSIX only, documented). Unknown: whether SQLite WAL, the
   heartbeat thread and temp-dir clean-up behave on Windows - nobody ran it.
8. **Competitor's maintainer.** Re-read PyPI JSON on 2026-11-07
   (`curl https://pypi.org/pypi/<name>/json`): latch-idempotent 0.4.1,
   agent-ledger 0.2.1, idempotency-kit 0.5.0, idempotency 0.1.0 - **same versions
   as the README comparison table**, so no row changed. Their feature cells were
   not re-read this round (only versions and summaries).

## Packaging proof (offline, scratch dir only)

- `python -m build` -> `idemkey-0.3.0.tar.gz` + `idemkey-0.3.0-py3-none-any.whl`, no warnings;
  `twine check` PASSED both.
- Wheel contents: 10 library files (incl. `py.typed`), `licenses/LICENSE`,
  METADATA, entry_points (`idemkey = idemkey.cli:main`). sdist: LICENSE, CHANGELOG,
  README, tests, bench; no `.github`, no `*.db`, no caches.
- Installed the wheel (not editable) into fresh venvs for Python 3.9.25, 3.10.20,
  3.11, 3.12, 3.13 and ran, from outside the source tree: `idemkey --version`,
  `idemkey demo`, `python -I -c "import idemkey"` (imports from site-packages),
  and the whole test suite (results below).
- Metadata consistency: version 0.3.0 in `pyproject.toml`, `idemkey.__version__`,
  CHANGELOG heading (checked by a test); author "Calas Automations Inc." in
  pyproject, LICENSE; `python_requires >=3.9` matches the classifiers and the
  tested versions.
- Not verified: installing from the sdist with a *different* build front-end;
  MCP SDK integration on these Python versions (the 3 skipped tests need `mcp` (2) or `pydantic` (1); CI has a job that forces them).

## Results after the changes

| | before | after |
|---|---|---|
| tests (repo checkout, Python 3.13) | 217 passed | **235 passed** (18 new in `tests/test_round4.py`) |
| tests (built wheel, run outside the source tree) | not run | 3.9.25: 232 passed / 3.10.20: 232 / 3.11.18: 232 / 3.12.3: 232 / 3.13.18: 232 - each with 3 skipped (no `pydantic` / `mcp` in those venvs) |
| `ruff check idemkey tests` | clean | clean |
| `twine check` | passed (but no LICENSE inside) | passed, LICENSE inside, no build warnings |
| README commands run from the fresh wheel (in a folder with a space in its name) | `--version` failed | demo, `--version`, `demo`, `log`, `log --json`, `export`, `doctor`, `show`, `stats`, `purge`, `forget` all work |
| fork repro (child hung) | hung in 3 of 6 runs | 0 of 25 |

Benchmark (`bench/bench.py all`, idemkey rows; raw: `bench/results-round4.json`,
before-run kept out of the repo). Correctness columns are identical before and
after: duplicate effects 33% default/fast and 0% reconcile/inject/atomic, 0 keys
with a duplicate in the 1000-racer test, lost-response-in-call 60/60 plain and
0/60 with `reconcile=` / `inject_key=`, semantic merges 9/11 (11/11 with
`ignore=`/`normalize=`), 0 wrong. Hostile-argument probes: 300k-int list
0.22 s -> 0.15 s, 20 MB string 0.35 s -> 0.13 s (both are load noise; that code
did not change). Speed, new keys/s default: 206 -> 563, fast: 3716 -> 4165,
replays/s default 11194 -> 18672. **These speed deltas are not a speed-up:** the
"before" run happened at load average ~17 and the "after" run at ~2-6 on a shared
VM, and the hot path only gained one `if`. The committed `bench/results.json`
(round 3, with competitor rows) is unchanged.


## Scored ideas (value / differentiation / effort / risk, 1-5; DONE or NOT DONE)

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | Fix fork deadlock (fresh locks in child) | 5 | 1 | 2 | 1 | DONE |
| 2 | Ship LICENSE in sdist + wheel | 5 | 1 | 1 | 1 | DONE |
| 3 | SPDX license expression, drop deprecated table | 4 | 1 | 1 | 2 | DONE |
| 4 | Keyless calls never fingerprinted (README rule holds) | 5 | 2 | 2 | 2 | DONE |
| 5 | Plain `ValueError` for unfingerprintable argument | 3 | 1 | 1 | 1 | DONE |
| 6 | No tracebacks from the CLI for store problems | 5 | 1 | 2 | 1 | DONE |
| 7 | Console-encoding-proof CLI output | 4 | 1 | 1 | 1 | DONE |
| 8 | `idemkey --version` | 4 | 1 | 1 | 1 | DONE |
| 9 | `idemkey demo` install self-check | 4 | 3 | 2 | 1 | DONE |
| 10 | `stats --json`, `doctor --json` | 3 | 2 | 1 | 1 | DONE |
| 11 | `purge --dry-run` | 3 | 2 | 1 | 1 | DONE |
| 12 | Document exit codes + stdout/stderr rule | 3 | 1 | 1 | 1 | DONE |
| 13 | Windows: LF export, POSIX-only tests skipped, README caveat | 3 | 1 | 1 | 1 | DONE (unrun) |
| 14 | Remove stray foreign wheel from repo | 2 | 1 | 1 | 1 | DONE |
| 15 | De-flake timing test | 3 | 1 | 1 | 1 | DONE |
| 16 | Python 3.9-3.13 classifiers backed by a real run | 3 | 1 | 1 | 1 | DONE |
| 17 | `project.urls` (repo, issues, changelog) | 4 | 1 | 1 | 1 | NOT DONE - address unknown, owner to give it |
| 18 | Trusted-publisher (OIDC) release workflow with tag check | 4 | 2 | 3 | 3 | NOT DONE - needs PyPI-side setup by the owner |
| 19 | Pin GitHub Actions to commit hashes | 3 | 1 | 1 | 2 | NOT DONE - cannot look up hashes offline |
| 20 | Shared store across hosts (Postgres/Redis backend) | 5 | 4 | 5 | 4 | NOT DONE - breaks "zero dependencies"; separate package idea |
| 21 | Real Windows/macOS CI run and fix | 5 | 1 | 2 | 2 | NOT DONE - needs a runner; CI is configured |
| 22 | `idemkey watch` / Prometheus text export of stats | 2 | 3 | 3 | 1 | NOT DONE |
| 23 | pytest plugin fixture around `check_tool` | 3 | 3 | 2 | 1 | NOT DONE |
| 24 | Store encryption at rest | 3 | 3 | 4 | 4 | NOT DONE - key management is the hard part |
| 25 | Built-in OpenTelemetry events | 3 | 2 | 3 | 2 | NOT DONE - `on_event` already allows it |
| 26 | Mypy/pyright in CI | 3 | 1 | 2 | 1 | NOT DONE - package ships `py.typed` but no checker has been run |
| 27 | Human approval gates (agent-ledger feature) | 2 | 2 | 5 | 3 | NOT DONE - out of scope on purpose |

Competitor check (read-only, 2026-11-07): the strongest are latch-idempotent
(reliability middleware: idempotency, circuit breaking, timeouts, budgets) and
idempotency-kit (async, Redis). Both need Redis or are broader; idemkey's
differentiator stays "zero dependencies + crash-safe local store + the three ways
of closing the crash-after-write window". Their feature cells were not re-read.

## What could not be tested

- Windows and macOS (nothing ran there; CI matrix exists but was not run).
- Read-only filesystem as a *mount* (only a read-only folder and a read-only file,
  as an unprivileged user); NFS/network drives.
- The MCP SDK paths on Python 3.9-3.12 (SDK not installed in these venvs; the
  existing CI job covers it).
- Installing from the sdist via a build front-end other than `build`.
- Anything on PyPI itself: no upload, no tag, no push was done.
- Benchmark speed columns are not comparable between runs on this shared VM.
