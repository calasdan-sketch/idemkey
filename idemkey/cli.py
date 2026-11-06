"""Command-line inspection of the dedup store.

    idemkey log [--store PATH] [--status pending|completed|failed|unknown] [--limit N]
    idemkey show KEY [--store PATH]
    idemkey purge [--store PATH] [--older-than HOURS] [--include-stale-pending]
    idemkey stats [--store PATH]
    idemkey forget KEY [--store PATH]
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


def cmd_log(args):
    store = _resolve_store(args.store)
    rows = store.list(status=args.status, limit=args.limit)
    if not rows:
        print("(no entries)")
        return
    print(f"{'KEY':<40} {'TOOL':<22} {'STATUS':<10} {'TRY':>3}  UPDATED")
    for r in rows:
        print(
            f"{_safe(r['key']):<40.39} {_safe(r['tool']):<22.21} {_safe(r['status']):<10} "
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
    pl.add_argument("--limit", type=int, default=50)
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

    pt = sub.add_parser("stats", help="store statistics")
    pt.set_defaults(func=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
