"""The ``guard`` wrapper: idempotent execution with safe retry.

Semantics (exactly what the panel approved):

* An explicit key (per-call kwarg, decorator-level, or derived) is required for
  any retry or dedup. A call with **no key executes exactly once, with no
  retry and no store interaction** — fail-loud instead of double-execute.
* Retries happen only on *exceptions* the caller classified as retryable
  (default: ``TimeoutError`` + :class:`RetryableError`). A returned result is
  never retried: after a result comes back you cannot know whether the write
  landed, so the write is verified (``verify`` hook) and committed instead.
* Same key + different args -> :class:`FingerprintMismatchError` by default.
  This is the "two genuinely different calls sharing a key" bug, and it must
  be loud: silently replaying call A's result for call B is worse than crashing.
* Concurrent same-key races: the loser waits for the winner (bounded) and
  replays the winner's stored result. Stale claims (dead owner) are reclaimed
  via heartbeat TTL, and every store write is fenced by a per-execution owner
  token so a stalled owner can never overwrite its successor's outcome.
* ``async def`` tools are supported natively: the same semantics, with store
  I/O moved off the event loop. A cancelled call whose write may have landed
  is recorded as ``unknown`` and is never silently re-executed.
* Keys are namespaced by ``(scope, tool, key_prefix)`` in the store, so the
  same model-chosen key on two tools (or two clients, with ``key_prefix``)
  can never collide.

Honest framing: this is **at-least-once execution with exactly-once effect**
as observed through the store. Nothing can make a network call truly
exactly-once; the store is what turns the second execution into a replay.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import random
import socket
import sys
import threading
import time
import typing
import uuid
import warnings

from .keys import derive_key, fingerprint
from .store import DedupStore

DEFAULT_STORE_ENV = "IDEMKEY_STORE"
DEFAULT_STORE_PATH = os.path.join(os.path.expanduser("~"), ".idemkey", "store.db")

_default_store = None


def get_default_store(path=None):
    """Process-wide default store (``IDEMKEY_STORE`` env or ``~/.idemkey/store.db``)."""
    global _default_store
    if path is not None:
        return DedupStore(path)
    if _default_store is None:
        _default_store = DedupStore(os.environ.get(DEFAULT_STORE_ENV, DEFAULT_STORE_PATH))
    return _default_store


# -- errors ---------------------------------------------------------------

class IdemKeyError(Exception):
    """Base for all idemkey errors."""


class RetryableError(IdemKeyError):
    """Raise (or subclass) to mark an exception as safe to retry.

    Retried automatically by default. Anything else propagates immediately
    with no retry and no second execution.
    """


class FingerprintMismatchError(IdemKeyError):
    """Same key reused with different call arguments.

    Either two different logical operations are sharing a key (bug), or the
    agent mutated arguments between retries (bug). Fix the key lifecycle;
    do not catch-and-ignore this.
    """


class VerificationError(IdemKeyError):
    """The ``verify`` hook reported the write did not land (or raised).

    Note: the write *may* have landed anyway (verify ran after it). Treat as
    "unknown outcome, investigated", not "definitely failed".
    """


class ResultNotReplayableError(IdemKeyError):
    """The original call completed but its result could not be stored
    (unserializable or over the size cap), so a duplicate cannot be replayed."""


class PreviousAttemptFailedError(IdemKeyError):
    """A concurrent execution of the same key failed; its error is attached."""


class InflightTimeoutError(IdemKeyError, TimeoutError):
    """Gave up waiting for a concurrent same-key execution to settle."""


class InvalidKeyError(IdemKeyError, ValueError):
    """The supplied idempotency key is not a usable key.

    Keys must be non-blank ``str`` values of at most :data:`MAX_KEY_LENGTH`
    characters. An empty string used to fall through to "no key" and silently
    disable dedup — exactly the bug an agent hits when a template variable is
    unset — so it is now rejected loudly.
    """


class MissingKeyError(IdemKeyError):
    """``require_key=True`` and the call carried no idempotency key."""


class OutcomeUnknownError(IdemKeyError):
    """A previous execution under this key was interrupted mid-call (e.g. the
    asyncio task was cancelled by a client timeout), so its write may or may
    not have landed. idemkey refuses to guess: check the downstream system,
    then ``idemkey forget KEY`` to allow a re-run (or set ``ttl``)."""


class ClaimLostWarning(UserWarning):
    """This execution's claim was taken over before it could record its result.

    It happens when an execution outlives ``claim_ttl`` without heartbeating
    (e.g. the process was suspended). Another owner may have executed the same
    operation; the store keeps *their* outcome. Investigate the downstream
    system for a duplicate effect and raise ``claim_ttl``.
    """


MAX_KEY_LENGTH = 255  # Stripe's limit; keeps keys sane for logs and the CLI
NS_SEP = "|"  # separates scope | tool | prefix | key in the stored key

# C0/C1 control characters and Unicode bidi overrides: a model-chosen key
# containing these could rewrite an operator's terminal (``idemkey log``) or
# visually spoof another key.
_BIDI = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")


def _bad_char(text):
    for ch in text:
        o = ord(ch)
        if o < 32 or 127 <= o < 160 or ch in _BIDI:
            return ch
    return None


# -- the wrapper ----------------------------------------------------------

def _canonical_call(fn, args, kwargs):
    """Normalise (args, kwargs) through the function signature.

    ``f(1)`` and ``f(1, b=2)`` (where ``b`` defaults to 2) are the same logical
    call and must share a fingerprint — otherwise a retry that merely spells
    the arguments differently trips a false FingerprintMismatchError.
    Falls back to the raw args when no signature is available.
    """
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return tuple(args), dict(kwargs)
    bound.apply_defaults()
    return bound.args, bound.kwargs


def _backoff_delay(attempt, base, cap):
    # Full jitter: uniform(0, min(cap, base * 2**attempt))
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def _encode_result(result, max_bytes):
    """Best-effort JSON encoding. Returns (payload_or_None, replayable_bool)."""
    try:
        payload = json.dumps(result, default=repr)
    except (TypeError, ValueError, RecursionError):
        return None, False
    if len(payload.encode("utf-8")) > max_bytes:
        return None, False
    return payload, True


def _validate_key(key, key_arg):
    if not isinstance(key, str):
        raise InvalidKeyError(
            f"idempotency key ({key_arg}=) must be a str, got {type(key).__name__}"
        )
    if not key.strip():
        raise InvalidKeyError(
            f"idempotency key ({key_arg}=) is empty/blank — that would silently "
            f"disable dedup. Pass a real key, or omit the argument entirely."
        )
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidKeyError(
            f"idempotency key ({key_arg}=) is {len(key)} chars; max is {MAX_KEY_LENGTH}"
        )
    bad = _bad_char(key)
    if bad is not None:
        raise InvalidKeyError(
            f"idempotency key ({key_arg}=) contains control character {bad!r}; "
            f"keys must be printable text"
        )
    return key


def _validate_ns_part(value, what):
    if not isinstance(value, str):
        raise InvalidKeyError(f"{what} must be a str, got {type(value).__name__}")
    if NS_SEP in value or _bad_char(value) is not None:
        raise InvalidKeyError(
            f"{what} {value!r} must not contain {NS_SEP!r} or control characters"
        )
    return value


def namespaced_key(key, tool, scope="default", prefix=""):
    """The key as stored: ``scope|tool|prefix|key``.

    ``scope``, ``tool`` and ``prefix`` never contain ``|`` (enforced), so the
    split is unambiguous even though the model-chosen ``key`` may.
    """
    return NS_SEP.join((scope, tool, prefix, key))


def _new_owner():
    # Unique per execution, not per process: two threads (or two asyncio
    # tasks) in one process must not be able to complete each other's claim.
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _resolved_signature(fn):
    """``inspect.signature(fn)`` with string annotations evaluated.

    Under ``from __future__ import annotations`` every annotation is a string.
    Once we set ``__signature__`` on the wrapper, frameworks resolve those
    strings against the *wrapper's* globals (this module), where names like a
    user's pydantic ``Order`` model don't exist — registration then fails. So
    resolve them here, against the tool's own module. Names that cannot be
    resolved (e.g. ``TYPE_CHECKING``-only imports) are left as strings.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    if sys.version_info >= (3, 10):
        try:
            return inspect.signature(fn, eval_str=True)
        except Exception:  # noqa: BLE001, S110 — fall through to partial resolution
            pass
    target = fn if (inspect.isfunction(fn) or inspect.ismethod(fn)) else getattr(fn, "__call__", fn)  # noqa: B004
    try:
        hints = typing.get_type_hints(target)
    except Exception:  # noqa: BLE001
        hints = {}
    if not hints:
        return sig
    params = [
        p.replace(annotation=hints[p.name]) if p.name in hints else p
        for p in sig.parameters.values()
    ]
    return sig.replace(
        parameters=params,
        return_annotation=hints.get("return", sig.return_annotation),
    )


