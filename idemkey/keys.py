"""Key generation, deterministic derivation, and request fingerprinting.

Two key kinds:
  * random keys  - ``new_key()``      -> ``idem_<32 hex>``
  * derived keys - ``derive_key(...)`` -> ``idem_d_<32 hex>`` (opt-in, scoped)

The ``idem_d_`` prefix marks a key as derived so operators can tell at a glance
which dedup entries came from deterministic derivation (higher false-positive
risk if the scope is wrong) versus agent-assigned random keys.
"""

from __future__ import annotations

import hashlib
import json
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
