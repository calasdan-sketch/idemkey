# Changelog

## 0.2.0 — unreleased

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

## 0.1.1
- Initial public release.