def _with_key_param(fn, key_arg, required=False):
    """Signature of ``fn`` plus a keyword-only ``key_arg: Optional[str] = None``.

    Frameworks that build a tool schema from the signature (MCP / FastMCP,
    LangChain, pydantic-validated callers) drop arguments the signature does
    not declare — so without this, the agent's ``idemkey`` never reaches the
    wrapper and dedup silently never happens.
    """
    sig = _resolved_signature(fn)
    if sig is None:
        return None
    params = list(sig.parameters.values())
    if key_arg in sig.parameters or not key_arg.isidentifier():
        return sig
    if required:
        # No default: schema generators (MCP, pydantic, LangChain) mark it
        # required, so the model is told up front it must send a key.
        new = inspect.Parameter(key_arg, inspect.Parameter.KEYWORD_ONLY, annotation=str)
    else:
        new = inspect.Parameter(
            key_arg, inspect.Parameter.KEYWORD_ONLY, default=None,
            annotation=typing.Optional[str],
        )
    idx = len(params)
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        idx -= 1
    params.insert(idx, new)
    try:
        return sig.replace(parameters=params)
    except ValueError:
        return sig


class _Guarded:
    def __init__(self, fn, **opts):
        self._fn = fn
        self._opts = opts
        functools.update_wrapper(self, fn)
        sig = _with_key_param(fn, opts["key_arg"], required=opts["require_key"])
        if sig is not None:
            self.__signature__ = sig

    # -- public ---------------------------------------------------------
    @property
    def idemkey_options(self):
        return dict(self._opts)

    def store_key(self, key):
        """The key exactly as this wrapper stores it (namespace applied)."""
        _validate_key(key, self._opts["key_arg"])
        if not self._opts["namespace"]:
            return key
        prefix = self._opts["key_prefix"]
        if callable(prefix):
            prefix = prefix()
        tool = self._opts["tool"] or getattr(self._fn, "__name__", "unknown")
        return namespaced_key(key, tool, self._opts["scope"], prefix or "")

    def _prepare(self, args, kwargs):
        """Resolve (key, tool, fingerprint, call_kwargs). Shared sync/async."""
        o = self._opts
        key_arg = o["key_arg"]
        # key_arg is popped BEFORE fingerprinting: the key identifies the
        # logical operation, it is not part of the call args.
        call_kwargs = dict(kwargs)
        key = call_kwargs.pop(key_arg, None)
        if key is None:
            key = o["key"]
        if key is not None:
            _validate_key(key, key_arg)
        tool = o["tool"] or getattr(self._fn, "__name__", "unknown")
        # Canonicalise BEFORE fingerprinting / deriving (see _canonical_call).
        cargs, ckwargs = _canonical_call(self._fn, args, call_kwargs)
        fp = fingerprint(tool, cargs, ckwargs)
        if key is None and o["derive"]:
            key = derive_key(tool, cargs, ckwargs, scope=o["scope"])
        if key is None and o["require_key"]:
            raise MissingKeyError(
                f"{tool}() requires an idempotency key: pass {key_arg}=<stable "
                f"key per logical operation>. Refusing to run a keyless write."
            )
        if key is not None and o["namespace"]:
            prefix = o["key_prefix"]
            if callable(prefix):
                prefix = prefix()
            prefix = _validate_ns_part("" if prefix is None else prefix, "key_prefix")
            key = namespaced_key(
                key, _validate_ns_part(tool, "tool name"), o["scope"], prefix
            )
        return key, tool, fp, call_kwargs

    def __call__(self, *args, **kwargs):
        o = self._opts
        key, tool, fp, call_kwargs = self._prepare(args, kwargs)

        # No key -> single attempt, no store, no retry. Fail loud.
        if key is None:
            return self._fn(*args, **call_kwargs)

        store = o["store"] or get_default_store()
        owner = _new_owner()
        outcome, row = store.claim(key, tool, fp, o["scope"], owner, o["claim_ttl"], o["ttl"])

        if outcome == "duplicate":
            return self._handle_duplicate(store, key, row, fp, owner, o, args, call_kwargs)
        if outcome == "inflight":
            if row is not None:
                # A different call wearing an in-flight (or stale) key: reject
                # now rather than wait for — or take over — someone else's op.
                self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            return self._handle_inflight(store, key, fp, owner, o)
        # "claimed": we own it
        return self._execute(store, key, owner, o, args, call_kwargs)

    # -- duplicate / inflight paths --------------------------------------
    def _check_fingerprint(self, key, row, fp, policy):
        if row["fingerprint"] == fp:
            return
        msg = (
            f"idempotency key {key!r} was first used for a different call "
            f"(tool={row['tool']!r}, scope={row['scope']!r}). Refusing to replay "
            f"that result for these arguments."
        )
        if policy == "error":
            raise FingerprintMismatchError(msg)
        if policy == "warn":
            warnings.warn(msg, stacklevel=4)
        elif policy != "replay":
            raise IdemKeyError(f"unknown fingerprint_policy {policy!r}")

    def _replay(self, key, row):
        if not row["replayable"] or row["result_json"] is None:
            raise ResultNotReplayableError(
                f"key {key!r} completed but its result was not storable; "
                f"cannot replay for this duplicate call."
            )
        return json.loads(row["result_json"])

    def _unknown_error(self, key, row):
        return OutcomeUnknownError(
            f"idempotency key {key!r}: a previous execution was interrupted "
            f"mid-call ({row['error']}); its write may have landed. Check the "
            f"downstream system, then run `idemkey forget` on this key to allow "
            f"a re-run."
        )

    def _handle_duplicate(self, store, key, row, fp, owner, o, args, kwargs):
        if row["status"] == "completed":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            return self._replay(key, row)
        if row["status"] == "unknown":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            raise self._unknown_error(key, row)
        # status == "failed": a retry with the same key re-executes. The
        # fingerprint check guards against arg drift on the retry path too:
        # if the agent changed the args, that is not a retry, it is a new
        # operation wearing an old key.
        self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
        if store.reclaim_failed(key, owner):
            return self._execute(store, key, owner, o, args, kwargs)
        # Lost the reclaim race: whoever won will settle the row; wait for them.
        return self._handle_inflight(store, key, fp, owner, o)

    def _settled(self, key, row, fp, o):
        if row["status"] == "completed":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            return self._replay(key, row)
        if row["status"] == "unknown":
            raise self._unknown_error(key, row)
        raise PreviousAttemptFailedError(
            f"concurrent execution of key {key!r} failed: {row['error']}"
        )

    def _inflight_error(self, key, o, exc):
        if isinstance(exc, KeyError):
            # The row vanished mid-wait (e.g. purged). Nothing to replay and
            # nothing to wait for: loud, no silent re-execution.
            return IdemKeyError(
                f"idempotency key {key!r} vanished while waiting for the "
                f"in-flight execution to settle."
            )
        return InflightTimeoutError(
            f"key {key!r} stayed pending for {o['inflight_timeout']}s; "
            f"the owner may be wedged. Investigate before retrying."
        )

    def _handle_inflight(self, store, key, fp, owner, o):
        try:
            row = store.wait_until_settled(key, o["inflight_timeout"])
        except (TimeoutError, KeyError) as exc:
            raise self._inflight_error(key, o, exc) from exc
        return self._settled(key, row, fp, o)

    # -- owned execution --------------------------------------------------
    def _start_heartbeat(self, store, key, owner, o):
        # Heartbeat while we hold the claim so a long-running call is never
        # mistaken for a dead owner and stolen mid-execution (which would
        # double-execute the write). Daemon thread; stops on every exit path.
        # A thread (not an asyncio task) so it keeps beating even if an async
        # tool blocks the event loop.
        stop = threading.Event()
        interval = min(max(o["claim_ttl"], 0.01) / 3.0, 60.0)

        def _beat():
            while not stop.wait(interval):
                try:
                    store.heartbeat(key, owner)
                except Exception:  # noqa: BLE001, S110
                    # Heartbeat failure must never kill the main path; the
                    # execution result (or its error) is the source of truth.
                    pass

        threading.Thread(target=_beat, daemon=True).start()
        return stop

    def _execute(self, store, key, owner, o, args, kwargs):
        stop = self._start_heartbeat(store, key, owner, o)
        try:
            return self._run_attempts(store, key, owner, o, args, kwargs)
        finally:
            stop.set()

    @staticmethod
    def _fail(store, key, owner, error):
        store.fail(key, error, owner=owner)

    @staticmethod
    def _commit(store, key, owner, o, result):
        payload, replayable = _encode_result(result, o["result_max_bytes"])
        if not store.complete(key, payload, replayable, owner=owner):
            warnings.warn(
                f"idempotency key {key!r}: this execution's claim was taken over "
                f"before its result could be recorded (it outlived claim_ttl="
                f"{o['claim_ttl']}s without a heartbeat). Another owner may have "
                f"executed the same operation — check for a duplicate effect.",
                ClaimLostWarning,
                stacklevel=4,
            )

    def _verify_failed(self, store, key, owner, exc=None):
        if exc is not None:
            self._fail(store, key, owner, f"verify raised {type(exc).__name__}: {exc}")
            return VerificationError(
                f"verify hook raised for key {key!r}: {type(exc).__name__}: {exc}"
            )
        self._fail(store, key, owner, "verify hook returned False (write not confirmed)")
        return VerificationError(
            f"verify hook reported the write did not land for key {key!r}. "
            f"The write may still have landed; outcome is unknown."
        )

    def _run_attempts(self, store, key, owner, o, args, kwargs):
        last_exc = None
        for attempt in range(o["max_retries"] + 1):
            try:
                result = self._fn(*args, **kwargs)
            except o["retry_on"] as exc:
                last_exc = exc
                store.heartbeat(key, owner)
                if attempt < o["max_retries"]:
                    o["sleep"](_backoff_delay(attempt, o["backoff_base"], o["backoff_cap"]))
                    continue
                break
            except Exception as exc:  # non-retryable: no second execution, ever
                self._fail(store, key, owner, f"{type(exc).__name__}: {exc}")
                raise
            # Verify the write landed before reporting success.
            if o["verify"] is not None:
                try:
                    ok = o["verify"](result)
                except Exception as exc:
                    raise self._verify_failed(store, key, owner, exc) from exc
                if not ok:
                    raise self._verify_failed(store, key, owner)
            self._commit(store, key, owner, o, result)
            return result
        self._fail(store, key, owner, f"{type(last_exc).__name__}: {last_exc}")
        raise last_exc


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _to_thread(func, *args):
    # asyncio.to_thread is 3.9+; run_in_executor keeps the same behaviour.
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, functools.partial(func, *args))


