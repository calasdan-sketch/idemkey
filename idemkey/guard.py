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
  via heartbeat TTL.

Honest framing: this is **at-least-once execution with exactly-once effect**
as observed through the store. Nothing can make a network call truly
exactly-once; the store is what turns the second execution into a replay.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import random
import socket
import threading
import time
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
    except (TypeError, ValueError):
        return None, False
    if len(payload.encode("utf-8")) > max_bytes:
        return None, False
    return payload, True


class _Guarded:
    def __init__(self, fn, **opts):
        self._fn = fn
        self._opts = opts
        functools.update_wrapper(self, fn)

    # -- public ---------------------------------------------------------
    @property
    def idemkey_options(self):
        return dict(self._opts)

    def __call__(self, *args, **kwargs):
        o = self._opts
        key_arg = o["key_arg"]

        # 1. Resolve the key. key_arg is popped BEFORE fingerprinting: the key
        #    identifies the logical operation, it is not part of the call args.
        call_kwargs = dict(kwargs)
        key = call_kwargs.pop(key_arg, None) or o["key"]
        tool = o["tool"] or getattr(self._fn, "__name__", "unknown")
        # Canonicalise BEFORE fingerprinting / deriving (see _canonical_call).
        cargs, ckwargs = _canonical_call(self._fn, args, call_kwargs)
        fp = fingerprint(tool, cargs, ckwargs)
        if key is None and o["derive"]:
            key = derive_key(tool, cargs, ckwargs, scope=o["scope"])

        # 2. No key -> single attempt, no store, no retry. Fail loud.
        if key is None:
            return self._fn(*args, **call_kwargs)

        store = o["store"] or get_default_store()
        owner = f"{socket.gethostname()}:{os.getpid()}"
        outcome, row = store.claim(key, tool, fp, o["scope"], owner, o["claim_ttl"])

        if outcome == "duplicate":
            return self._handle_duplicate(store, key, row, fp, owner, o, args, call_kwargs)
        if outcome == "inflight":
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

    def _handle_duplicate(self, store, key, row, fp, owner, o, args, kwargs):
        if row["status"] == "completed":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            return self._replay(key, row)
        # status == "failed": a retry with the same key re-executes. The
        # fingerprint check guards against arg drift on the retry path too:
        # if the agent changed the args, that is not a retry, it is a new
        # operation wearing an old key.
        self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
        if store.reclaim_failed(key, owner):
            return self._execute(store, key, owner, o, args, kwargs)
        # Lost the reclaim race: whoever won will settle the row; wait for them.
        return self._handle_inflight(store, key, fp, owner, o)

    def _handle_inflight(self, store, key, fp, owner, o):
        try:
            row = store.wait_until_settled(key, o["inflight_timeout"])
        except TimeoutError as exc:
            raise InflightTimeoutError(
                f"key {key!r} stayed pending for {o['inflight_timeout']}s; "
                f"the owner may be wedged. Investigate before retrying."
            ) from exc
        except KeyError as exc:
            # The row vanished mid-wait (e.g. purged). Nothing to replay and
            # nothing to wait for: loud, no silent re-execution.
            raise IdemKeyError(
                f"idempotency key {key!r} vanished while waiting for the "
                f"in-flight execution to settle."
            ) from exc
        if row["status"] == "completed":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            return self._replay(key, row)
        raise PreviousAttemptFailedError(
            f"concurrent execution of key {key!r} failed: {row['error']}"
        )

    # -- owned execution --------------------------------------------------
    def _execute(self, store, key, owner, o, args, kwargs):
        # Heartbeat while we hold the claim so a long-running call is never
        # mistaken for a dead owner and stolen mid-execution (which would
        # double-execute the write). Daemon thread; stops on every exit path.
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

        beater = threading.Thread(target=_beat, daemon=True)
        beater.start()
        try:
            return self._run_attempts(store, key, owner, o, args, kwargs)
        finally:
            stop.set()

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
                store.fail(key, f"{type(exc).__name__}: {exc}")
                raise
            # Verify the write landed before reporting success.
            if o["verify"] is not None:
                try:
                    ok = o["verify"](result)
                except Exception as exc:
                    store.fail(key, f"verify raised {type(exc).__name__}: {exc}")
                    raise VerificationError(
                        f"verify hook raised for key {key!r}: {type(exc).__name__}: {exc}"
                    ) from exc
                if not ok:
                    store.fail(key, "verify hook returned False (write not confirmed)")
                    raise VerificationError(
                        f"verify hook reported the write did not land for key {key!r}. "
                        f"The write may still have landed; outcome is unknown."
                    )
            payload, replayable = _encode_result(result, o["result_max_bytes"])
            store.complete(key, payload, replayable)
            return result
        store.fail(key, f"{type(last_exc).__name__}: {last_exc}")
        raise last_exc


_DEFAULTS = {
    "key": None,
    "key_arg": "idemkey",
    "tool": None,
    "derive": False,
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
    "result_max_bytes": 1_000_000,
    "sleep": time.sleep,
}


def guard(_fn=None, **opts):
    """Wrap ``fn`` with idempotency-key dedup + safe retry.

    Usable as ``guard(fn, ...)``, ``@guard(...)``, or bare ``@guard``.

    Key options: ``key`` (fixed key for every call — rarely what you want),
    ``key_arg`` (kwarg name the agent passes the key through, default
    ``"idemkey"``), ``derive=True`` (deterministic key from scope+tool+args;
    opt-in, only for by-construction-idempotent operations).

    Retry options: ``max_retries`` (default 3), ``backoff_base``/``backoff_cap``
    seconds with full jitter, ``retry_on`` (exception tuple; default
    ``(TimeoutError, RetryableError)``), ``sleep`` (injectable for tests).

    Correctness options: ``verify`` (callable ``(result) -> bool`` confirming
    the write landed), ``fingerprint_policy`` (``"error"`` default), ``store``
    (a :class:`DedupStore`; defaults to ``~/.idemkey/store.db``),
    ``claim_ttl``, ``inflight_timeout``, ``result_max_bytes``.
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

    def wrap(fn):
        return _Guarded(fn, **merged)

    if _fn is not None:
        return wrap(_fn)
    return wrap


__all__ = ["guard"]
