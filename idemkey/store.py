"""SQLite-backed dedup store.

One row per idempotency key, with an atomic claim protocol:

  * ``INSERT OR IGNORE`` is the claim. The winner executes; losers either replay
    the winner's stored result (completed), wait for the winner (pending), or
    reclaim the row (failed, or pending with a stale heartbeat).
  * Statuses: ``pending -> completed | failed | unknown``. ``failed`` rows are
    reclaimable so an agent retrying with the same key after a failure
    re-executes instead of replaying the failure. ``unknown`` rows (the owner
    was cancelled mid-call; the write may have landed) are *not* reclaimable:
    a human or a ``ttl`` must clear them.

Single-file SQLite in WAL mode: zero dependencies, survives agent crashes
(an in-memory store that forgets on crash is a decoration, not a dedup store).
Thread-safe via one connection + a lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import weakref
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS idem_keys (
    key         TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'default',
    status      TEXT NOT NULL,              -- pending | completed | failed | unknown
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

SETTLED = ("completed", "failed", "unknown")

# Settling writes (complete/fail/unknown) happen *after* the tool's side
# effect. Losing one to a transient SQLITE_BUSY (another process held the
# write lock past busy_timeout) would leave the row pending and the caller
# with an exception for a write that succeeded — so they are retried.
WRITE_RETRIES = 6
WRITE_RETRY_BASE = 0.05


def _is_busy(exc):
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


class StoreCorruptedError(Exception):
    """Raised when the SQLite file is unreadable/corrupt."""


class StoreBusyError(sqlite3.OperationalError):
    """The store file stayed locked by another program for the whole retry
    budget (about half a minute). The tool was NOT started for a claim that
    fails this way. Subclasses ``sqlite3.OperationalError`` so older
    ``except sqlite3.OperationalError`` code keeps working."""


def _busy_error(path):
    return StoreBusyError(
        f"the key store {path!r} is locked by another program (database is locked); "
        f"close whatever else has it open (a DB browser, a backup, a stuck process) and retry"
    )


_STORES: Any = weakref.WeakSet()


def _reset_locks_after_fork():
    # A forked child inherits each store's locks in whatever state they were in
    # at fork time. If the heartbeat thread (which does not exist in the child)
    # held one, the child would wait on it forever. Give every store fresh locks;
    # the SQLite connection itself is reopened on first use (see _ensure_open).
    for store in list(_STORES):
        store._lock = threading.Lock()
        store._settled_cv = threading.Condition()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_locks_after_fork)


class DedupStore:
    def __init__(self, path: str, durability: str = "full"):
        if durability not in ("full", "normal"):
            raise ValueError("durability must be 'full' (default) or 'normal'")
        self.path = path
        # "full": every claim/settle is fsynced before it returns (a power
        # cut cannot lose a recorded key). "normal": WAL is fsynced at
        # checkpoints only - several times faster, survives a process crash
        # (SIGKILL) exactly as well, but the last few records can be lost on
        # power failure/OS crash, which re-opens the duplicate window.
        self.durability = durability
        self._lock = threading.Lock()
        self._settled_cv = threading.Condition()
        self._settle_seq = 0
        self._conn: Any = None
        self._pid = None
        self._closed = False
        _STORES.add(self)
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            # Restrictive perms on directories we create; pre-existing
            # directories are left exactly as the operator set them.
            if not os.path.isdir(parent):
                try:
                    os.makedirs(parent, mode=0o700, exist_ok=True)
                except OSError as exc:
                    raise StoreCorruptedError(
                        f"cannot create the folder for the dedup store {self.path!r}: "
                        f"{exc.strerror or exc}") from exc
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
            if self.durability == "normal" and self.path != ":memory:":
                conn.execute("PRAGMA synchronous=NORMAL;")
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

    def find(self, key: str):
        """Rows whose *user* key is ``key``, across namespaces.

        Stored keys are ``scope|tool|prefix|key`` (see ``idemkey.guard``); the
        CLI and tests use this so an operator can type the key the agent used.
        An exact stored-key match (raw 0.1.x layout, or a full namespaced key)
        is returned on its own.
        """
        with self._lock:
            self._ensure_open()
            exact = self._get_locked(key)
            if exact is not None:
                return [exact]
            esc = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self._conn.execute(
                "SELECT * FROM idem_keys WHERE key LIKE ? ESCAPE '\\' ORDER BY updated_at DESC",
                ("%|" + esc,),
            ).fetchall()
        out = []
        for r in rows:
            parts = r["key"].split("|", 3)
            if len(parts) == 4 and parts[3] == key:
                out.append(dict(r))
        return out

    # -- claim protocol ----------------------------------------------------
    def claim(self, key, tool, fingerprint, scope, owner, claim_ttl, ttl=None, alt_fingerprints=()):
        """:meth:`_claim` retried on transient SQLITE_BUSY (many racing
        processes can outlast ``busy_timeout``; a claim is idempotent to
        retry because every statement in it is atomic and re-checks state)."""
        for attempt in range(WRITE_RETRIES):
            try:
                return self._claim(key, tool, fingerprint, scope, owner, claim_ttl, ttl, alt_fingerprints)
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                if attempt == WRITE_RETRIES - 1:
                    raise _busy_error(self.path) from exc
                with self._lock:
                    try:
                        self._conn.rollback()
                    except Exception:  # noqa: BLE001, S110
                        pass
                time.sleep(WRITE_RETRY_BASE * (2 ** attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def _claim(self, key, tool, fingerprint, scope, owner, claim_ttl, ttl=None, alt_fingerprints=()):
        """Try to claim key for execution.

        Returns ``(outcome, row)`` where outcome is one of:
          * ``"claimed"``   — we own the key, execute.
          * ``"duplicate"`` — row exists with status completed|failed; caller
                              decides (replay result / reclaim for retry).
          * ``"inflight"``  — another live owner holds it; caller should wait.
                              A stale claim is only taken over by a call with
                              the *same* fingerprint; a different call gets
                              ``"inflight"`` with the row so it can reject it.

        ``ttl`` (seconds, optional): a settled row (completed|failed) last
        updated more than ``ttl`` seconds ago is treated as expired and
        atomically re-claimed as a brand-new operation (Stripe semantics: a
        key reused after it was pruned starts a new request). ``None`` means
        settled rows never expire on their own (only ``purge`` removes them).
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
            if row["status"] in SETTLED:
                if ttl is not None and now - row["updated_at"] > ttl:
                    # Expired: overwrite in place as a fresh claim. The
                    # updated_at guard makes this a compare-and-swap, so of
                    # several racers exactly one wins.
                    cur = self._conn.execute(
                        """UPDATE idem_keys
                           SET tool = ?, fingerprint = ?, scope = ?,
                               status = 'pending', result_json = NULL,
                               replayable = 1, error = NULL, attempts = 1,
                               owner = ?, created_at = ?, updated_at = ?,
                               heartbeat = ?
                           WHERE key = ? AND status = ? AND updated_at = ?""",
                        (tool, fingerprint, scope, owner, now, now, now,
                         key, row["status"], row["updated_at"]),
                    )
                    self._conn.commit()
                    if cur.rowcount == 1:
                        return "claimed", self._get_locked(key)
                    row = self._get_locked(key)
                    if row is None:
                        return "inflight", None
                    if row["status"] == "pending":
                        return "inflight", row
                return "duplicate", row
            # pending: reclaim if the owner's heartbeat went stale (owner died)
            if now - row["heartbeat"] > claim_ttl:
                cur = self._conn.execute(
                    """UPDATE idem_keys
                       SET owner = ?, heartbeat = ?, updated_at = ?,
                           attempts = attempts + 1
                       WHERE key = ? AND status = 'pending' AND heartbeat <= ?
                         AND fingerprint IN (""" + ",".join("?" * (1 + len(alt_fingerprints))) + ")",
                    (owner, now, now, key, now - claim_ttl, fingerprint, *alt_fingerprints),
                )
                self._conn.commit()
                if cur.rowcount == 1:
                    return "claimed", self._get_locked(key)
            return "inflight", row

    def reclaim_failed(self, key, owner):
        """Take over a failed row for a retry. Atomic; exactly one winner."""
        return self.reclaim(key, owner, "failed")

    def reclaim(self, key, owner, from_status):
        """Take over a ``failed`` or ``unknown`` row. Atomic; one winner."""
        if from_status not in ("failed", "unknown"):
            raise ValueError("can only reclaim failed or unknown rows")
        now = time.time()
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                """UPDATE idem_keys
                   SET status = 'pending', owner = ?, heartbeat = ?,
                       updated_at = ?, attempts = attempts + 1, error = NULL
                   WHERE key = ? AND status = ?""",
                (owner, now, now, key, from_status),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def heartbeat(self, key, owner):
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "UPDATE idem_keys SET heartbeat = ?, updated_at = ? "
                "WHERE key = ? AND owner = ? AND status = 'pending'",
                (time.time(), time.time(), key, owner),
            )
            self._conn.commit()

    def _settle(self, sql_unfenced, sql_fenced, params, key, owner):
        """Run a settling UPDATE, fenced by owner when given, retrying on BUSY."""
        if owner is None:
            sql, full = sql_unfenced, (*params, key)
        else:
            sql, full = sql_fenced, (*params, key, owner)
        for attempt in range(WRITE_RETRIES):
            try:
                with self._lock:
                    self._ensure_open()
                    cur = self._exec(sql, full)
                    self._conn.commit()
                    self._notify_settled()
                    return cur.rowcount == 1
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                if attempt == WRITE_RETRIES - 1:
                    raise _busy_error(self.path) from exc
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001, S110
                    pass
                time.sleep(WRITE_RETRY_BASE * (2 ** attempt))
        return False  # pragma: no cover

    def _notify_settled(self):
        with self._settled_cv:
            self._settle_seq += 1
            self._settled_cv.notify_all()

    def _exec(self, sql, params):
        # Single seam for writes (lets tests inject SQLITE_BUSY).
        return self._conn.execute(sql, params)

    def complete(self, key, result_json, replayable=True, owner=None):
        """Mark ``key`` completed with its replay payload.

        With ``owner`` set, the write is *fenced*: it only applies while the
        row is still ``pending`` and held by that owner. Returns ``True`` if
        the row was updated, ``False`` if the claim had been lost (a stale
        claim was taken over by another owner) — in which case the other
        owner's outcome is left untouched. Retried on SQLITE_BUSY.
        """
        now = time.time()
        set_ = """UPDATE idem_keys
                  SET status = 'completed', result_json = ?, replayable = ?,
                      error = NULL, updated_at = ?, heartbeat = ?
                  WHERE key = ?"""
        return self._settle(
            set_, set_ + " AND owner = ? AND status = 'pending'",
            (result_json, 1 if replayable else 0, now, now), key, owner,
        )

    def fail(self, key, error, owner=None):
        """Mark ``key`` failed (reclaimable). Fenced like :meth:`complete`."""
        set_ = "UPDATE idem_keys SET status = 'failed', error = ?, updated_at = ? WHERE key = ?"
        return self._settle(
            set_, set_ + " AND owner = ? AND status = 'pending'",
            (error, time.time()), key, owner,
        )

    def mark_unknown(self, key, error, owner=None):
        """Mark ``key`` unknown: the write may have landed. NOT reclaimable.

        A same-key call raises instead of re-executing; clear the row with
        ``idemkey forget KEY`` after checking the downstream system (or let a
        ``ttl`` expire it). Fenced like :meth:`complete`.
        """
        set_ = "UPDATE idem_keys SET status = 'unknown', error = ?, updated_at = ? WHERE key = ?"
        return self._settle(
            set_, set_ + " AND owner = ? AND status = 'pending'",
            (error, time.time()), key, owner,
        )

    def delete(self, key, allow_pending=False):
        """Remove one key so its next use re-executes. Returns ``True`` if deleted.

        This is the manual escape hatch for an "unknown outcome" key after
        you have checked the downstream system by hand. A ``pending`` row (an
        execution may be in flight) is only deleted with ``allow_pending=True``;
        the status check and the delete are one atomic statement.
        """
        with self._lock:
            self._ensure_open()
            if allow_pending:
                cur = self._conn.execute("DELETE FROM idem_keys WHERE key = ?", (key,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM idem_keys WHERE key = ? AND status != 'pending'", (key,)
                )
            self._conn.commit()
            self._notify_settled()
            return cur.rowcount == 1

    def wait_until_settled(self, key, timeout, poll_interval=0.05):
        """Block until the row leaves 'pending'. Returns the settled row.

        Same-process waiters are woken the instant the owner settles (no
        polling latency); waiters in *other* processes poll with a backoff
        from 2 ms up to ``poll_interval``.

        Raises ``TimeoutError`` on timeout, ``KeyError`` if the row vanished
        (e.g. purged mid-wait).
        """
        deadline = time.monotonic() + timeout
        delay = min(0.002, poll_interval)
        while True:
            with self._settled_cv:
                seq = self._settle_seq
            row = self.get(key)
            if row is None:
                raise KeyError(f"idempotency key {key!r} vanished while waiting")
            if row["status"] != "pending":
                return row
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for key {key!r} to settle")
            with self._settled_cv:
                if self._settle_seq == seq:  # nothing settled since we looked
                    self._settled_cv.wait(min(delay, remaining))
            delay = min(delay * 2, poll_interval)

    # -- atomic (transactional) execution ---------------------------------
    def begin_atomic(self):
        """Open a dedicated connection with a write transaction (BEGIN
        IMMEDIATE) for :func:`idemkey.guard`'s ``atomic=`` mode.

        The tool's own writes (to tables in this same database file) and the
        key's ``completed`` record are committed by ONE ``COMMIT`` in
        :meth:`commit_atomic`, so a crash leaves both or neither.
        """
        if self.path == ":memory:":
            raise ValueError("atomic mode needs a file-backed store, not ':memory:'")
        with self._lock:
            self._ensure_open()
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None,
                               check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            if self.durability == "normal":
                conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            conn.close()
            raise
        return conn

    def commit_atomic(self, conn, key, owner, result_json, replayable=True):
        """Record ``completed`` inside ``conn``'s transaction and COMMIT.

        Fenced: if this execution no longer owns a pending claim, the whole
        transaction (including the tool's own writes) is rolled back and
        ``False`` is returned - a stale owner's effect is undone, not kept.
        """
        try:
            now = time.time()
            cur = conn.execute(
                """UPDATE idem_keys
                   SET status = 'completed', result_json = ?, replayable = ?,
                       error = NULL, updated_at = ?, heartbeat = ?
                   WHERE key = ? AND owner = ? AND status = 'pending'""",
                (result_json, 1 if replayable else 0, now, now, key, owner),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        except BaseException:
            self.rollback_atomic(conn)
            raise
        finally:
            conn.close()
        self._notify_settled()
        return True

    @staticmethod
    def rollback_atomic(conn):
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass

    # -- maintenance --------------------------------------------------------
    def purge(self, older_than_seconds=DEFAULT_TTL_SECONDS, include_stale_pending=False,
              dry_run=False):
        """Delete settled rows last updated longer ago than the TTL. Returns count.

        ``pending`` rows are never deleted by default — one may be a live
        execution, and deleting it would let a duplicate run. With
        ``include_stale_pending=True``, pending rows whose *heartbeat* is older
        than the cutoff (owner presumed dead) are deleted too. ``dry_run=True``
        deletes nothing and returns how many rows *would* go.
        """
        if not (isinstance(older_than_seconds, (int, float)) and older_than_seconds > 0):
            raise ValueError("purge older_than_seconds must be a positive number")
        cutoff = time.time() - older_than_seconds
        with self._lock:
            self._ensure_open()
            verb = "SELECT COUNT(*) AS n FROM" if dry_run else "DELETE FROM"
            if include_stale_pending:
                cur = self._conn.execute(
                    f"""{verb} idem_keys WHERE
                       (status != 'pending' AND updated_at < ?)
                       OR (status = 'pending' AND heartbeat < ?)""",
                    (cutoff, cutoff),
                )
            else:
                cur = self._conn.execute(
                    f"{verb} idem_keys WHERE status != 'pending' AND updated_at < ?",
                    (cutoff,),
                )
            if dry_run:
                return cur.fetchone()["n"]
            self._conn.commit()
            return cur.rowcount

    def migrate_legacy_keys(self, dry_run=False):
        """Rewrite 0.1.x rows (stored under the raw key) to the namespaced
        layout ``scope|tool||key`` that 0.2+ looks up, so a store written by
        0.1.x keeps replaying without ``namespace=False``.

        0.1.x had no ``key_prefix``, so the prefix part is empty. Rows already
        in namespaced form, rows whose scope/tool contain ``|``, and rows whose
        new name is already taken are left alone (and counted). Status,
        result and fingerprint are not touched. One transaction: all or
        nothing. Returns ``{"migrated", "already_namespaced", "skipped"}``.
        """
        with self._lock:
            self._ensure_open()
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute("SELECT key, tool, scope FROM idem_keys").fetchall()
                out = {"migrated": 0, "already_namespaced": 0, "skipped": 0}
                for r in rows:
                    key, tool, scope = r["key"], r["tool"], r["scope"]
                    parts = key.split("|", 3)
                    if len(parts) == 4 and parts[0] == scope and parts[1] == tool:
                        out["already_namespaced"] += 1
                        continue
                    new = f"{scope}|{tool}||{key}"
                    if "|" in scope or "|" in tool or conn.execute(
                            "SELECT 1 FROM idem_keys WHERE key = ?", (new,)).fetchone():
                        out["skipped"] += 1
                        continue
                    if not dry_run:
                        conn.execute("UPDATE idem_keys SET key = ? WHERE key = ?", (new, key))
                    out["migrated"] += 1
                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()
                return out
            except BaseException:
                conn.rollback()
                raise

    def quick_check(self):
        """SQLite's own corruption check. Returns ``[]`` when the file is sound,
        otherwise the problems it reported (plain strings)."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute("PRAGMA quick_check").fetchall()
        msgs = [str(r[0]) for r in rows]
        return [] if msgs == ["ok"] else msgs

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

    def iter_rows(self, status=None, batch=500):
        """Yield every row (oldest first) in batches, so a large store can be
        exported without loading it into memory or holding the lock."""
        last = ("", "")
        while True:
            with self._lock:
                self._ensure_open()
                if status:
                    rows = self._conn.execute(
                        "SELECT * FROM idem_keys WHERE status = ? AND (created_at, key) > (?, ?) "
                        "ORDER BY created_at, key LIMIT ?",
                        (status, last[0] or 0, last[1], batch)).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT * FROM idem_keys WHERE (created_at, key) > (?, ?) "
                        "ORDER BY created_at, key LIMIT ?",
                        (last[0] or 0, last[1], batch)).fetchall()
            if not rows:
                return
            for r in rows:
                yield dict(r)
            last = (rows[-1]["created_at"], rows[-1]["key"])

    def close(self):
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001, S110
                    pass
                self._conn = None
