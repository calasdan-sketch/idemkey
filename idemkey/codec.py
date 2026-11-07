"""Result codec: JSON that round-trips the types agents' tools really return.

Plain JSON turns tuples into lists, ``bytes``/``set``/``Decimal``/``datetime``
into ``repr()`` strings and integer dict keys into strings - so the first call
and a replay of the same call returned *different* values. The typed codec
tags those values, so a replay equals the original result. Anything it does not
know (arbitrary objects) is still stored as ``repr()`` text, as before.

Tag shape: a one-key object ``{"$t": ...}``. A user dict that could be
mistaken for a tag (a key starting with ``$``, or any non-string key) is stored
as ``{"$m": [[key, value], ...]}``, so decoding never confuses data with tags.
Rows written by earlier versions contain no tags and decode exactly as before.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import json
import uuid

_MAX_DEPTH = 200


def _enc(o, depth=0):
    if depth > _MAX_DEPTH:
        raise ValueError("result nested too deeply")
    if o is None or isinstance(o, (bool, str)):
        return o
    if isinstance(o, int):
        return o if type(o) is int else int(o)
    if isinstance(o, float):
        return o
    d = depth + 1
    if isinstance(o, tuple) and hasattr(o, "_fields"):  # namedtuple -> plain tuple
        return {"$t": [_enc(v, d) for v in o]}
    if isinstance(o, tuple):
        return {"$t": [_enc(v, d) for v in o]}
    if isinstance(o, list):
        return [_enc(v, d) for v in o]
    if isinstance(o, dict):
        if all(isinstance(k, str) and not k.startswith("$") for k in o):
            return {k: _enc(v, d) for k, v in o.items()}
        return {"$m": [[_enc(k, d), _enc(v, d)] for k, v in o.items()]}
    if isinstance(o, (set, frozenset)):
        items = [_enc(v, d) for v in o]
        items.sort(key=lambda v: json.dumps(v, sort_keys=True, default=repr))
        return {"$f" if isinstance(o, frozenset) else "$s": items}
    if isinstance(o, (bytes, bytearray)):
        return {"$b": base64.b64encode(bytes(o)).decode("ascii")}
    if isinstance(o, decimal.Decimal):
        return {"$d": str(o)}
    if isinstance(o, datetime.datetime):
        return {"$dt": o.isoformat()}
    if isinstance(o, datetime.date):
        return {"$da": o.isoformat()}
    if isinstance(o, datetime.time):
        return {"$ti": o.isoformat()}
    if isinstance(o, uuid.UUID):
        return {"$u": str(o)}
    return repr(o)


def _hook(d):
    if len(d) == 1:
        (k, v), = d.items()
        try:
            if k == "$t" and isinstance(v, list):
                return tuple(v)
            if k == "$s" and isinstance(v, list):
                return set(v)
            if k == "$f" and isinstance(v, list):
                return frozenset(v)
            if k == "$b" and isinstance(v, str):
                return base64.b64decode(v, validate=True)
            if k == "$d" and isinstance(v, str):
                return decimal.Decimal(v)
            if k == "$dt" and isinstance(v, str):
                return datetime.datetime.fromisoformat(v)
            if k == "$da" and isinstance(v, str):
                return datetime.date.fromisoformat(v)
            if k == "$ti" and isinstance(v, str):
                return datetime.time.fromisoformat(v)
            if k == "$u" and isinstance(v, str):
                return uuid.UUID(v)
            if k == "$m" and isinstance(v, list):
                return {_freeze(a): b for a, b in v}
        except (ValueError, TypeError):
            pass
    return d


def _freeze(k):
    # JSON arrays decode to lists, which cannot be dict keys; tuple keys were
    # tagged "$t" and are already tuples by the time the hook reaches the pair.
    return tuple(_freeze(x) for x in k) if isinstance(k, list) else k


def dumps(result) -> str:
    return json.dumps(_enc(result))


def loads(text: str):
    return json.loads(text, object_hook=_hook)