class _AsyncGuarded(_Guarded):
    """``guard`` for ``async def`` tools. Same semantics as the sync wrapper.

    Store calls (SQLite, may wait on a lock up to ``busy_timeout``) run in the
    default executor so the event loop is never blocked; in-flight waits poll
    with ``asyncio.sleep``.

    Cancellation (e.g. an MCP client timeout cancelling the handler):

    * before the tool started -> the claim is released (``failed``), since
      nothing ran; a retry executes normally;
    * while the tool / verify was running -> ``unknown``: the write may have
      landed, so a same-key retry raises :class:`OutcomeUnknownError` instead
      of executing a second time;
    * while the result was being committed -> the commit is shielded and
      finishes, so the retry replays the real result.
    """

    async def __call__(self, *args, **kwargs):
        o = self._opts
        key, tool, fp, call_kwargs = self._prepare(args, kwargs)
        if key is None:
            return await self._fn(*args, **call_kwargs)

        store = o["store"] or get_default_store()
        owner = _new_owner()
        outcome, row = await self._shielded_claim(
            store.claim, store, key, owner,
            key, tool, fp, o["scope"], owner, o["claim_ttl"], o["ttl"],
        )
        if outcome == "claimed":
            return await self._aexecute(store, key, owner, o, args, call_kwargs)
        if outcome == "duplicate":
            # Same rules as the sync path: replay completed, re-execute failed.
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            if row["status"] == "completed":
                return self._replay(key, row)
            if row["status"] == "unknown":
                raise self._unknown_error(key, row)
            won = await self._shielded_claim(store.reclaim_failed, store, key, owner, key, owner)
            if won:
                return await self._aexecute(store, key, owner, o, args, call_kwargs)
        elif row is not None:
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
        # In flight elsewhere (or we lost the reclaim race): wait and replay.
        return await self._ahandle_inflight(store, key, fp, o)

    @staticmethod
    async def _shielded_claim(func, store, key, owner, *args):
        """Run a claiming store call; if cancelled meanwhile, let it finish and
        release whatever it claimed (the tool never started, so ``failed`` —
        reclaimable — is the truthful status)."""
        fut = _to_thread(func, *args)
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            try:
                res = await fut
                claimed = res[0] == "claimed" if isinstance(res, tuple) else bool(res)
                if claimed:
                    store.fail(key, "CancelledError: cancelled before execution", owner=owner)
            except Exception:  # noqa: BLE001, S110
                pass
            raise

    async def _ahandle_inflight(self, store, key, fp, o, poll_interval=0.05):
        deadline = time.monotonic() + o["inflight_timeout"]
        while True:
            row = await _to_thread(store.get, key)
            if row is None:
                raise self._inflight_error(key, o, KeyError(key))
            if row["status"] != "pending":
                return self._settled(key, row, fp, o)
            if time.monotonic() >= deadline:
                exc = TimeoutError(key)
                raise self._inflight_error(key, o, exc) from exc
            await asyncio.sleep(poll_interval)

    async def _aexecute(self, store, key, owner, o, args, kwargs):
        stop = self._start_heartbeat(store, key, owner, o)
        try:
            return await self._arun_attempts(store, key, owner, o, args, kwargs)
        except asyncio.CancelledError:
            # Cancelled mid-flight: the write may or may not have landed.
            # Record ``unknown`` (fenced: a no-op if the row already settled)
            # so a same-key retry neither re-executes nor hangs on a dead
            # pending row. Never ``failed`` — that would re-run the write.
            try:
                store.mark_unknown(
                    key, "CancelledError: task cancelled mid-call; outcome unknown",
                    owner=owner,
                )
            except Exception:  # noqa: BLE001, S110
                pass
            raise
        finally:
            stop.set()

    async def _asleep(self, o, delay):
        if o["sleep"] is time.sleep:
            await asyncio.sleep(delay)
        else:
            await _maybe_await(o["sleep"](delay))

    async def _arun_attempts(self, store, key, owner, o, args, kwargs):
        last_exc = None
        for attempt in range(o["max_retries"] + 1):
            try:
                result = await self._fn(*args, **kwargs)
            except o["retry_on"] as exc:
                last_exc = exc
                await _to_thread(store.heartbeat, key, owner)
                if attempt < o["max_retries"]:
                    await self._asleep(o, _backoff_delay(attempt, o["backoff_base"], o["backoff_cap"]))
                    continue
                break
            except Exception as exc:  # non-retryable
                await _to_thread(self._fail, store, key, owner, f"{type(exc).__name__}: {exc}")
                raise
            if o["verify"] is not None:
                try:
                    ok = await _maybe_await(o["verify"](result))
                except Exception as exc:
                    raise await _to_thread(self._verify_failed, store, key, owner, exc) from exc
                if not ok:
                    raise await _to_thread(self._verify_failed, store, key, owner)
            commit = _to_thread(self._commit, store, key, owner, o, result)
            try:
                await asyncio.shield(commit)
            except asyncio.CancelledError:
                # The tool already succeeded: finish recording it, then honour
                # the cancellation. The fenced mark_unknown in _aexecute is a
                # no-op on the now-completed row.
                await commit
                raise
            return result
        await _to_thread(self._fail, store, key, owner, f"{type(last_exc).__name__}: {last_exc}")
        raise last_exc


