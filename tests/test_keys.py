from idemkey.keys import (
    DERIVED_PREFIX,
    RANDOM_PREFIX,
    derive_key,
    fingerprint,
    is_derived,
    new_key,
)


def test_new_key_format_and_uniqueness():
    keys = {new_key() for _ in range(1000)}
    assert len(keys) == 1000
    k = new_key()
    assert k.startswith(RANDOM_PREFIX)
    assert len(k) == len(RANDOM_PREFIX) + 32


def test_new_key_custom_prefix():
    assert new_key(prefix="order_").startswith("order_")


def test_derive_key_deterministic():
    a = derive_key("capture", (1, 2), {"x": "y"})
    b = derive_key("capture", (1, 2), {"x": "y"})
    assert a == b
    assert a.startswith(DERIVED_PREFIX)


def test_derive_key_sensitive_to_inputs():
    base = derive_key("capture", (1,), {})
    assert derive_key("capture", (2,), {}) != base
    assert derive_key("refund", (1,), {}) != base
    assert derive_key("capture", (1,), {}, scope="other") != base
    assert derive_key("capture", (1,), {"a": 1}) != base


def test_derive_key_kwarg_order_insensitive():
    assert derive_key("t", (), {"a": 1, "b": 2}) == derive_key("t", (), {"b": 2, "a": 1})


def test_is_derived():
    assert is_derived(derive_key("t"))
    assert not is_derived(new_key())


def test_fingerprint_stable_and_order_insensitive():
    f1 = fingerprint("tool", (1, "a"), {"x": 1, "y": 2})
    f2 = fingerprint("tool", (1, "a"), {"y": 2, "x": 1})
    assert f1 == f2
    assert len(f1) == 64


def test_fingerprint_type_tagged():
    # 1 and "1" must never share a fingerprint (the str() collision trap)
    assert fingerprint("t", (1,)) != fingerprint("t", ("1",))
    assert fingerprint("t", (True,)) != fingerprint("t", (1,))
    assert fingerprint("t", (None,)) != fingerprint("t", ("None",))


def test_fingerprint_distinguishes_tools_and_nesting():
    assert fingerprint("a", (1,)) != fingerprint("b", (1,))
    assert fingerprint("a", ({"x": [1, 2]},)) != fingerprint("a", ({"x": [1, 3]},))
    assert fingerprint("a", ((1, 2),)) != fingerprint("a", ([1, 2],))
