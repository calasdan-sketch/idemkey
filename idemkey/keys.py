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


_BIG_DIGITS = 4000  # beyond this an integral value is spelled as digits+exponent


def _canon_number(x) -> str:
    """Exact, context-free encoding of a number.

    Never rounds (the global ``decimal`` context would silently merge two
    Decimals that differ after 28 digits) and never expands a huge exponent
    into a huge integer (``Decimal("1e999999999")`` must not hang the caller).
    """
    if isinstance(x, int):
        if x.bit_length() <= 14000:  # < 4300 digits, the interpreter's int-to-str limit
            return f"num:{x}"
        return ("num:-0x" if x < 0 else "num:0x") + format(abs(x), "x")
    if isinstance(x, float):
        if math.isnan(x):
            return "num:nan"
        if math.isinf(x):
            return "num:inf" if x > 0 else "num:-inf"
        if x == int(x) and abs(x) < 1e15:
            return f"num:{int(x)}"  # -0.0 -> num:0, 10.0 -> num:10
        d = decimal.Decimal(x)  # exact binary value: 0.1 != Decimal("0.1")
    else:
        if not x.is_finite():
            return "num:" + str(x).lower()
        d = x
    sign, digit_tuple, exp_raw = d.as_tuple()
    if not isinstance(exp_raw, int):  # NaN / Infinity: handled above, kept for the type checker
        return "num:" + str(d).lower()
    exp = exp_raw
    digits = "".join(map(str, digit_tuple)).lstrip("0")
    if not digits:
        return "num:0"
    stripped = digits.rstrip("0")
    exp += len(digits) - len(stripped)
    digits = stripped
    neg = "-" if sign else ""
    if exp >= 0:
        if len(digits) + exp <= _BIG_DIGITS:
            return f"num:{neg}{digits}{'0' * exp}"
        return f"num:{neg}{digits}e{exp}"
    point = len(digits) + exp  # digits before the decimal point
    if exp < -_BIG_DIGITS:
        return f"num:{neg}{digits}e{exp}"
    if point > 0:
        return f"num:{neg}{digits[:point]}.{digits[point:]}"
    return f"num:{neg}0.{'0' * -point}{digits}"


_HASH_ABOVE = 4096  # strings / bytes longer than this are fingerprinted by digest


def _canon_text(obj: str) -> str:
    text = unicodedata.normalize("NFC", obj)
    if len(text) > _HASH_ABOVE:
        return "str:h:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return "str:" + json.dumps(text, ensure_ascii=True)


def _canon_bytes(obj) -> str:
    b = bytes(obj)
    if len(b) > _HASH_ABOVE:
        return "bytes:h:" + hashlib.sha256(b).hexdigest()
    return "bytes:" + b.hex()


def _canon2(obj, _depth=0, _seen=None) -> str:
    if _depth > _MAX_DEPTH:
        # Never truncate silently (two different deep values would merge):
        # hash the whole repr, or refuse (fail closed) if it cannot be taken.
        try:
            return "deep:" + hashlib.sha256(_ADDR.sub("", repr(obj)).encode("utf-8")).hexdigest()
        except RecursionError:
            raise ValueError("argument is nested too deeply to fingerprint safely") from None
    t = type(obj)
    if t is int:  # fast paths for the overwhelmingly common exact types
        return _canon_number(obj)
    if t is str:
        return _canon_text(obj)
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
        return _canon_text(obj)
    if isinstance(obj, (bytes, bytearray)):
        return _canon_bytes(obj)
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
        except (RecursionError, ValueError):  # v1 could never fingerprint this call anyway
            fp.alts = ()
    return fp