def _is_async(fn):
    if inspect.iscoroutinefunction(fn):
        return True
    # Callable instances whose __call__ is ``async def``.
    call = None if inspect.isfunction(fn) else inspect.getattr_static(type(fn), "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


def _make_wrapper(fn, opts):
    if not _is_async(fn):
        return _Guarded(fn, **opts)
    core = _AsyncGuarded(fn, **opts)

    # A real ``async def`` (not a callable instance) so frameworks that test
    # ``inspect.iscoroutinefunction`` — FastMCP / MCPServer included — await it.
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await core(*args, **kwargs)

    if hasattr(core, "__signature__"):
        wrapper.__signature__ = core.__signature__
    wrapper.idemkey_options = core.idemkey_options
    wrapper.store_key = core.store_key
    return wrapper


_DEFAULTS = {
    "key": None,
    "key_arg": "idemkey",
    "tool": None,
    "derive": False,
    "require_key": False,
    "namespace": True,
    "key_prefix": None,
    "scope": "default",
    "store": None,
    "max_retries": 3,
    "backoff_base": 0.5,
    "backoff_cap": 30.0,
    "retry_on": (TimeoutError, RetryableError),
    "verify": None,
    "fingerprint_policy": "error",  # error | warn | replay
    "claim_ttl": 300.0,
    "inflight_timeout": 60.0,
    "ttl": None,
    "result_max_bytes": 1_000_000,
    "sleep": time.sleep,
}


def guard(_fn=None, **opts):
    """Wrap ``fn`` with idempotency-key dedup + safe retry.

    Usable as ``guard(fn, ...)``, ``@guard(...)``, or bare ``@guard``. Works
    on plain functions and on ``async def`` coroutine functions.

    Key options: ``key`` (fixed key for every call — rarely what you want),
    ``key_arg`` (kwarg name the agent passes the key through, default
    ``"idemkey"``), ``derive=True`` (deterministic key from scope+tool+args;
    opt-in, only for by-construction-idempotent operations),
    ``require_key=True`` (refuse keyless calls with :class:`MissingKeyError`).

    Namespacing: keys are stored as ``scope|tool|prefix|key`` so the same key
    on two tools never collides. ``key_prefix`` (a str, or a zero-arg callable
    evaluated per call — e.g. reading the MCP session / tenant from a
    ``contextvars.ContextVar``) isolates clients sharing one store.
    ``namespace=False`` stores raw keys (0.1.x layout; shares keys across
    tools — only for reading a store written by 0.1.x).

    Retry options: ``max_retries`` (default 3), ``backoff_base``/``backoff_cap``
    seconds with full jitter, ``retry_on`` (exception tuple; default
    ``(TimeoutError, RetryableError)``), ``sleep`` (injectable for tests).

    Correctness options: ``verify`` (callable ``(result) -> bool`` confirming
    the write landed; may be async for async tools), ``fingerprint_policy``
    (``"error"`` default), ``store`` (a :class:`DedupStore`; defaults to
    ``~/.idemkey/store.db``), ``claim_ttl``, ``inflight_timeout``,
    ``ttl`` (seconds after which a settled key expires and re-executes;
    default ``None`` = never), ``result_max_bytes``.
    """
    unknown = set(opts) - set(_DEFAULTS)
    if unknown:
        raise TypeError(f"guard() got unknown option(s): {sorted(unknown)}")
    merged = {**_DEFAULTS, **opts}
    # Normalise retry_on to a tuple of exception classes.
    ro = merged["retry_on"]
    if isinstance(ro, type) and issubclass(ro, BaseException):
        ro = (ro,)
    merged["retry_on"] = tuple(ro)
    for e in merged["retry_on"]:
        if not (isinstance(e, type) and issubclass(e, BaseException)):
            raise TypeError(
                f"guard() retry_on must be exception classes, got {e!r} — "
                "a non-exception here would explode at except-time instead of "
                "failing fast at wrap-time"
            )
    if merged["key"] is not None:
        _validate_key(merged["key"], "key")
    if not isinstance(merged["key_arg"], str) or not merged["key_arg"]:
        raise TypeError("guard() key_arg must be a non-empty str")
    _validate_ns_part(merged["scope"], "scope")
    if merged["tool"] is not None:
        _validate_ns_part(merged["tool"], "tool")
    kp = merged["key_prefix"]
    if kp is not None and not callable(kp):
        _validate_ns_part(kp, "key_prefix")
    if merged["ttl"] is not None and not merged["ttl"] > 0:
        raise ValueError("guard() ttl must be a positive number of seconds or None")

    def wrap(fn):
        return _make_wrapper(fn, merged)

    if _fn is not None:
        return wrap(_fn)
    return wrap


__all__ = ["guard"]
