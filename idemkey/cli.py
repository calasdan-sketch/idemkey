"""Command-line inspection of the dedup store.

    idemkey log [--store PATH] [--status pending|completed|failed] [--limit N]
    idemkey show KEY [--store PATH]
    idemkey purge [--store PATH] [--older-than HOURS]
    idemkey stats [--store PATH]
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


def cmd_log(args):
    store = _resolve_store(args.store)
    rows = store.list(status=args.status, limit=args.limit)
    if not rows:
        print("(no entries)")
        return
    print(f"{'KEY':<40} {'TOOL':<22} {'STATUS':<10} {'TRY':>3}  UPDATED")
    for r in rows:
        print(
            f"{r['key']:<40.39} {r['tool']:<22.21} {r['status']:<10} "
            f"{r['attempts']:>3}  {_fmt_ts(r['updated_at'])}"
        )


def cmd_show(args):
    store = _resolve_store(args.store)
    row = store.get(args.key)
    if row is None:
        print(f"key {args.key!r} not found", file=sys.stderr)
        raise SystemExit(1)
    out = dict(row)
    for ts_field in ("created_at", "updated_at", "heartbeat"):
        out[ts_field + "_human"] = _fmt_ts(out[ts_field])
    if out.get("result_json"):
        try:
            out["result"] = json.loads(out["result_json"])
        except ValueError:
            out["result"] = out["result_json"]
    print(json.dumps(out, indent=2, default=str))


def cmd_purge(args):
    store = _resolve_store(args.store)
    n = store.purge(older_than_seconds=args.older_than * 3600)
    print(f"purged {n} entr{'y' if n == 1 else 'ies'} older than {args.older_than}h")


def cmd_stats(args):
    store = _resolve_store(args.store)
    s = store.stats()
    print(f"store: {s['path']}")
    print(f"total keys: {s['total']}")
    for status, n in sorted(s["by_status"].items()):
        print(f"  {status:<10} {n}")


def build_parser():
    p = argparse.ArgumentParser(prog="idemkey", description="Inspect the idemkey dedup store.")
    p.add_argument("--store", default=None, help="path to the SQLite store")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("log", help="list recent key entries")
    pl.add_argument("--status", choices=["pending", "completed", "failed"], default=None)
    pl.add_argument("--limit", type=int, default=50)
    pl.set_defaults(func=cmd_log)

    ps = sub.add_parser("show", help="show one key entry in full")
    ps.add_argument("key")
    ps.set_defaults(func=cmd_show)

    pp = sub.add_parser("purge", help="delete entries older than the TTL")
    pp.add_argument("--older-than", type=float, default=24.0, help="hours (default 24)")
    pp.set_defaults(func=cmd_purge)

    pt = sub.add_parser("stats", help="store statistics")
    pt.set_defaults(func=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
