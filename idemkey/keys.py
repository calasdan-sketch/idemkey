"""Key generation, deterministic derivation, and request fingerprinting.

Two key kinds:
  * random keys  - ``new_key()``      -> ``idem_<32 hex>``
  * derived keys - ``derive_key(...)`` -> ``idem_d_<32 hex>`` (opt-in, scoped)

The ``idem_d_`` prefix marks a key as derived so operators can tell at a glance
which dedup entries came from deterministic derivation (higher false-positive
risk if the scope is wrong) versus agent-assigned random keys.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import hashlib
import json
import math
import pathlib
import re
import unicodedata
import uuid

RANDOM_PREFIX = "idem_"
DERIVED_PREFIX = "idem_d_"


def new_key(prefix: str = RANDOM_PREFIX) -> str:
    """Generate a fresh random idempotency key (UUID4, hex)."""
    return f"{prefix}{uuid.uuid4().hex}"


def _canon(obj) -> str:
    """Deterministic canonical encoding of (mostly JSON-like) values.

    Type-tagged so ``1`` and ``"1"`` never share an encoding, and dict key
    order / kwarg order never affects the output.
    """
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return f"int:{obj}"
    if isinstance(obj, float):
        return f"float:{obj!r}"
    if isinstance(obj, str):
        return "str:" + json.dumps(obj, ensure_ascii=True)
    if isinstance(obj, (bytes, bytearray)):
        return "bytes:" + bytes(obj).hex()
    if isinstance(obj, (list, tuple)):
        tag = "list" if isinstance(obj, list) else "tuple"
        return tag + ":[" + ",".join(_canon(v) for v in obj) + "]"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: _canon(kv[0]))
        return "dict:{" + ",".join(_canon(k) + "=>" + _canon(v) for k, v in items) + "}"
    if isinstance(obj, (set, frozenset)):
        return "set:{" + ",".join(sorted(_canon(v) for v in obj)) + "}"
    # Fallback for arbitrary objects: type-qualified repr. Documented limitation:
    # two distinct objects with identical repr() share an encoding.
    return f"obj:{type(obj).__qualname__}:{obj!r}"


def fingerprint(tool: str, args: tuple = (), kwargs: dict | None = None) -> str:
    """Stable sha256 hex digest of (tool name, positional args, keyword args).

    The fingerprint locks a key to the exact call it was first used for. A later
    call reusing the key with different args is a bug signal, not a retry.
    """
    payload = "fp\x00" + _canon(tool) + "\x00" + _canon(tuple(args)) + "\x00" + _canon(dict(kwargs or {}))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_key(
    tool: str,
    args: tuple = (),
    kwargs: dict | None = None,
    scope: str = "default",
) -> str:
    """Deterministically derive a key from (scope, tool, args).

    Opt-in. Only use for operations that are idempotent *by construction*
    (e.g. ``ensure_webhook_registered``). Never use for operations an agent may
    legitimately invoke twice with identical arguments (e.g. ``transfer``) —
    the second intentional call would be swallowed as a duplicate.
    """
    payload = "dk\x00" + _canon(scope) + "\x00" + _canon(tool) + "\x00" + _canon(tuple(args)) + "\x00" + _canon(dict(kwargs or {}))
    return DERIVED_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def is_derived(key: str) -> bool:
    """True if the key was produced by :func:`derive_key`."""
    return key.startswith(DERIVED_PREFIX)



# -- semantic (v2) canonicalisation --------------------------------------
#
# v1 (``_canon``) is kept byte-for-byte so fingerprints stored by earlier
# versions still match. v2 only ever merges encodings that are *the same
# value spelled differently* (never two different values):
#   * int / float / Decimal that are numerically equal  -> one encoding
#     (``10``, ``10.0``, ``Decimal("10.00")``); bool stays distinct;
#   * str in different Unicode normalisation forms (NFC)  -> one encoding;
#   * dataclasses, namedtuples, Enums, pydantic models, datetimes, UUIDs,
#     Paths and plain objects with state  -> encoded by content, not by a
#     ``repr()`` that may embed a memory address;
#   * dict/set order never matters.

_MAX_DEPTH = 40
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


def _canon_number(x) -> str:
    if isinstance(x, float):
        if math.isnan(x):
            return "num:nan"
        if math.isinf(x):
            return "num:inf" if x > 0 else "num:-inf"
        d = decimal.Decimal(x)  # exact binary value: 0.1 != Decimal("0.1")
    elif isinstance(x, decimal.Decimal):
        if not x.is_finite():
            return "num:" + str(x).lower()
        d = x
    else:
        d = decimal.Decimal(x)
    if d == d.to_integral_value():
        return f"num:{int(d)}"
    return "num:" + format(d.normalize(), "f")


def _canon2(obj, _depth=0, _seen=None) -> str:
    if _depth > _MAX_DEPTH:
        # Never truncate silently (two different deep values would merge):
        # hash the whole repr, or refuse (fail closed) if it cannot be taken.
        try:
            return "deep:" + hashlib.sha256(_ADDR.sub("", repr(obj)).encode("utf-8")).hexdigest()
        except RecursionError:
            raise ValueError("argument is nested too deeply to fingerprint safely") from None
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, enum.Enum):
        return f"enum:{type(obj).__qualname__}.{obj.name}"
    if isinstance(obj, (int, float, decimal.Decimal)):
        return _canon_number(obj)
    if isinstance(obj, str):
        return "str:" + json.dumps(unicodedata.normalize("NFC", obj), ensure_ascii=True)
    if isinstance(obj, (bytes, bytearray)):
        return "bytes:" + bytes(obj).hex()
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return f"{type(obj).__name__}:{obj.isoformat()}"
    if isinstance(obj, datetime.timedelta):
        return f"timedelta:{obj.total_seconds()!r}"
    if isinstance(obj, uuid.UUID):
        return f"uuid:{obj}"
    if isinstance(obj, pathlib.PurePath):
        return f"path:{obj.as_posix()}"
    seen = _seen if _seen is not None else set()
    if id(obj) in seen:
        return "cycle"
    seen = seen | {id(obj)}
    d1 = _depth + 1
    if isinstance(obj, tuple) and hasattr(obj, "_fields"):  # namedtuple
        return f"nt:{type(obj).__qualname__}:[" + ",".join(
            f"{f}=" + _canon2(v, d1, seen) for f, v in zip(obj._fields, obj)) + "]"
    if isinstance(obj, (list, tuple)):
        tag = "list" if isinstance(obj, list) else "tuple"
        return tag + ":[" + ",".join(_canon2(v, d1, seen) for v in obj) + "]"
    if isinstance(obj, dict):
        items = sorted(((_canon2(k, d1, seen), _canon2(v, d1, seen)) for k, v in obj.items()))
        return "dict:{" + ",".join(k + "=>" + v for k, v in items) + "}"
    if isinstance(obj, (set, frozenset)):
        return "set:{" + ",".join(sorted(_canon2(v, d1, seen) for v in obj)) + "}"
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return f"dc:{type(obj).__qualname__}:" + _canon2(
            {f.name: getattr(obj, f.name, None) for f in dataclasses.fields(obj)}, d1, seen)
    dump = getattr(obj, "model_dump", None) or getattr(obj, "dict", None)  # pydantic v2 / v1
    if callable(dump):
        try:
            return f"model:{type(obj).__qualname__}:" + _canon2(dump(), d1, seen)
        except Exception:  # noqa: BLE001, S110
            pass
    state = getattr(obj, "__dict__", None)
    if isinstance(state, dict):
        return f"obj:{type(obj).__qualname__}:" + _canon2(state, d1, seen)
    slots = [s for c in type(obj).__mro__ for s in getattr(c, "__slots__", ()) if isinstance(s, str)]
    if slots:
        return f"obj:{type(obj).__qualname__}:" + _canon2(
            {s: getattr(obj, s, None) for s in slots}, d1, seen)
    # Last resort: repr, with any memory address scrubbed so two equal
    # objects built in different processes still agree.
    return f"obj:{type(obj).__qualname__}:{_ADDR.sub('', repr(obj))}"


class Fingerprint(str):
    """A v2 fingerprint. ``alts`` holds the legacy v1 digest so rows written
    by idemkey <= 0.2 still match after an upgrade."""

    alts: tuple = ()

    def matches(self, stored: str) -> bool:
        return stored == self or stored in self.alts


def fingerprint2(tool: str, args: tuple = (), kwargs: dict | None = None,
                 legacy: bool = True) -> Fingerprint:
    """Semantic fingerprint (see module notes). ``legacy=True`` also carries
    the v1 digest so previously stored rows still match."""
    kw = dict(kwargs or {})
    payload = "fp2\x00" + _canon2(tool) + "\x00" + _canon2(tuple(args)) + "\x00" + _canon2(kw)
    fp = Fingerprint(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    if legacy:
        try:
            fp.alts = (fingerprint(tool, args, kw),)
        except RecursionError:  # v1 could never fingerprint this call anyway
            fp.alts = ()
    return fp
