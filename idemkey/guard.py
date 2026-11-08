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
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import random
import socket
import sys
import threading
import time
import typing
import uuid
import warnings

from . import codec as _codec
from .keys import derive_key, fingerprint2
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

    ``retry_after`` (seconds, optional) is honoured as the wait before the
    next attempt, capped at ``backoff_cap`` - use it for a 429 / ``Retry-After``.
    Any exception carrying a numeric ``retry_after`` attribute is treated the same.
    """

    def __init__(self, *args, retry_after=None):
        super().__init__(*args)
        self.retry_after = retry_after


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


class ClaimLostError(IdemKeyError):
    """``atomic=`` mode: this execution lost its claim before commit. Its
    transaction (the tool's own writes included) was rolled back, so no effect
    from this execution exists; the new owner's outcome stands."""


class ReconcileError(IdemKeyError):
    """The ``reconcile`` hook raised, so idemkey cannot tell whether the earlier
    attempt's write landed. The key is marked ``unknown`` (fail closed)."""


class _NotFound:
    def __repr__(self):
        return "idemkey.NOT_FOUND"

    def __bool__(self):
        return False


#: Return this from a ``reconcile`` hook when the earlier attempt left no trace
#: downstream (safe to execute again).
NOT_FOUND = _NotFound()


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

def _canonical_call(sig, args, kwargs, ignore=(), normalize=None):
    """Normalise (args, kwargs) through the function signature.

    ``f(1)`` and ``f(1, b=2)`` (where ``b`` defaults to 2) are the same logical
    call and must share a fingerprint - otherwise a retry that merely spells
    the arguments differently trips a false FingerprintMismatchError.
    ``ignore`` drops volatile parameters (request ids, timestamps) and
    ``normalize`` maps ``{param: callable}`` over values before fingerprinting.
    Falls back to the raw args when no signature is available.
    """
    if sig is None:
        return tuple(args), dict(kwargs)
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return tuple(args), dict(kwargs)
    bound.apply_defaults()
    if ignore or normalize:
        named = dict(bound.arguments)
        for name in ignore:
            named.pop(name, None)
        for name, fn in (normalize or {}).items():
            if name in named:
                named[name] = fn(named[name])
        return (), named
    return bound.args, bound.kwargs


def _backoff_delay(attempt, base, cap, exc=None):
    # Full jitter: uniform(0, min(cap, base * 2**attempt)), unless the failure
    # told us how long to wait (Retry-After): then wait at least that, capped.
    hint = getattr(exc, "retry_after", None)
    if isinstance(hint, (int, float)) and not isinstance(hint, bool) and not math.isnan(hint) and hint >= 0:
        return min(float(hint), cap)
    return random.uniform(0, min(cap, base * (2 ** attempt)))


_log = logging.getLogger("idemkey")


def _emit(o, event, key, tool, **info):
    """Observability: ``on_event(dict)`` and the ``idemkey`` logger (DEBUG).

    A hook that raises can never break (or change the outcome of) a guarded
    call. The key is passed as stored; escape it before printing to a terminal.
    """
    cb = o.get("on_event")
    if cb is None and not _log.isEnabledFor(logging.DEBUG):
        return
    ev = {"event": event, "key": key, "tool": tool, **info}
    if _log.isEnabledFor(logging.DEBUG):
        _log.debug("idemkey %s tool=%s key=%a %s", event, tool, key, info)
    if cb is not None:
        try:
            cb(ev)
        except Exception:  # noqa: BLE001
            _log.debug("idemkey on_event hook raised", exc_info=True)


def _encode_result(result, max_bytes, typed=True):
    """Best-effort JSON encoding. Returns (payload_or_None, replayable_bool)."""
    try:
        payload = _codec.dumps(result) if typed else json.dumps(result, default=repr)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return None, False
    if len(payload.encode("utf-8")) > max_bytes:
        return None, False
    return payload, True


# Text a model writes into an optional string argument when it has nothing to say.
PLACEHOLDER_KEYS = frozenset({"null", "none", "nil", "undefined", "nan", "n/a", "na"})


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
    if key != key.strip():
        raise InvalidKeyError(
            f"idempotency key ({key_arg}=) has spaces or line breaks at the start or "
            f"end; 'order-1 ' and 'order-1' would be two different keys. Remove them."
        )
    if key.lower() in PLACEHOLDER_KEYS:
        raise InvalidKeyError(
            f"idempotency key ({key_arg}={key!r}) looks like an empty placeholder a model "
            f"fills in when it has no real key, and would make unrelated calls share one key. "
            f"Pass a real key, or omit the argument."
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


def downstream_key(stored_key):
    """Stable key to hand to a downstream API that supports idempotency keys
    (Stripe ``Idempotency-Key`` etc.): a hash of the stored key, so it is the
    same on every retry and on takeover by another process, and reveals
    nothing about scope/tool/prefix."""
    return "idk_" + hashlib.sha256(stored_key.encode("utf-8")).hexdigest()[:40]


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


def _with_key_param(fn, key_arg, required=False, hide=()):
    """Signature of ``fn`` plus a keyword-only ``key_arg: Optional[str] = None``.

    Frameworks that build a tool schema from the signature (MCP / FastMCP,
    LangChain, pydantic-validated callers) drop arguments the signature does
    not declare — so without this, the agent's ``idemkey`` never reaches the
    wrapper and dedup silently never happens.
    """
    sig = _resolved_signature(fn)
    if sig is None:
        return None
    params = [p for p in sig.parameters.values() if p.name not in hide]
    sig = sig.replace(parameters=params)
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


class _Stopper:
    """Handle returned by ``_start_heartbeat``; ``set()`` stops the beat."""

    def __init__(self, token):
        self._token = token

    def set(self):
        _HEARTBEATER.unregister(self._token)


class _Heartbeater:
    """One lazily started daemon thread that heartbeats all in-flight claims."""

    def __init__(self):
        self._cv = threading.Condition()
        self._entries = {}   # token -> [next_due, interval, store, key, owner]
        self._next = 0
        self._thread = None
        self._pid = None

    def register(self, store, key, owner, interval):
        with self._cv:
            self._next += 1
            token = self._next
            self._entries[token] = [time.monotonic() + interval, interval, store, key, owner]
            if self._thread is None or self._pid != os.getpid() or not self._thread.is_alive():
                self._pid = os.getpid()
                self._thread = threading.Thread(target=self._loop, name="idemkey-heartbeat", daemon=True)
                self._thread.start()
            self._cv.notify()
            return token

    def unregister(self, token):
        with self._cv:
            self._entries.pop(token, None)

    def _loop(self):
        while True:
            with self._cv:
                if os.getpid() != self._pid:
                    return  # forked child: a fresh thread is started on demand
                now = time.monotonic()
                due = [(t, e) for t, e in self._entries.items() if e[0] <= now]
                for _, e in due:
                    e[0] = now + e[1]
                if not due:
                    nxt = min((e[0] for e in self._entries.values()), default=None)
                    self._cv.wait(None if nxt is None else max(nxt - now, 0.001))
                    continue
            for _, e in due:
                try:
                    e[2].heartbeat(e[3], e[4])
                except Exception:  # noqa: BLE001, S110
                    # Heartbeat failure must never kill the main path; the
                    # execution result (or its error) is the source of truth.
                    pass


_HEARTBEATER = _Heartbeater()


def _reset_heartbeater_after_fork():
    # A forked child inherits a lock that may have been held by a thread that
    # no longer exists; start from a clean, empty heartbeater.
    global _HEARTBEATER
    _HEARTBEATER = _Heartbeater()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_heartbeater_after_fork)


class RunContext:
    """Per-run call counter for ``derive="seq"`` (see :func:`run`)."""

    def __init__(self, run_id):
        if not isinstance(run_id, str) or not run_id.strip():
            raise InvalidKeyError("run id must be a non-blank str")
        self.run_id = run_id
        self._lock = threading.Lock()
        self._counts = {}

    def peek(self, tool, fp):
        with self._lock:
            return self._counts.get((tool, str(fp)), 0)

    def advance(self, tool, fp):
        with self._lock:
            k = (tool, str(fp))
            self._counts[k] = self._counts.get(k, 0) + 1

    def __enter__(self):
        self._token = _RUN.set(self)
        return self

    def __exit__(self, *exc):
        _RUN.reset(self._token)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


_RUN = contextvars.ContextVar("idemkey_run", default=None)


def run(run_id):
    """Scope ``derive="seq"`` keys to one agent run::

        with idemkey.run("run-2026-11-07-a"):
            agent.execute()      # the Nth identical call -> the Nth key

    If the run is replayed after a crash (LangGraph checkpoint resume, a
    restarted worker) the same sequence of calls maps to the same keys, so
    completed steps replay from the store instead of executing again - while
    two *intentional* identical calls inside one run still get different keys.
    The counter advances only after a call succeeds, so retrying a call that
    raised reuses its key. Works in threads and asyncio tasks (contextvars).
    """
    return RunContext(run_id)


def _errtext(o, exc):
    if o["error_detail"] == "type":
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"[:500]


class _Guarded:
    def __init__(self, fn, **opts):
        self._fn = fn
        self._opts = opts
        self._tool = opts["tool"] or getattr(fn, "__name__", "unknown")
        functools.update_wrapper(self, fn)
        self._hidden = tuple(n for n in (opts["inject_key"], opts["atomic"]) if n)
        self._check_hidden_and_names(fn)
        sig = _with_key_param(fn, opts["key_arg"], required=opts["require_key"], hide=self._hidden)
        if sig is not None:
            self.__signature__ = sig
        # Signature used for fingerprinting: the tool's own, minus the params
        # idemkey fills in itself (those must never affect the fingerprint).
        try:
            full = inspect.signature(fn)
            self._bind_sig = full.replace(
                parameters=[p for p in full.parameters.values() if p.name not in self._hidden])
        except (TypeError, ValueError):
            self._bind_sig = None

    def _check_hidden_and_names(self, fn):
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = None
        o = self._opts
        if params is not None:
            for name in self._hidden:
                p = params.get(name)
                if p is None:
                    if not any(q.kind is inspect.Parameter.VAR_KEYWORD for q in params.values()):
                        raise TypeError(f"guard(): {fn!r} has no parameter named {name!r}")
                elif p.kind not in (inspect.Parameter.KEYWORD_ONLY,):
                    raise TypeError(
                        f"guard(): parameter {name!r} is filled in by idemkey - make it "
                        f"keyword-only (put `*,` before it) so it can never collide with "
                        f"positional arguments")
            names = set(o["ignore"]) | set(o["normalize"] or ())
            has_kw = any(q.kind is inspect.Parameter.VAR_KEYWORD for q in params.values())
            for n in names:
                if n not in params and not has_kw:
                    raise TypeError(f"guard(): ignore/normalize names {n!r}, which is not a parameter of {fn!r}")
                if n in self._hidden:
                    raise TypeError(f"guard(): {n!r} cannot be both injected and fingerprinted")

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

    def downstream_key(self, key):
        """The stable key handed to the tool (``inject_key=``) for ``key``."""
        return downstream_key(self.store_key(key))

    def _prepare(self, args, kwargs):
        """Resolve (key, tool, fingerprint, call_kwargs, advance). Shared sync/async."""
        o = self._opts
        key_arg = o["key_arg"]
        # key_arg is popped BEFORE fingerprinting: the key identifies the
        # logical operation, it is not part of the call args.
        call_kwargs = dict(kwargs)
        key = call_kwargs.pop(key_arg, None)
        for h in self._hidden:
            if h in call_kwargs:
                raise TypeError(f"{h!r} is supplied by idemkey and cannot be passed by the caller")
        if key is None:
            key = o["key"]
        if key is not None:
            _validate_key(key, key_arg)
        tool = o["tool"] or getattr(self._fn, "__name__", "unknown")
        cargs = ckwargs = fp = None
        if key is None and not o["derive"]:
            # Keyless call: it runs once, with no store and no retry, so the
            # arguments are never fingerprinted. (They used to be, so an
            # argument whose repr() raises stopped a call that has no dedup.)
            if o["require_key"]:
                raise MissingKeyError(
                    f"{tool}() requires an idempotency key: pass {key_arg}=<stable "
                    f"key per logical operation>. Refusing to run a keyless write."
                )
            return None, tool, None, call_kwargs, None
        # Canonicalise BEFORE fingerprinting / deriving (see _canonical_call).
        try:
            cargs, ckwargs = _canonical_call(
                self._bind_sig, args, call_kwargs, o["ignore"], o["normalize"])
            custom = bool(o["ignore"] or o["normalize"])
            fp = fingerprint2(tool, cargs, ckwargs, legacy=not custom)
        except (TypeError, ValueError, MemoryError, RecursionError):
            raise
        except Exception as exc:  # an argument's own repr()/getattr blew up
            raise ValueError(
                f"{tool}(): cannot fingerprint an argument ({type(exc).__name__}: {exc}); "
                f"pass plain values, or ignore=/normalize= for that argument"
            ) from exc
        advance = None
        if key is None and o["derive"] == "seq":
            ctx = _RUN.get()
            if ctx is None:
                raise MissingKeyError(
                    f"{tool}() uses derive='seq' and must be called inside "
                    f"`with idemkey.run(<run id>):`")
            n = ctx.peek(tool, fp)
            raw = f"seq\x00{ctx.run_id}\x00{tool}\x00{fp}\x00{n}"
            key = "seq_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            advance = functools.partial(ctx.advance, tool, fp)
        elif key is None and o["derive"] == "semantic":
            key = "idem_s_" + hashlib.sha256(
                f"sk\x00{o['scope']}\x00{fp}".encode()).hexdigest()[:32]
        elif key is None and o["derive"]:
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
        return key, tool, fp, call_kwargs, advance

    def _hidden_kwargs(self, key, tx=None):
        o = self._opts
        h = {}
        if o["inject_key"]:
            h[o["inject_key"]] = None if key is None else downstream_key(key)
        if o["atomic"]:
            h[o["atomic"]] = tx
        return h

    def __call__(self, *args, **kwargs):
        o = self._opts
        key, tool, fp, call_kwargs, advance = self._prepare(args, kwargs)

        # No key -> single attempt, no store, no retry. Fail loud.
        if key is None:
            if o["atomic"]:
                raise MissingKeyError(f"{tool}() uses atomic=: an idempotency key is required")
            return self._fn(*args, **call_kwargs, **self._hidden_kwargs(None))

        store = o["store"] or get_default_store()
        owner = _new_owner()
        outcome, row = store.claim(key, tool, str(fp), o["scope"], owner, o["claim_ttl"], o["ttl"],
                                   fp.alts)

        if outcome == "duplicate":
            result = self._handle_duplicate(store, key, row, fp, owner, o, args, call_kwargs)
        elif outcome == "inflight":
            if row is not None:
                # A different call wearing an in-flight (or stale) key: reject
                # now rather than wait for - or take over - someone else's op.
                self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            result = self._handle_inflight(store, key, fp, owner, o)
        else:  # "claimed": we own it
            result = self._execute(store, key, owner, o, args, call_kwargs,
                                   retry=row["attempts"] > 1)
        if advance is not None:
            advance()
        return result

    # -- duplicate / inflight paths --------------------------------------
    def _check_fingerprint(self, key, row, fp, policy):
        if fp.matches(row["fingerprint"]):
            return
        msg = (
            f"idempotency key {key!r} was first used for a different call "
            f"(tool={row['tool']!r}, scope={row['scope']!r}). Refusing to replay "
            f"that result for these arguments."
        )
        _emit(self._opts, "fingerprint_mismatch", key, self._tool, policy=policy)
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
        _emit(self._opts, "replayed", key, self._tool)
        return _codec.loads(row["result_json"])

    def _unknown_error(self, key, row):
        return OutcomeUnknownError(
            f"idempotency key {key!r}: a previous execution was interrupted "
            f"mid-call ({row['error']}); its write may have landed. Check the "
            f"downstream system, then run `idemkey forget` on this key to allow "
            f"a re-run - or give guard() a `reconcile=` hook to resolve this "
            f"automatically."
        )

    def _handle_duplicate(self, store, key, row, fp, owner, o, args, kwargs):
        if row["status"] == "completed":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            return self._replay(key, row)
        if row["status"] == "unknown":
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            if o["reconcile"] is not None and store.reclaim(key, owner, "unknown"):
                return self._execute(store, key, owner, o, args, kwargs, retry=True)
            raise self._unknown_error(key, row)
        # status == "failed": a retry with the same key re-executes. The
        # fingerprint check guards against arg drift on the retry path too:
        # if the agent changed the args, that is not a retry, it is a new
        # operation wearing an old key.
        self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
        if store.reclaim_failed(key, owner):
            return self._execute(store, key, owner, o, args, kwargs, retry=True)
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
        # double-execute the write). One shared daemon thread serves every
        # in-flight call (no thread spawned per call); it beats on its own
        # thread so it keeps going even if an async tool blocks the loop.
        # atomic= calls hold the write lock for their whole (short)
        # transaction and cannot be double-executed, so they skip it.
        if o["atomic"]:
            return _Stopper(None)
        interval = min(max(o["claim_ttl"], 0.01) / 3.0, 60.0)
        return _Stopper(_HEARTBEATER.register(store, key, owner, interval))

    def _execute(self, store, key, owner, o, args, kwargs, retry=False):
        stop = self._start_heartbeat(store, key, owner, o)
        try:
            if retry and o["reconcile"] is not None:
                found, result = self._reconcile(store, key, owner, o, args, kwargs)
                if found:
                    self._commit(store, key, owner, o, result)
                    return result
            _emit(o, "executing", key, self._tool, takeover_or_retry=retry)
            if o["atomic"]:
                out = self._run_atomic(store, key, owner, o, args, kwargs)
            else:
                out = self._run_attempts(store, key, owner, o, args, kwargs)
            _emit(o, "executed", key, self._tool)
            return out
        except BaseException as exc:
            _emit(o, "failed", key, self._tool, error=type(exc).__name__)
            raise
        finally:
            stop.set()

    def _reconcile(self, store, key, owner, o, args, kwargs):
        """Ask the downstream system whether an earlier attempt landed."""
        try:
            res = o["reconcile"](*args, **kwargs, **self._hidden_kwargs(key))
        except Exception as exc:
            store.mark_unknown(key, f"reconcile raised {_errtext(o, exc)}", owner=owner)
            raise ReconcileError(
                f"reconcile hook raised for key {key!r} ({type(exc).__name__}); cannot tell "
                f"whether the earlier attempt landed - key marked unknown") from exc
        if res is NOT_FOUND:
            return False, None
        return True, res

    @staticmethod
    def _fail(store, key, owner, error):
        store.fail(key, error, owner=owner)

    @staticmethod
    def _payload(o, result):
        if o["store_result"] is False:
            return None, False
        # The write has already happened. Nothing below may turn that into an
        # exception that leaves the key pending (a later retry would re-run the
        # write): a result that cannot be redacted or serialised is recorded as
        # "completed, not replayable" (fail closed - the unredacted value is
        # never stored).
        try:
            if o["redact"] is not None:
                result = o["redact"](result)
            return _encode_result(result, o["result_max_bytes"], o["result_codec"] == "typed")
        except Exception as exc:  # noqa: BLE001
            warnings.warn(
                f"idemkey: the result of a completed call could not be stored "
                f"({type(exc).__name__} in redact/serialise); recorded as not replayable",
                RuntimeWarning, stacklevel=2)
            return None, False

    @staticmethod
    def _commit(store, key, owner, o, result):
        payload, replayable = _Guarded._payload(o, result)
        if not store.complete(key, payload, replayable, owner=owner):
            warnings.warn(
                f"idempotency key {key!r}: this execution's claim was taken over "
                f"before its result could be recorded (it outlived claim_ttl="
                f"{o['claim_ttl']}s without a heartbeat). Another owner may have "
                f"executed the same operation - check for a duplicate effect.",
                ClaimLostWarning,
                stacklevel=4,
            )

    def _verify_failed(self, store, key, owner, exc=None):
        if exc is not None:
            self._fail(store, key, owner, f"verify raised {type(exc).__name__}: {exc}"[:500])
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
        hidden = self._hidden_kwargs(key)
        for attempt in range(o["max_retries"] + 1):
            if attempt > 0 and o["reconcile"] is not None:
                # The previous attempt raised a retryable error (a timeout is
                # exactly "the write may have landed"): ask downstream before
                # writing again.
                found, earlier = self._reconcile(store, key, owner, o, args, kwargs)
                if found:
                    _emit(o, "reconciled", key, self._tool, attempt=attempt)
                    self._commit(store, key, owner, o, earlier)
                    return earlier
            try:
                result = self._fn(*args, **kwargs, **hidden)
            except o["retry_on"] as exc:
                last_exc = exc
                store.heartbeat(key, owner)
                _emit(o, "retryable_error", key, self._tool, attempt=attempt, error=type(exc).__name__)
                if attempt < o["max_retries"]:
                    o["sleep"](_backoff_delay(attempt, o["backoff_base"], o["backoff_cap"], exc))
                    continue
                break
            except Exception as exc:  # non-retryable: no second execution, ever
                self._fail(store, key, owner, _errtext(o, exc))
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
        self._fail(store, key, owner, _errtext(o, last_exc))
        raise last_exc

    def _run_atomic(self, store, key, owner, o, args, kwargs):
        """Tool writes + the key's ``completed`` row commit in ONE transaction."""
        last_exc = None
        for attempt in range(o["max_retries"] + 1):
            try:
                conn = store.begin_atomic()
            except Exception as exc:
                # Nothing ran: release the claim so a retry is not blocked
                # until claim_ttl expires.
                self._fail(store, key, owner, _errtext(o, exc))
                raise
            done = False
            try:
                try:
                    result = self._fn(*args, **kwargs, **self._hidden_kwargs(key, tx=conn))
                except o["retry_on"] as exc:
                    last_exc = exc
                    store.rollback_atomic(conn)
                    done = True
                    if attempt < o["max_retries"]:
                        o["sleep"](_backoff_delay(attempt, o["backoff_base"], o["backoff_cap"], exc))
                        continue
                    break
                except Exception as exc:
                    store.rollback_atomic(conn)
                    done = True
                    self._fail(store, key, owner, _errtext(o, exc))
                    raise
                if o["verify"] is not None:
                    try:
                        ok = o["verify"](result)
                    except Exception as exc:
                        store.rollback_atomic(conn)
                        done = True
                        raise self._verify_failed(store, key, owner, exc) from exc
                    if not ok:
                        store.rollback_atomic(conn)
                        done = True
                        raise self._verify_failed(store, key, owner)
                payload, replayable = self._payload(o, result)
                done = True
                if store.commit_atomic(conn, key, owner, payload, replayable):
                    return result
                row = store.get(key)
                if row is not None and row["status"] == "completed":
                    return self._replay(key, row)
                raise ClaimLostError(
                    f"idempotency key {key!r}: claim lost before commit; this execution's "
                    f"transaction was rolled back (no effect from it exists)")
            finally:
                if not done:
                    store.rollback_atomic(conn)
        self._fail(store, key, owner, _errtext(o, last_exc))
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
      of executing a second time (or, with ``reconcile=``, asks downstream);
    * while the result was being committed -> the commit is shielded and
      finishes, so the retry replays the real result.
    """

    async def __call__(self, *args, **kwargs):
        o = self._opts
        key, tool, fp, call_kwargs, advance = self._prepare(args, kwargs)
        if key is None:
            return await self._fn(*args, **call_kwargs, **self._hidden_kwargs(None))

        store = o["store"] or get_default_store()
        owner = _new_owner()
        outcome, row = await self._shielded_claim(
            store.claim, store, key, owner,
            key, tool, str(fp), o["scope"], owner, o["claim_ttl"], o["ttl"], fp.alts,
        )
        result = await self._adispatch(store, key, owner, o, fp, outcome, row, args, call_kwargs)
        if advance is not None:
            advance()
        return result

    async def _adispatch(self, store, key, owner, o, fp, outcome, row, args, call_kwargs):
        if outcome == "claimed":
            return await self._aexecute(store, key, owner, o, args, call_kwargs,
                                        retry=row["attempts"] > 1)
        if outcome == "duplicate":
            # Same rules as the sync path: replay completed, re-execute failed.
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
            if row["status"] == "completed":
                return self._replay(key, row)
            if row["status"] == "unknown":
                if o["reconcile"] is not None:
                    won = await self._shielded_claim(
                        store.reclaim, store, key, owner, key, owner, "unknown")
                    if won:
                        return await self._aexecute(store, key, owner, o, args, call_kwargs, retry=True)
                else:
                    raise self._unknown_error(key, row)
            else:
                won = await self._shielded_claim(store.reclaim_failed, store, key, owner, key, owner)
                if won:
                    return await self._aexecute(store, key, owner, o, args, call_kwargs, retry=True)
        elif row is not None:
            self._check_fingerprint(key, row, fp, o["fingerprint_policy"])
        # In flight elsewhere (or we lost the reclaim race): wait and replay.
        return await self._ahandle_inflight(store, key, fp, o)

    @staticmethod
    async def _shielded_claim(func, store, key, owner, *args):
        """Run a claiming store call; if cancelled meanwhile, let it finish and
        release whatever it claimed (the tool never started, so ``failed`` -
        reclaimable - is the truthful status)."""
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
        delay = 0.002
        while True:
            row = await _to_thread(store.get, key)
            if row is None:
                raise self._inflight_error(key, o, KeyError(key))
            if row["status"] != "pending":
                return self._settled(key, row, fp, o)
            if time.monotonic() >= deadline:
                exc = TimeoutError(key)
                raise self._inflight_error(key, o, exc) from exc
            await asyncio.sleep(delay)
            delay = min(delay * 2, poll_interval)

    async def _aexecute(self, store, key, owner, o, args, kwargs, retry=False):
        stop = self._start_heartbeat(store, key, owner, o)
        try:
            if retry and o["reconcile"] is not None:
                found, result = await self._areconcile(store, key, owner, o, args, kwargs)
                if found:
                    await asyncio.shield(_to_thread(self._commit, store, key, owner, o, result))
                    return result
            _emit(o, "executing", key, self._tool, takeover_or_retry=retry)
            out = await self._arun_attempts(store, key, owner, o, args, kwargs)
            _emit(o, "executed", key, self._tool)
            return out
        except asyncio.CancelledError:
            _emit(o, "failed", key, self._tool, error="CancelledError")
            # Cancelled mid-flight: the write may or may not have landed.
            # Record ``unknown`` (fenced: a no-op if the row already settled)
            # so a same-key retry neither re-executes nor hangs on a dead
            # pending row. Never ``failed`` - that would re-run the write.
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

    async def _areconcile(self, store, key, owner, o, args, kwargs):
        try:
            hidden = self._hidden_kwargs(key)
            if inspect.iscoroutinefunction(o["reconcile"]):
                res = await o["reconcile"](*args, **kwargs, **hidden)
            else:
                res = await _to_thread(functools.partial(o["reconcile"], *args, **kwargs, **hidden))
        except Exception as exc:
            store.mark_unknown(key, f"reconcile raised {_errtext(o, exc)}", owner=owner)
            raise ReconcileError(
                f"reconcile hook raised for key {key!r} ({type(exc).__name__}); cannot tell "
                f"whether the earlier attempt landed - key marked unknown") from exc
        if res is NOT_FOUND:
            return False, None
        return True, res

    async def _asleep(self, o, delay):
        if o["sleep"] is time.sleep:
            await asyncio.sleep(delay)
        else:
            await _maybe_await(o["sleep"](delay))

    async def _arun_attempts(self, store, key, owner, o, args, kwargs):
        last_exc = None
        hidden = self._hidden_kwargs(key)
        for attempt in range(o["max_retries"] + 1):
            if attempt > 0 and o["reconcile"] is not None:
                found, earlier = await self._areconcile(store, key, owner, o, args, kwargs)
                if found:
                    _emit(o, "reconciled", key, self._tool, attempt=attempt)
                    await asyncio.shield(_to_thread(self._commit, store, key, owner, o, earlier))
                    return earlier
            try:
                result = await self._fn(*args, **kwargs, **hidden)
            except o["retry_on"] as exc:
                last_exc = exc
                await _to_thread(store.heartbeat, key, owner)
                _emit(o, "retryable_error", key, self._tool, attempt=attempt, error=type(exc).__name__)
                if attempt < o["max_retries"]:
                    await self._asleep(o, _backoff_delay(attempt, o["backoff_base"], o["backoff_cap"], exc))
                    continue
                break
            except Exception as exc:  # non-retryable
                await _to_thread(self._fail, store, key, owner, _errtext(o, exc))
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
        await _to_thread(self._fail, store, key, owner, _errtext(o, last_exc))
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
    wrapper.downstream_key = core.downstream_key
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
    "inject_key": None,
    "reconcile": None,
    "atomic": None,
    "ignore": (),
    "normalize": None,
    "redact": None,
    "store_result": True,
    "error_detail": "full",
    "result_codec": "typed",   # typed | json
    "on_event": None,
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

    Exactly-once helpers (0.3.0):

    * ``inject_key="idempotency_key"`` - hand the tool a stable downstream key
      (same on every retry/takeover) to pass to Stripe-style APIs. The
      parameter must be keyword-only; it is hidden from the tool schema.
    * ``reconcile=fn(*args, **kwargs)`` - called before any *re-execution*
      (stale takeover, retry after failure, ``unknown`` outcome). Return the
      earlier attempt's result if it landed, or :data:`NOT_FOUND`.
    * ``atomic="tx"`` - the tool receives a sqlite3 connection in an open
      transaction on the store's database; its writes and the key's
      ``completed`` record commit together (crash => both or neither).
    * ``ignore=("request_id",)`` / ``normalize={"email": str.lower}`` -
      semantic duplicates: drop volatile arguments / normalise values before
      fingerprinting.
    * ``derive="seq"`` (inside ``idemkey.run(run_id)``) or ``"semantic"`` -
      see the README.
    * ``redact=fn(result)``, ``store_result=False``, ``error_detail="type"`` -
      keep PII out of the store.
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

    if merged["derive"] not in (False, True, "semantic", "seq"):
        raise ValueError("guard() derive must be False, True, 'semantic' or 'seq'")
    if merged["result_codec"] not in ("typed", "json"):
        raise ValueError("guard() result_codec must be 'typed' or 'json'")
    if merged["on_event"] is not None and not callable(merged["on_event"]):
        raise TypeError("guard() on_event must be callable")
    if merged["error_detail"] not in ("full", "type"):
        raise ValueError("guard() error_detail must be 'full' or 'type'")
    for name in ("inject_key", "atomic"):
        v = merged[name]
        if v is not None and not (isinstance(v, str) and v.isidentifier()):
            raise TypeError(f"guard() {name} must be the name of a parameter of the tool")
    if merged["inject_key"] and merged["inject_key"] == merged["atomic"]:
        raise TypeError("guard() inject_key and atomic must name different parameters")
    for name in ("reconcile", "redact", "verify"):
        if merged[name] is not None and not callable(merged[name]):
            raise TypeError(f"guard() {name} must be callable")
    if isinstance(merged["ignore"], str):
        merged["ignore"] = (merged["ignore"],)
    merged["ignore"] = tuple(merged["ignore"])
    if merged["normalize"] is not None:
        merged["normalize"] = dict(merged["normalize"])
        for n, f in merged["normalize"].items():
            if not callable(f):
                raise TypeError(f"guard() normalize[{n!r}] must be callable")
    if merged["store_result"] not in (True, False):
        raise TypeError("guard() store_result must be True or False")

    def wrap(fn):
        if merged["atomic"] and _is_async(fn):
            raise TypeError("guard(atomic=...) supports sync tools only (the transaction "
                            "must stay on one thread)")
        return _make_wrapper(fn, merged)

    if _fn is not None:
        return wrap(_fn)
    return wrap


__all__ = ["guard"]
