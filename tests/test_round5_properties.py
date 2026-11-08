"""Round 5: property-based and model-based tests (need `hypothesis`; skipped without it)."""

import os

import pytest

import idemkey
from idemkey import DedupStore, guard

pytest.importorskip("hypothesis")
from hypothesis import given, settings
from hypothesis import strategies as st_

from idemkey import codec
from idemkey.guard import namespaced_key
from idemkey.keys import fingerprint

json_leaf = st_.one_of(st_.none(), st_.booleans(), st_.integers(-10**12, 10**12),
                       st_.floats(allow_nan=False, allow_infinity=False), st_.text(max_size=20))
json_val = st_.recursive(json_leaf, lambda c: st_.one_of(
    st_.lists(c, max_size=4), st_.dictionaries(st_.text(max_size=8), c, max_size=4)), max_leaves=12)

SLOW = settings(max_examples=int(os.environ.get("IDEMKEY_EXAMPLES", "60")), deadline=None)


@SLOW
@given(json_val)
def test_codec_roundtrip_plain_json(v):
    assert codec.loads(codec.dumps(v)) == v


@SLOW
@given(st_.dictionaries(st_.text(min_size=1, max_size=6), json_leaf, max_size=6))
def test_fingerprint_ignores_dict_order(d):
    rev = dict(reversed(list(d.items())))
    assert fingerprint("t", (d,), {}) == fingerprint("t", (rev,), {})
    assert fingerprint("t", (), d) == fingerprint("t", (), rev)


@SLOW
@given(json_val, json_val)
def test_fingerprint_separates_different_values(a, b):
    # equal fingerprints are allowed only for deliberately merged values (10 vs 10.0, NFC/NFD)
    if codec.dumps(a) != codec.dumps(b) and fingerprint("t", (a,), {}) == fingerprint("t", (b,), {}):
        assert _loosely_equal(a, b)


def _loosely_equal(a, b):
    import unicodedata
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_loosely_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        na = {unicodedata.normalize("NFC", k): v for k, v in a.items()}
        nb = {unicodedata.normalize("NFC", k): v for k, v in b.items()}
        return na.keys() == nb.keys() and all(_loosely_equal(na[k], nb[k]) for k in na)
    return a == b


part = st_.text(alphabet=st_.characters(blacklist_characters="|", blacklist_categories=("Cc", "Cs")), max_size=6)


@SLOW
@given(part, part, part, st_.text(max_size=10), part, part, part, st_.text(max_size=10))
def test_namespaced_key_is_injective(s1, t1, p1, k1, s2, t2, p2, k2):
    if (s1, t1, p1, k1) != (s2, t2, p2, k2):
        assert namespaced_key(k1, t1, s1, p1) != namespaced_key(k2, t2, s2, p2)


call = st_.tuples(st_.sampled_from(["k1", "k2", "k3"]), st_.sampled_from(["a", "b"]))


@settings(max_examples=int(os.environ.get("IDEMKEY_EXAMPLES", "40")), deadline=None)
@given(st_.lists(call, min_size=1, max_size=12))
def test_guard_matches_a_dict_model(tmp_path_factory, calls):
    """Reference model: first call per key runs and fixes its argument; later
    calls with the same argument replay; a different argument is refused."""
    store = DedupStore(str(tmp_path_factory.mktemp("m") / "m.db"))
    runs = []

    @guard(store=store)
    def tool(arg):
        runs.append(arg)
        return {"arg": arg, "n": len(runs)}

    model = {}
    for key, arg in calls:
        if key not in model:
            r = tool(arg, idemkey=key)
            model[key] = (arg, r)
            assert r == {"arg": arg, "n": len(runs)}
        elif model[key][0] == arg:
            assert tool(arg, idemkey=key) == model[key][1]
        else:
            with pytest.raises(idemkey.FingerprintMismatchError):
                tool(arg, idemkey=key)
    assert len(runs) == len(model)       # exactly one real execution per key
    store.close()
