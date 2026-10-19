"""SQLite-backed dedup store.

One row per idempotency key, with an atomic claim protocol:

  * ``INSERT OR IGNORE`` is the claim. The winner executes; losers either replay
    the winner's stored result (completed), wait for the winner (pending), or
    reclaim the row (failed, or pending with a stale heartbeat).
  * Statuses: ``pending -> completed | failed``. ``failed`` rows are reclaimable
    so an agent retrying with the same key after a failure re-executes instead
    of replaying the failure.

Single-file SQLite in WAL mode: zero dependencies, survives agent crashes
(an in-memory store that forgets on crash is a decoration, not a dedup store).
Thread-safe via one connection + a lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS idem_keys (
    key         TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'default',
    status      TEXT NOT NULL,              -- pending | completed | failed
    result_json TEXT,                       -- replay payload for completed rows
    replayable  INTEGER NOT NULL DEFAULT 1,
    error       TEXT,                       -- last failure, human readable
    attempts    INTEGER NOT NULL DEFAULT 0, -- executions started under this key
    owner       TEXT,                       -- hostname:pid of the claim holder
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    heartbeat   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_status ON idem_keys(status);
CREATE INDEX IF NOT EXISTS idx_idem_updated ON idem_keys(updated_at);
"""

DEFAULT_TTL_SECONDS = 24 * 3600  # keys live 24h by default (Stripe parity)


class StoreCorruptedError(Exception):
    """Raised when the SQLite file is unreadable/corrupt."""


class DedupStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = None
        self._pid = None
        self._closed = False
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            # Restrictive perms on directories we create; pre-existing
            # directories are left exactly as the operator set them.
            if not os.path.isdir(parent):
                os.makedirs(parent, mode=0o700, exist_ok=True)
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
        with self._lock:
            self._open_locked()
        # Best-effort: the store can hold sensitive tool results — keep the
        # file owner-only. (SQLite's -wal/-shm sidecars are transient; the
        # main file is what matters.)
        if path != ":memory:":
            for p in (path, path + "-wal", path + "-shm"):
                try:
                    if os.path.exists(p):
                        os.chmod(p, 0o600)
                except OSError:
                    pass

    def _open_locked(self):
        """(Re)open the SQLite connection. Call with ``self._lock`` held."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self._conn = None
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        except sqlite3.Error as exc:
            raise StoreCorruptedError(f"cannot open dedup store {self.path!r}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.executescript(SCHEMA)
            conn.commit()
        except sqlite3.DatabaseError as exc:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass
            raise StoreCorruptedError(f"dedup store {self.path!r} is corrupt: {exc}") from exc
        self._conn = conn
        self._pid = os.getpid()

    def _ensure_open(self):
        """Call with ``self._lock`` held. Reconnects after fork().

        A SQLite connection must not be used across ``fork()`` — the child
        would share the parent's file description and risk database
        corruption. Detect the pid change and reopen transparently.
        """
        if self._closed:
            raise StoreCorruptedError(f"dedup store {self.path!r} is closed")
        if self._conn is None or os.getpid() != self._pid:
            self._open_locked()

    # -- internal ---------------------------------------------------------
    def _get_locked(self, key: str):
        row = self._conn.execute("SELECT * FROM idem_keys WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def get(self, key: str):
        """Return the row dict for key, or None."""
        with self._lock:
            self._ensure_open()
            return self._get_locked(key)

    # -- claim protocol ----------------------------------------------------
    def claim(self, key, tool, fingerprint, scope, owner, claim_ttl):
        """Try to claim key for execution.

        Returns ``(outcome, row)`` where outcome is one of:
          * ``"claimed"``   — we own the key, execute.
          * ``"duplicate"`` — row exists with status completed|failed; caller
                              decides (replay result / reclaim for retry).
          * ``"inflight"``  — another live owner holds it; caller should wait.
        """
        now = time.time()
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO idem_keys
                   (key, tool, fingerprint, scope, status, attempts, owner,
                    created_at, updated_at, heartbeat)
                   VALUES (?, ?, ?, ?, 'pending', 1, ?, ?, ?, ?)""",
                (key, tool, fingerprint, scope, owner, now, now, now),
            )
            self._conn.commit()
            if cur.rowcount == 1:
                return "claimed", self._get_locked(key)
            row = self._get_locked(key)
            if row is None:  # lost a race with purge; treat as claimable next call
                return "inflight", None
            if row["status"] in ("completed", "failed"):
                return "duplicate", row
            # pending: reclaim if the owner's heartbeat went stale (owner died)
            if now - row["heartbeat"] > claim_ttl:
                cur = self._conn.execute(
                    """UPDATE idem_keys
                       SET owner = ?, heartbeat = ?, updated_at = ?,
                           attempts = attempts + 1
                       WHERE key = ? AND status = 'pending' AND heartbeat <= ?""",
                    (owner, now, now, key, now - claim_ttl),
                )
                self._conn.commit()
                if cur.rowcount == 1:
                    return "claimed", self._get_locked(key)
            return "inflight", row

    def reclaim_failed(self, key, owner):
        """Take over a failed row for a retry. Atomic; exactly one winner."""
        now = time.time()
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                """UPDATE idem_keys
                   SET status = 'pending', owner = ?, heartbeat = ?,
                       updated_at = ?, attempts = attempts + 1, error = NULL
                   WHERE key = ? AND status = 'failed'""",
                (owner, now, now, key),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def heartbeat(self, key, owner):
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "UPDATE idem_keys SET heartbeat = ?, updated_at = ? WHERE key = ? AND owner = ?",
                (time.time(), time.time(), key, owner),
            )
            self._conn.commit()

    def complete(self, key, result_json, replayable=True):
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                """UPDATE idem_keys
                   SET status = 'completed', result_json = ?, replayable = ?,
                       error = NULL, updated_at = ?, heartbeat = ?
                   WHERE key = ?""",
                (result_json, 1 if replayable else 0, time.time(), time.time(), key),
            )
            self._conn.commit()

    def fail(self, key, error):
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "UPDATE idem_keys SET status = 'failed', error = ?, updated_at = ? WHERE key = ?",
                (error, time.time(), key),
            )
            self._conn.commit()

    def wait_until_settled(self, key, timeout, poll_interval=0.05):
        """Block until the row leaves 'pending'. Returns the settled row.

        Raises ``TimeoutError`` on timeout, ``KeyError`` if the row vanished
        (e.g. purged mid-wait).
        """
        deadline = time.monotonic() + timeout
        while True:
            row = self.get(key)
            if row is None:
                raise KeyError(f"idempotency key {key!r} vanished while waiting")
            if row["status"] != "pending":
                return row
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for key {key!r} to settle")
            time.sleep(poll_interval)

    # -- maintenance --------------------------------------------------------
    def purge(self, older_than_seconds=DEFAULT_TTL_SECONDS):
        """Delete rows last updated longer ago than the TTL. Returns count."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute("DELETE FROM idem_keys WHERE updated_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def stats(self):
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM idem_keys GROUP BY status"
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) AS n FROM idem_keys").fetchone()["n"]
        by_status = {r["status"]: r["n"] for r in rows}
        return {"total": total, "by_status": by_status, "path": self.path}

    def list(self, status=None, limit=50):
        with self._lock:
            self._ensure_open()
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM idem_keys WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM idem_keys ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def close(self):
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001, S110
                    pass
                self._conn = None
