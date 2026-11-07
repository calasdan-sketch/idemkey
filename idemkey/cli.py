"""Command-line inspection of the dedup store.

    idemkey log [--store PATH] [--status pending|completed|failed|unknown] [--limit N]
    idemkey show KEY [--store PATH]
    idemkey purge [--store PATH] [--older-than HOURS] [--include-stale-pending]
    idemkey stats [--store PATH]
    idemkey forget KEY [--store PATH]
    idemkey export [--store PATH] [--status S] [--no-results]    (JSON lines)
    idemkey doctor [--store PATH] [--claim-ttl SECONDS]          (exit 1 if attention needed)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .guard import DEFAULT_STORE_ENV, DEFAULT_STORE_PATH
from .store import DedupStore


def _resolve_store(path_arg):
    path = os.path.expanduser(path_arg or os.environ.get(DEFAULT_STORE_ENV, DEFAULT_STORE_PATH))
    if not os.path.exists(path):
        print(f"no dedup store at {path}", file=sys.stderr)
        raise SystemExit(1)
    return DedupStore(path)


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def _safe(text):
    """Escape anything non-printable so stored text can't drive the terminal.

    Keys and tool names may come from a model. Escape sequences, newlines or
    bidi overrides in them must never reach the operator's terminal raw.
    """
    text = "" if text is None else str(text)
    if text.isprintable() and not any(c in text for c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"):
        return text
    return text.encode("unicode_escape").decode("ascii")


def _lookup(store, key):
    rows = store.find(key)
    if not rows:
        print(f"key {key!r} not found", file=sys.stderr)
        raise SystemExit(1)
    if len(rows) > 1:
        print(
            f"key {key!r} exists in {len(rows)} namespaces; pass the full stored key:",
            file=sys.stderr,
        )
        for r in rows:
            print(f"  {_safe(r['key'])}", file=sys.stderr)
        raise SystemExit(3)
    return rows[0]


def _positive_int(text):
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if v < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return v


def _mid(text, width):
    """Shorten in the middle so the distinguishing end of a key stays visible."""
    if len(text) <= width:
        return text
    keep = width - 1
    return text[: keep // 2] + "~" + text[-(keep - keep // 2):]


def _row_json(r, results=True):
    out = {k: r[k] for k in ("key", "tool", "scope", "status", "attempts", "owner", "error",
                             "replayable", "fingerprint", "created_at", "updated_at", "heartbeat")}
    if results:
        out["result_json"] = r.get("result_json")
    return out


def cmd_log(args):
    store = _resolve_store(args.store)
    rows = store.list(status=args.status, limit=args.limit)
    if args.json:
        # ensure_ascii escapes every control and bidi character
        print(json.dumps([_row_json(r, results=False) for r in rows]))
        return
    if not rows:
        print("(no entries)")
        return
    print(f"{'KEY':<40} {'TOOL':<22} {'STATUS':<10} {'TRY':>3}  UPDATED")
    for r in rows:
        print(
            f"{_mid(_safe(r['key']), 40):<40} {_safe(r['tool']):<22.21} {_safe(r['status']):<10} "
            f"{r['attempts']:>3}  {_fmt_ts(r['updated_at'])}"
        )


def cmd_show(args):
    store = _resolve_store(args.store)
    out = dict(_lookup(store, args.key))
    for ts_field in ("created_at", "updated_at", "heartbeat"):
        out[ts_field + "_human"] = _fmt_ts(out[ts_field])
    if out.get("result_json"):
        try:
            out["result"] = json.loads(out["result_json"])
        except ValueError:
            out["result"] = out["result_json"]
    # ensure_ascii (default) escapes every control character.
    print(json.dumps(out, indent=2, default=str))


def _positive_hours(text):
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not v > 0 or v == float("inf"):
        raise argparse.ArgumentTypeError("--older-than must be a positive number of hours")
    return v


def cmd_purge(args):
    store = _resolve_store(args.store)
    n = store.purge(
        older_than_seconds=args.older_than * 3600,
        include_stale_pending=args.include_stale_pending,
    )
    extra = " (incl. stale pending)" if args.include_stale_pending else " (pending rows kept)"
    print(f"purged {n} entr{'y' if n == 1 else 'ies'} older than {args.older_than}h{extra}")


def cmd_export(args):
    """One JSON object per line (ensure_ascii: control/bidi characters escaped)."""
    store = _resolve_store(args.store)
    n = 0
    for r in store.iter_rows(status=args.status):
        sys.stdout.write(json.dumps(_row_json(r, results=not args.no_results)) + "\n")
        n += 1
    print(f"exported {n} row(s)", file=sys.stderr)


def cmd_doctor(args):
    """Health check for operators and CI. Exit 0 = nothing needs a human."""
    store = _resolve_store(args.store)
    now = time.time()
    problems, notes = [], []
    counts = {"pending_stale": 0, "pending_live": 0, "unknown": 0, "failed": 0, "not_replayable": 0}
    oldest = None
    for r in store.iter_rows():
        oldest = r["created_at"] if oldest is None else min(oldest, r["created_at"])
        if r["status"] == "pending":
            counts["pending_stale" if now - r["heartbeat"] > args.claim_ttl else "pending_live"] += 1
        elif r["status"] in ("unknown", "failed"):
            counts[r["status"]] += 1
        if r["status"] == "completed" and not r["replayable"]:
            counts["not_replayable"] += 1
    if counts["unknown"]:
        problems.append(f"{counts['unknown']} key(s) in state 'unknown': a write may have landed - "
                        f"check the downstream system, then `idemkey forget KEY`")
    if counts["pending_stale"]:
        problems.append(f"{counts['pending_stale']} pending key(s) with no heartbeat for "
                        f">{args.claim_ttl:g}s: the owner is probably dead; the next same-key call takes over")
    if counts["failed"]:
        notes.append(f"{counts['failed']} failed key(s): a same-key retry will run again")
    if counts["not_replayable"]:
        notes.append(f"{counts['not_replayable']} completed key(s) whose result was not stored "
                     f"(too large / not serialisable / store_result=False): a duplicate call raises")
    if oldest is not None and now - oldest > 30 * 86400:
        notes.append("rows older than 30 days exist: consider `idemkey purge` or guard(ttl=...)")
    if os.name == "posix" and os.path.exists(store.path):
        mode = os.stat(store.path).st_mode & 0o077
        if mode:
            problems.append(f"store file is readable by other users (mode {oct(os.stat(store.path).st_mode & 0o777)}); "
                            f"it holds tool results in plaintext")
    st = store.stats()
    print(f"store: {_safe(st['path'])}  keys: {st['total']}")
    for line in problems:
        print(f"ATTENTION: {_safe(line)}")
    for line in notes:
        print(f"note: {_safe(line)}")
    if not problems:
        print("ok")
    if problems:
        raise SystemExit(1)


def cmd_forget(args):
    store = _resolve_store(args.store)
    row = _lookup(store, args.key)
    # The delete re-checks the status atomically (DELETE ... WHERE status !=
    # 'pending'), so a row reclaimed between lookup and delete is never removed
    # out from under a running execution.
    if store.delete(row["key"], allow_pending=args.force):
        print(f"forgot {_safe(repr(args.key))} (was {row['status']}); its next use will execute again")
        return
    now = store.get(row["key"])
    if now is None:
        print(f"key {args.key!r} not found", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"key {args.key!r} is pending (an execution may be in flight). "
        f"Re-run with --force only if you are sure its owner is dead.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def cmd_stats(args):
    store = _resolve_store(args.store)
    s = store.stats()
    print(f"store: {_safe(s['path'])}")
    print(f"total keys: {s['total']}")
    for status, n in sorted(s["by_status"].items()):
        print(f"  {_safe(status):<10} {n}")


def build_parser():
    p = argparse.ArgumentParser(prog="idemkey", description="Inspect the idemkey dedup store.")
    p.add_argument("--store", default=None, help="path to the SQLite store")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("log", help="list recent key entries")
    pl.add_argument("--status", choices=["pending", "completed", "failed", "unknown"], default=None)
    pl.add_argument("--limit", type=_positive_int, default=50)
    pl.add_argument("--json", action="store_true", help="machine-readable output (no results)")
    pl.set_defaults(func=cmd_log)

    ps = sub.add_parser("show", help="show one key entry in full")
    ps.add_argument("key")
    ps.set_defaults(func=cmd_show)

    pp = sub.add_parser("purge", help="delete entries older than the TTL")
    pp.add_argument("--older-than", type=_positive_hours, default=24.0, help="hours (default 24)")
    pp.add_argument(
        "--include-stale-pending", action="store_true",
        help="also delete pending rows whose heartbeat is older than the cutoff",
    )
    pp.set_defaults(func=cmd_purge)

    pf = sub.add_parser(
        "forget", help="delete one key so its next use re-executes (after checking by hand)"
    )
    pf.add_argument("key")
    pf.add_argument("--force", action="store_true", help="also forget a pending key")
    pf.set_defaults(func=cmd_forget)

    pe = sub.add_parser("export", help="dump rows as JSON lines (audit / backup)")
    pe.add_argument("--status", choices=["pending", "completed", "failed", "unknown"], default=None)
    pe.add_argument("--no-results", action="store_true", help="leave stored results out (they may hold PII)")
    pe.set_defaults(func=cmd_export)

    pd = sub.add_parser("doctor", help="check for stuck/unknown keys and a loose file mode")
    pd.add_argument("--claim-ttl", type=float, default=300.0, help="seconds without heartbeat = dead owner")
    pd.set_defaults(func=cmd_doctor)

    pt = sub.add_parser("stats", help="store statistics")
    pt.set_defaults(func=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
