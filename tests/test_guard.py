import threading
import time

import pytest

from idemkey import (
    DedupStore,
    FingerprintMismatchError,
    InflightTimeoutError,
    ResultNotReplayableError,
    RetryableError,
    VerificationError,
    guard,
    new_key,
)


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def nosleep(_):
    pass


def test_no_key_executes_once_no_retry(store):
    calls = []

    @guard(store=store, sleep=nosleep)
    def flaky():
        calls.append(1)
        raise TimeoutError("slow")

    with pytest.raises(TimeoutError):
        flaky()
    assert len(calls) == 1  # fail loud, no retry, no store row
    assert store.stats()["total"] == 0


def test_retry_on_timeout_then_success(store):
    calls = []

    @guard(store=store, sleep=nosleep, max_retries=3)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("slow")
        return {"ok": True}

    assert flaky(idemkey="k1") == {"ok": True}
    assert len(calls) == 3
    assert store.get("k1")["status"] == "completed"


def test_retryable_error_retried_by_default(store):
    calls = []

    @guard(store=store, sleep=nosleep, max_retries=1)
    def f():
        calls.append(1)
        raise RetryableError("try again")

    with pytest.raises(RetryableError):
        f(idemkey="k1")
    assert len(calls) == 2


def test_non_retryable_no_second_execution(store):
    calls = []

    @guard(store=store, sleep=nosleep, max_retries=5)
    def f():
        calls.append(1)
        raise ValueError("bad args")

    with pytest.raises(ValueError):
        f(idemkey="k1")
    assert len(calls) == 1
    row = store.get("k1")
    assert row["status"] == "failed"
    assert "ValueError" in row["error"]


def test_custom_retry_on(store):
    calls = []

    @guard(store=store, sleep=nosleep, max_retries=2, retry_on=ValueError)
    def f():
        calls.append(1)
        raise ValueError("retry me")

    with pytest.raises(ValueError):
        f(idemkey="k1")
    assert len(calls) == 3


def test_retry_exhaustion_marks_failed(store):
    @guard(store=store, sleep=nosleep, max_retries=2)
    def f():
        raise TimeoutError("down")

    with pytest.raises(TimeoutError):
        f(idemkey="k1")
    assert store.get("k1")["status"] == "failed"


def test_duplicate_replays_result_fn_called_once(store):
    calls = []

    @guard(store=store, sleep=nosleep)
    def charge(amount):
        calls.append(amount)
        return {"charged": amount}

    r1 = charge(100, idemkey="ord-1")
    r2 = charge(100, idemkey="ord-1")
    assert r1 == r2 == {"charged": 100}
    assert calls == [100]


def test_same_key_different_args_raises(store):
    @guard(store=store, sleep=nosleep)
    def charge(amount):
        return {"charged": amount}

    charge(100, idemkey="ord-1")
    with pytest.raises(FingerprintMismatchError):
        charge(200, idemkey="ord-1")  # not a retry: different operation


def test_failed_then_retry_same_key_reexecutes(store):
    calls = []

    @guard(store=store, sleep=nosleep, max_retries=0)
    def f():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("first attempt dies")
        return "recovered"

    with pytest.raises(TimeoutError):
        f(idemkey="k1")
    assert f(idemkey="k1") == "recovered"
    assert len(calls) == 2
    assert store.get("k1")["status"] == "completed"


def test_failed_then_retry_with_different_args_raises(store):
    @guard(store=store, sleep=nosleep, max_retries=0)
    def charge(amount):
        raise TimeoutError("died")

    with pytest.raises(TimeoutError):
        charge(100, idemkey="ord-1")
    with pytest.raises(FingerprintMismatchError):
        charge(999, idemkey="ord-1")  # arg drift on the "retry" path


def test_verify_success(store):
    seen = []

    @guard(store=store, sleep=nosleep, verify=lambda r: seen.append(r) or True)
    def write():
        return "row-1"

    assert write(idemkey="k1") == "row-1"
    assert seen == ["row-1"]


def test_verify_false_raises_and_marks_failed(store):
    @guard(store=store, sleep=nosleep, verify=lambda r: False)
    def write():
        return "row-1"

    with pytest.raises(VerificationError):
        write(idemkey="k1")
    assert store.get("k1")["status"] == "failed"


def test_verify_raises_becomes_verification_error(store):
    def bad_verify(r):
        raise RuntimeError("checker exploded")

    @guard(store=store, sleep=nosleep, verify=bad_verify)
    def write():
        return "row-1"

    with pytest.raises(VerificationError):
        write(idemkey="k1")


