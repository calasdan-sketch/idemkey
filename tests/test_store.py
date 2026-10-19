import threading
import time

import pytest

from idemkey.store import DedupStore, StoreCorruptedError


@pytest.fixture()
def store(tmp_path):
    s = DedupStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_claim_then_duplicate(store):
    out, row = store.claim("k1", "tool", "fp", "default", "owner1", 300)
    assert out == "claimed"
    store.complete("k1", '{"ok": true}')
    out, row = store.claim("k1", "tool", "fp", "default", "owner2", 300)
    assert out == "duplicate"
    assert row["status"] == "completed"


def test_second_claim_while_pending_is_inflight(store):
    out, _ = store.claim("k1", "tool", "fp", "default", "owner1", 300)
    assert out == "claimed"
    out, row = store.claim("k1", "tool", "fp", "default", "owner2", 300)
    assert out == "inflight"
    assert row["owner"] == "owner1"


def test_stale_claim_is_stolen(store):
    out, _ = store.claim("k1", "tool", "fp", "default", "owner1", claim_ttl=0.05)
    assert out == "claimed"
    time.sleep(0.08)  # heartbeat goes stale
    out, row = store.claim("k1", "tool", "fp", "default", "owner2", claim_ttl=0.05)
    assert out == "claimed"
    assert row["owner"] == "owner2"
    assert row["attempts"] == 2


def test_fresh_claim_not_stolen(store):
    store.claim("k1", "tool", "fp", "default", "owner1", claim_ttl=300)
    out, _ = store.claim("k1", "tool", "fp", "default", "owner2", claim_ttl=300)
    assert out == "inflight"


def test_reclaim_failed_is_atomic(store):
    store.claim("k1", "tool", "fp", "default", "o1", 300)
    store.fail("k1", "boom")
    assert store.reclaim_failed("k1", "o2") is True
    assert store.reclaim_failed("k1", "o3") is False  # already pending again
    row = store.get("k1")
    assert row["status"] == "pending" and row["owner"] == "o2" and row["error"] is None


def test_complete_and_fail_roundtrip(store):
    store.claim("k1", "tool", "fp", "default", "o1", 300)
    store.heartbeat("k1", "o1")  # must not raise
    store.complete("k1", '{"n": 1}')
    row = store.get("k1")
    assert row["status"] == "completed" and row["result_json"] == '{"n": 1}'
    assert row["replayable"] == 1


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


def test_wait_until_settled_happy_path(store):
    store.claim("k1", "tool", "fp", "default", "o1", 300)

    def finisher():
        time.sleep(0.1)
        store.complete("k1", '{"done": true}')

    t = threading.Thread(target=finisher)
    t.start()
    row = store.wait_until_settled("k1", timeout=5)
    t.join()
    assert row["status"] == "completed"


def test_wait_until_settled_timeout(store):
    store.claim("k1", "tool", "fp", "default", "o1", 300)
    with pytest.raises(TimeoutError):
        store.wait_until_settled("k1", timeout=0.05, poll_interval=0.01)


def test_wait_until_settled_missing_key(store):
    with pytest.raises(KeyError):
        store.wait_until_settled("ghost", timeout=1)


def test_purge_old_only(store):
    s2_path = store.path
    store.claim("old", "t", "fp", "d", "o", 300)
    store.complete("old", None)
    # backdate the row
    with store._lock:
        store._conn.execute("UPDATE idem_keys SET updated_at = 0 WHERE key = 'old'")
        store._conn.commit()
    store.claim("new", "t", "fp", "d", "o", 300)
    n = store.purge(older_than_seconds=3600)
    assert n == 1
    assert store.get("old") is None
    assert store.get("new") is not None
    assert s2_path == store.path


def test_stats_and_list(store):
    store.claim("a", "t", "fp", "d", "o", 300)
    store.complete("a", None)
    store.claim("b", "t", "fp", "d", "o", 300)
    store.fail("b", "x")
    s = store.stats()
    assert s["total"] == 2
    assert s["by_status"] == {"completed": 1, "failed": 1}
    assert len(store.list(limit=10)) == 2
    assert len(store.list(status="failed")) == 1


def test_concurrent_claim_single_winner(store):
    winners = []

    def try_claim(i):
        out, _ = store.claim("k", "t", "fp", "d", f"o{i}", 300)
        if out == "claimed":
            winners.append(i)

    threads = [threading.Thread(target=try_claim, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_corrupt_store_raises(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not sqlite")
    with pytest.raises(StoreCorruptedError):
        DedupStore(str(bad))


# --- audit-phase hardening --------------------------------------------------


def test_store_file_is_owner_only(tmp_path):
    import os as _os
    import stat as _stat

    p = str(tmp_path / "sub" / "t.db")
    s = DedupStore(p)
    try:
        mode = _stat.S_IMODE(_os.stat(p).st_mode)
        assert mode == 0o600, f"store file perms {oct(mode)}, want 0o600"
        dmode = _stat.S_IMODE(_os.stat(str(tmp_path / "sub")).st_mode)
        assert dmode == 0o700, f"store dir perms {oct(dmode)}, want 0o700"
    finally:
        s.close()


def test_preexisting_parent_dir_perms_untouched(tmp_path):
    import os as _os
    import stat as _stat

    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    before = _stat.S_IMODE(_os.stat(str(parent)).st_mode)
    s = DedupStore(str(parent / "t.db"))
    try:
        # Whatever the operator set (modulo umask), we must not touch it.
        assert _stat.S_IMODE(_os.stat(str(parent)).st_mode) == before
    finally:
        s.close()


def test_fork_reconnects_without_corruption(tmp_path):
    """A child forked after the store was opened must not corrupt the DB."""
    import os as _os

    p = str(tmp_path / "t.db")
    s = DedupStore(p)
    s.claim("parent-key", "tool", "fp", "default", "parent-owner", 300)
    pid = _os.fork()
    if pid == 0:  # child
        try:
            s.claim("child-key", "tool", "fp", "default", "child-owner", 300)
            s.complete("child-key", '{"ok": true}')
            _os._exit(0)
        except Exception:  # noqa: BLE001
            _os._exit(1)
    _, status = _os.waitpid(pid, 0)
    assert _os.waitstatus_to_exitcode(status) == 0, "child failed after fork"
    try:
        # parent connection still healthy; child's row is visible
        row = s.get("child-key")
        assert row is not None and row["status"] == "completed"
        assert s.get("parent-key")["status"] == "pending"
    finally:
        s.close()


def test_pid_change_triggers_reconnect(tmp_path):
    import os as _os

    p = str(tmp_path / "t.db")
    s = DedupStore(p)
    try:
        s.claim("k1", "tool", "fp", "default", "o", 300)
        old_conn = s._conn
        s._pid = -1  # simulate a fork
        assert s.get("k1")["status"] == "pending"  # transparently reconnected
        assert s._conn is not old_conn
        assert s._pid == _os.getpid()
    finally:
        s.close()


def test_use_after_close_raises_clean_error(tmp_path):
    s = DedupStore(str(tmp_path / "t.db"))
    s.close()
    with pytest.raises(StoreCorruptedError, match="closed"):
        s.get("k1")


def test_guard_rejects_non_exception_retry_on(tmp_path):
    from idemkey import guard

    with pytest.raises(TypeError, match="retry_on must be exception classes"):
        guard(lambda: None, retry_on=(ValueError, "nope"), store=__import__("idemkey").DedupStore(str(tmp_path / "t.db")))