def test_concurrent_same_key_single_execution(store):
    calls = []

    @guard(store=store, sleep=nosleep)
    def slow(x):
        calls.append(x)
        time.sleep(0.3)
        return {"x": x}

    results = []

    def caller():
        results.append(slow(7, idemkey="race"))

    threads = [threading.Thread(target=caller) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [7]  # executed exactly once
    assert results == [{"x": 7}] * 5  # everyone got the replayed result


def test_concurrent_loser_sees_winner_failure(store):
    release = threading.Event()

    @guard(store=store, sleep=nosleep, max_retries=0, inflight_timeout=10)
    def doomed():
        release.wait(5)
        raise ValueError("winner failed")

    errors = []

    def caller():
        try:
            doomed(idemkey="k1")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=caller)
    t1.start()
    time.sleep(0.15)  # let t1 claim
    t2 = threading.Thread(target=caller)
    t2.start()
    time.sleep(0.15)
    release.set()
    t1.join()
    t2.join()
    kinds = sorted(type(e).__name__ for e in errors)
    assert kinds == ["PreviousAttemptFailedError", "ValueError"]


def test_inflight_timeout(store):
    # Simulate a wedged owner: pending row with a fresh heartbeat.
    store.claim("stuck", "f", "fp", "default", "someone-else", claim_ttl=3600)

    @guard(store=store, sleep=nosleep, inflight_timeout=0.05)
    def f():
        return 1

    with pytest.raises(InflightTimeoutError):
        f(idemkey="stuck")


def test_key_arg_popped_before_call(store):
    received = {}

    @guard(store=store, sleep=nosleep)
    def f(a, b=1):
        received["kwargs"] = {"a": a, "b": b}
        return a + b

    assert f(1, b=2, idemkey="k1") == 3
    assert received["kwargs"] == {"a": 1, "b": 2}  # idemkey never reached fn


def test_decorator_forms(store):
    @guard(key="fixed", store=store, sleep=nosleep)
    def a():
        return "a"

    assert a() == "a"
    assert a() == "a"  # duplicate replay

    def b():
        return "b"

    wb = guard(b, store=store, sleep=nosleep)
    assert wb(idemkey="bk") == "b"

    @guard  # bare decorator, no key -> single execution
    def c():
        return "c"

    assert c() == "c"


def test_derive_opt_in_dedups_across_instances(store):
    calls = []

    @guard(store=store, sleep=nosleep, derive=True, scope="orders")
    def ensure(x):
        calls.append(x)
        return f"ensured-{x}"

    assert ensure("w1") == "ensured-w1"
    # a second wrapper instance with identical (tool, args, scope) derives the
    # same key -> deduped even though it is a different function object
    def ensure_clone(x):
        calls.append("clone:" + x)
        return f"ensured-{x}"

    ensure_clone.__name__ = "ensure"
    wrapped_clone = guard(ensure_clone, store=store, sleep=nosleep, derive=True, scope="orders")
    assert wrapped_clone("w1") == "ensured-w1"
    assert calls == ["w1"]  # clone never executed


def test_fingerprint_policy_warn_replays(store):
    @guard(store=store, sleep=nosleep, fingerprint_policy="warn")
    def charge(amount):
        return {"charged": amount}

    charge(100, idemkey="ord-1")
    with pytest.warns(UserWarning):
        assert charge(200, idemkey="ord-1") == {"charged": 100}


def test_unreplayable_result_raises_on_duplicate(store):
    @guard(store=store, sleep=nosleep, result_max_bytes=10)
    def big():
        return "x" * 1000

    assert big(idemkey="k1") == "x" * 1000  # primary call still succeeds
    with pytest.raises(ResultNotReplayableError):
        big(idemkey="k1")


def test_unknown_option_rejected():
    with pytest.raises(TypeError):
        guard(lambda: 1, bogus_option=True)


def test_canonical_args_no_false_positive(store):
    # f(1) and f(1, b=2) are the same logical call; a retry that merely spells
    # the arguments differently must replay, not raise.
    calls = []

    @guard(store=store, sleep=nosleep)
    def f(a, b=2):
        calls.append((a, b))
        return a + b

    assert f(1, idemkey="k1") == 3
    assert f(1, b=2, idemkey="k1") == 3
    assert calls == [(1, 2)]


def test_heartbeat_prevents_mid_execution_steal(store):
    release = threading.Event()

    @guard(store=store, sleep=nosleep, claim_ttl=0.2)
    def slow():
        release.wait(5)
        return "done"

    t = threading.Thread(target=lambda: slow(idemkey="lk"))
    t.start()
    time.sleep(0.45)  # well past claim_ttl: without heartbeat the claim is stale
    try:
        out, _ = store.claim("lk", "slow", "fp", "default", "thief", claim_ttl=0.2)
        assert out == "inflight", f"claim was stealable mid-execution (got {out})"
    finally:
        release.set()
        t.join()


def test_new_key_integration(store):
    @guard(store=store, sleep=nosleep)
    def f():
        return 42

    k = new_key()
    assert f(idemkey=k) == 42
    assert f(idemkey=k) == 42  # replay, no re-execution
