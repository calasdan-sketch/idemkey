"""idemkey fault-injection + concurrency benchmark.  Re-run:  python bench/bench.py all

Measures what idempotency libraries are bought for - how often a logical
operation's side effect happens MORE THAN ONCE (or not at all):

  crash  one subprocess per attempt; attempt 1 is SIGKILLed at an injected
         point (before the effect / after the effect but before the library
         recorded it / after everything), attempt 2 is a fresh process retrying
         with the same key.  Reports duplicate-effect and lost-effect rates.
  race   R racers (processes x threads) hit K keys at the same instant;
         the side effect is slow (20 ms).  Reports effects-per-key > 1, errors.
  speed  single-host throughput: new keys / replays / 8 threads, in ops/s.

The "world" is an append-only file (what the outside system saw).  Nothing
here is mocked inside the libraries: they run as released.  Optional
competitors are skipped when not installed:
  pip install idempotency latch-idempotent redis     (+ a redis-server binary)
To compare against an older idemkey, pass --legacy-path DIR (an unpacked
source tree, e.g. `git archive <rev> | tar -x -C DIR`).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import multiprocessing as mp
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REDIS_PORT = 6391

# -- the outside world ---------------------------------------------------------


class World:
    """Append-only effect log. ``apply(op, dk)`` is the 'API call'.  If a
    downstream key ``dk`` is supplied the world behaves like Stripe: a repeated
    key is acknowledged but not applied again."""

    def __init__(self, workdir):
        self.path = os.path.join(workdir, "world.log")
        open(self.path, "a").close()

    def apply(self, op, dk=None):
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            if dk is not None:
                f.seek(0)
                if any(line.rstrip("\n").split("\t")[-1] == dk for line in f):
                    return {"op": op, "deduped": True}
            f.write(f"{op}\t{dk or ''}\n")
            f.flush()
        return {"op": op}

    def find(self, op):
        with open(self.path) as f:
            return any(line.split("\t")[0] == op for line in f)

    def counts(self):
        c = {}
        with open(self.path) as f:
            for line in f:
                op = line.split("\t")[0]
                c[op] = c.get(op, 0) + 1
        return c


def _maybe_crash(point):
    if os.environ.get("BENCH_CRASH") == point:
        os.kill(os.getpid(), signal.SIGKILL)


# -- implementations -----------------------------------------------------------


def build(impl, workdir, slow=0.0, claim_ttl=0.5):
    """Return (call(op_id) -> result, world_counter() -> {op: n})."""
    world = World(workdir)
    db = os.path.join(workdir, "store.db")

    def effect(op, dk=None):
        _maybe_crash("before_effect")
        if slow:
            time.sleep(slow)
        r = world.apply(op, dk)
        _maybe_crash("after_effect")
        return r

    if impl == "naive":
        return (lambda op: effect(op)), world.counts

    if impl.startswith("idemkey"):
        from idemkey import DedupStore, guard
        mode = impl.split(":", 1)[1] if ":" in impl else "default"
        store = DedupStore(db)
        base = {"store": store, "claim_ttl": claim_ttl, "max_retries": 0, "inflight_timeout": 30}
        if mode == "default":
            g = guard(effect, **base)
        elif mode == "fast":
            g = guard(effect, **{**base, "store": DedupStore(db, durability="normal")})
        elif mode == "reconcile":
            from idemkey import NOT_FOUND
            g = guard(effect, reconcile=lambda op: {"op": op, "reconciled": True}
                      if world.find(op) else NOT_FOUND, **base)
        elif mode == "inject":
            def tool(op, *, dk=None):
                return effect(op, dk)
            g = guard(tool, inject_key="dk", **base)
        elif mode == "atomic":
            def tool(op, *, tx):
                _maybe_crash("before_effect")
                tx.execute("CREATE TABLE IF NOT EXISTS effects(op TEXT)")
                tx.execute("INSERT INTO effects VALUES (?)", (op,))
                _maybe_crash("after_effect")
                return {"op": op}
            g = guard(tool, atomic="tx", **base)

            def counts():
                c = sqlite3.connect(db)
                try:
                    return dict(c.execute("SELECT op, COUNT(*) FROM effects GROUP BY op"))
                except sqlite3.OperationalError:
                    return {}
                finally:
                    c.close()
            return (lambda op: g(op, idemkey=op)), counts
        else:
            raise SystemExit(f"unknown idemkey mode {mode}")
        return (lambda op: g(op, idemkey=op)), world.counts

    if impl == "idempotency-filestore":
        from idempotency import idempotent
        from idempotency.stores import FileStore
        g = idempotent(store=FileStore(os.path.join(workdir, "fs")), key=lambda op: op)(effect)
        return (lambda op: g(op)), world.counts

    if impl == "latch-redis":
        import redis
        from latch import idempotent
        from latch.stores.redis import RedisStore
        client = redis.Redis(port=REDIS_PORT)
        g = idempotent(store=RedisStore(client=client, key_prefix=f"b{os.path.basename(workdir)}:"))(
            lambda op: effect(op))
        return (lambda op: g(op, idempotency_key=op)), world.counts

    raise SystemExit(f"unknown impl {impl}")


ALL_IMPLS = ["naive", "idemkey:default", "idemkey:fast", "idemkey:reconcile", "idemkey:inject", "idemkey:atomic",
             "idempotency-filestore", "latch-redis"]


def available(impl):
    try:
        if impl == "idempotency-filestore":
            import idempotency  # noqa: F401
        elif impl == "latch-redis":
            import latch  # noqa: F401
            import redis  # noqa: F401
            if not shutil.which("redis-server"):
                return False
    except ImportError:
        return False
    return True


# -- worker (one attempt in its own process) -----------------------------------


def worker_main(a):
    out = None
    try:
        call, _ = build(a.worker_impl, a.workdir, claim_ttl=a.claim_ttl)
        out = call(a.op)
    except Exception as exc:  # noqa: BLE001
        out = {"error": type(exc).__name__, "msg": str(exc)}
    if os.environ.get("BENCH_CRASH") == "after_commit":
        os.kill(os.getpid(), signal.SIGKILL)
    print(json.dumps(out))


def _spawn(impl, workdir, op, crash, legacy, claim_ttl):
    env = dict(os.environ)
    env.pop("BENCH_CRASH", None)
    if crash:
        env["BENCH_CRASH"] = crash
    env["PYTHONPATH"] = (legacy if (legacy and impl.startswith("idemkey-legacy")) else ROOT)
    argv = [sys.executable, os.path.abspath(__file__), "worker", "--worker-impl",
            impl.replace("idemkey-legacy", "idemkey"), "--workdir", workdir, "--op", op,
            "--claim-ttl", str(claim_ttl)]
    return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


# -- scenarios -------------------------------------------------------------------

CRASH_POINTS = ["before_effect", "after_effect", "after_commit"]


def run_crash(impl, n_ops, legacy, claim_ttl=0.5, parallel=6):
    workdir = tempfile.mkdtemp(prefix="idembench-")
    ops = [f"op{i:04d}" for i in range(n_ops)]
    point = {op: CRASH_POINTS[i % 3] for i, op in enumerate(ops)}
    _run_batch(impl, workdir, ops, point, legacy, claim_ttl, parallel)
    time.sleep(claim_ttl + 0.3)  # let dead owners' claims go stale
    ok = {}
    batch2 = _run_batch(impl, workdir, ops, {}, legacy, claim_ttl, parallel)
    for op, out in batch2.items():
        ok[op] = isinstance(out, dict) and "error" not in out
    _, counts = build(impl.replace("idemkey-legacy", "idemkey"), workdir)
    c = counts()
    res = {"ops": n_ops}
    for pt in CRASH_POINTS:
        sel = [o for o in ops if point[o] == pt]
        res[pt] = {
            "ops": len(sel),
            "duplicate": sum(1 for o in sel if c.get(o, 0) > 1),
            "lost": sum(1 for o in sel if c.get(o, 0) == 0),
            "retry_errors": sum(1 for o in sel if not ok.get(o)),
        }
    res["duplicate_rate"] = round(sum(res[p]["duplicate"] for p in CRASH_POINTS) / n_ops, 4)
    res["lost_rate"] = round(sum(res[p]["lost"] for p in CRASH_POINTS) / n_ops, 4)
    shutil.rmtree(workdir, ignore_errors=True)
    return res


def _run_batch(impl, workdir, ops, point, legacy, claim_ttl, parallel):
    outs, running = {}, []
    queue = list(ops)
    while queue or running:
        while queue and len(running) < parallel:
            op = queue.pop()
            running.append((op, _spawn(impl, workdir, op, point.get(op), legacy, claim_ttl)))
        for item in list(running):
            op, p = item
            if p.poll() is not None:
                txt = p.stdout.read().strip()
                try:
                    outs[op] = json.loads(txt) if txt else {"killed": True}
                except ValueError:
                    outs[op] = {"error": "bad-output"}
                running.remove(item)
        time.sleep(0.005)
    return outs


def _racer_proc(impl, workdir, proc_idx, n_threads, n_keys, total, barrier, q, legacy_flag):
    if legacy_flag:
        sys.path.insert(0, legacy_flag)
    call, _ = build(impl, workdir, slow=0.02, claim_ttl=30)
    local = threading.Barrier(n_threads)
    results = []
    lock = threading.Lock()

    def racer(i):
        op = f"k{i % n_keys}"
        local.wait()
        if i % n_threads == 0:
            barrier.wait()
        t0 = time.perf_counter()
        try:
            call(op)
            err = None
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        with lock:
            results.append((time.perf_counter() - t0, err))

    base = proc_idx * n_threads
    ts = [threading.Thread(target=racer, args=(base + j,)) for j in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    q.put(results)


def run_race(impl, racers, keys, procs, legacy):
    workdir = tempfile.mkdtemp(prefix="idembench-")
    real = impl.replace("idemkey-legacy", "idemkey")
    build(real, workdir)  # create store/world first
    n_threads = racers // procs
    ctx = mp.get_context("spawn" if sys.platform == "win32" else "fork")
    barrier = ctx.Barrier(procs)
    q = ctx.Queue()
    legacy_flag = legacy if (legacy and impl.startswith("idemkey-legacy")) else None
    t0 = time.perf_counter()
    ps = [ctx.Process(target=_racer_proc,
                      args=(real, workdir, i, n_threads, keys, racers, barrier, q, legacy_flag))
          for i in range(procs)]
    for p in ps:
        p.start()
    results = []
    for _ in ps:
        results.extend(q.get(timeout=300))
    for p in ps:
        p.join()
    wall = time.perf_counter() - t0
    _, counts = build(real, workdir)
    c = counts()
    lat = sorted(r[0] for r in results if r[1] is None)
    errs = {}
    for _, e in results:
        if e:
            errs[e] = errs.get(e, 0) + 1
    res = {
        "racers": racers, "keys": keys,
        "keys_with_duplicate_effect": sum(1 for k in range(keys) if c.get(f"k{k}", 0) > 1),
        "keys_never_executed": sum(1 for k in range(keys) if c.get(f"k{k}", 0) == 0),
        "extra_effects": sum(max(0, v - 1) for v in c.values()),
        "errors": errs,
        "p50_ms": round(statistics.median(lat) * 1000, 1) if lat else None,
        "p99_ms": round(lat[int(len(lat) * 0.99) - 1] * 1000, 1) if len(lat) > 1 else None,
        "wall_s": round(wall, 2),
    }
    shutil.rmtree(workdir, ignore_errors=True)
    return res


def run_speed(impl, n, legacy):
    workdir = tempfile.mkdtemp(prefix="idembench-")
    real = impl
    call, _ = build(real, workdir)
    t0 = time.perf_counter()
    for i in range(n):
        call(f"s{i}")
    seq = n / (time.perf_counter() - t0)
    t0 = time.perf_counter()
    for i in range(n):
        call(f"s{i}")
    rep = n / (time.perf_counter() - t0)

    def th(base):
        for i in range(n // 8):
            call(f"t{base}-{i}")
    ts = [threading.Thread(target=th, args=(j,)) for j in range(8)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    par = (n // 8 * 8) / (time.perf_counter() - t0)
    shutil.rmtree(workdir, ignore_errors=True)
    return {"new_keys_per_s": round(seq), "replays_per_s": round(rep), "threads8_new_keys_per_s": round(par)}


# -- semantic duplicates -------------------------------------------------------


def run_semantic():
    """Pairs of calls that are (or are not) the same logical operation, sent
    under one key.  Counts pairs deduplicated vs. raising FingerprintMismatch."""
    import dataclasses
    import decimal
    import unicodedata
    import warnings
    from collections import namedtuple

    from idemkey import DedupStore, guard
    warnings.simplefilter("ignore")
    tmp = tempfile.mkdtemp(prefix="idembench-")

    class Pt:
        def __init__(self, x, y):
            self.x, self.y = x, y

    @dataclasses.dataclass
    class DC:
        a: int

    NT = namedtuple("NT", "a b")
    cafe_nfc, cafe_nfd = "caf\u00e9", unicodedata.normalize("NFD", "caf\u00e9")

    def pay(amount, memo="", *, request_id=None, email=""):
        return {"ok": True}

    kw = {"store": DedupStore(os.path.join(tmp, "s.db")), "max_retries": 0}
    plain = guard(pay, **kw)
    try:
        conf = guard(pay, ignore=("request_id",), normalize={"email": lambda s: s.strip().casefold()}, **kw)
    except TypeError:  # older idemkey has no ignore/normalize
        conf = None
    D = decimal.Decimal
    same = [  # (label, args_a, kwargs_a, args_b, kwargs_b, needs_config)
        ("int vs float 10/10.0", (10,), {}, (10.0,), {}, False),
        ("int vs Decimal('10.00')", (10,), {}, (D("10.00"),), {}, False),
        ("NFC vs NFD unicode", (1, cafe_nfc), {}, (1, cafe_nfd), {}, False),
        ("positional vs keyword spelling", (5, "m"), {}, (), {"amount": 5, "memo": "m"}, False),
        ("dict key order", ({"a": 1, "b": 2},), {}, ({"b": 2, "a": 1},), {}, False),
        ("set order", ({3, 1, 2},), {}, ({2, 3, 1},), {}, False),
        ("equal dataclass instances", (DC(1),), {}, (DC(1),), {}, False),
        ("equal plain-object instances", (Pt(1, 2),), {}, (Pt(1, 2),), {}, False),
        ("equal namedtuples", (NT(1, 2),), {}, (NT(1, 2),), {}, False),
        ("fresh request_id per retry", (5,), {"request_id": "a"}, (5,), {"request_id": "b"}, True),
        ("email case/whitespace", (5,), {"email": " Bob@X.com "}, (5,), {"email": "bob@x.com"}, True),
    ]
    diff = [
        ("10 vs 11", (10,), {}, (11,), {}),
        ("'10' vs 10", ("10",), {}, (10,), {}),
        ("True vs 1", (True,), {}, (1,), {}),
        ("0.1 vs Decimal('0.1')", (0.1,), {}, (D("0.1"),), {}),
        ("memo 'a' vs 'A'", (1, "a"), {}, (1, "A"), {}),
        ("[1,2] vs [2,1]", ([1, 2],), {}, ([2, 1],), {}),
        ("None vs ''", (None,), {}, ("",), {}),
        ("objects, different state", (Pt(1, 2),), {}, (Pt(1, 3),), {}),
        ("dataclasses, different state", (DC(1),), {}, (DC(2),), {}),
        ("request_id differs, NOT ignored", (5,), {"request_id": "a"}, (5,), {"request_id": "b"}),
    ]
    n = [0]

    def attempt(g, aa, ka, ab, kb):
        n[0] += 1
        key = f"pair{n[0]}"
        g(*aa, idemkey=key, **ka)
        try:
            g(*ab, idemkey=key, **kb)
            return True
        except Exception:  # noqa: BLE001
            return False

    res = {"merged_default": 0, "merged_configured": 0, "same_total": len(same),
           "wrongly_merged": 0, "diff_total": len(diff), "detail": {}}
    for label, aa, ka, ab, kb, needs in same:
        d = attempt(plain, aa, ka, ab, kb)
        c = attempt(conf, aa, ka, ab, kb) if conf else d
        res["merged_default"] += d
        res["merged_configured"] += c
        res["detail"][label] = {"default": d, "configured": c}
    for label, aa, ka, ab, kb in diff:
        wrong = attempt(plain, aa, ka, ab, kb)
        if conf and "NOT ignored" not in label:
            wrong = wrong or attempt(conf, aa, ka, ab, kb)
        res["wrongly_merged"] += wrong
        res["detail"][label] = {"wrongly_merged": wrong}
    shutil.rmtree(tmp, ignore_errors=True)
    return res


# -- round 3: lost responses inside one call, and fingerprint safety/latency -----


def run_lost(n=60):
    """The write lands, then the response is lost (``TimeoutError``) - inside ONE
    guarded call, so the library's own retry loop re-runs the tool.  One effect
    per op is correct.  Runs against whatever idemkey is on PYTHONPATH."""
    from idemkey import NOT_FOUND, DedupStore, guard
    tmp = tempfile.mkdtemp(prefix="idembench-")
    res = {}

    def scenario(name, make):
        world, seen, first = [], set(), set()

        def effect(op, dk=None):
            if dk is not None and dk in seen:
                pass
            else:
                if dk is not None:
                    seen.add(dk)
                world.append(op)
            if op not in first:  # the first attempt of every op "loses" its response
                first.add(op)
                raise TimeoutError("response lost")
            return {"op": op}

        g = make(effect, world, DedupStore(os.path.join(tmp, name + ".db")))
        errors = 0
        for i in range(n):
            try:
                g(f"op{i}", idemkey=f"lost-{i}")
            except Exception:  # noqa: BLE001
                errors += 1
        dup = sum(1 for i in range(n) if world.count(f"op{i}") > 1)
        res[name] = {"ops": n, "ops_with_duplicate_effect": dup, "errors": errors}

    base = {"max_retries": 3, "sleep": lambda s: None}
    scenario("default", lambda eff, w, st: guard(lambda op: eff(op), store=st, **base))
    scenario("reconcile", lambda eff, w, st: guard(
        lambda op: eff(op), store=st, reconcile=lambda op: {"op": op} if op in w else NOT_FOUND, **base))

    def inj(eff, w, st):
        def tool(op, *, dk=None):
            return eff(op, dk)
        return guard(tool, store=st, inject_key="dk", **base)
    scenario("inject_key", inj)
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def run_fp():
    """Fingerprint latency on large/hostile arguments and exactness of numbers.
    Each probe is bounded to 20 s (a probe that does not finish reports 'hang')."""
    import decimal
    import multiprocessing as _mp

    def probe(q, which):
        from idemkey.keys import fingerprint2 as fp
        D = decimal.Decimal
        t0 = time.perf_counter()
        if which == "ints_300k":
            fp("t", (list(range(300_000)),))
        elif which == "text_20MB":
            fp("t", ("a" * 20_000_000,))
        elif which == "decimal_1e999999999":
            fp("t", (D("1e999999999"),))
        elif which == "int_10^100000":
            try:
                fp("t", (10 ** 100000,))
            except ValueError as exc:
                q.put(("error: " + type(exc).__name__, 0))
                return
        elif which == "decimals_differ_past_28_digits":
            a = fp("t", (D("1.0000000000000000000000000000001"),))
            b = fp("t", (D("1.0000000000000000000000000000002"),))
            q.put(("distinct" if a != b else "WRONGLY MERGED", 0))
            return
        q.put(("ok", round(time.perf_counter() - t0, 3)))

    out = {}
    ctx = _mp.get_context("fork" if sys.platform != "win32" else "spawn")
    for which in ("ints_300k", "text_20MB", "decimal_1e999999999", "int_10^100000",
                  "decimals_differ_past_28_digits"):
        q = ctx.Queue()
        p = ctx.Process(target=probe, args=(q, which))
        p.start()
        p.join(20)
        if p.is_alive():
            p.terminate()
            p.join()
            out[which] = "hang (>20 s)"
        else:
            status, secs = q.get() if not q.empty() else ("crashed", 0)
            out[which] = status if status != "ok" else f"{secs} s"
    return out


# -- driver --------------------------------------------------------------------


def start_redis():
    if not shutil.which("redis-server"):
        return None
    p = subprocess.Popen(["redis-server", "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return p


def table(title, rows, cols):
    print(f"\n## {title}\n")
    print("| impl | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name, r in rows:
        print(f"| {name} | " + " | ".join(str(r.get(c, "")) for c in cols) + " |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["all", "crash", "race", "speed", "semantic", "lost", "fp", "worker", "speed1", "semantic1",
                                     "lost1", "fp1"])
    ap.add_argument("--impl", action="append")
    ap.add_argument("--ops", type=int, default=90)
    ap.add_argument("--racers", type=int, default=1000)
    ap.add_argument("--keys", type=int, default=20)
    ap.add_argument("--procs", type=int, default=10)
    ap.add_argument("--speed-n", type=int, default=400)
    ap.add_argument("--legacy-path")
    ap.add_argument("--prev-path", help="source tree of the previous round, for the lost/fp before-after rows")
    ap.add_argument("--json")
    ap.add_argument("--worker-impl")
    ap.add_argument("--workdir")
    ap.add_argument("--op")
    ap.add_argument("--claim-ttl", type=float, default=0.5)
    a = ap.parse_args()
    if a.what == "worker":
        return worker_main(a)
    if a.what == "semantic1":
        print(json.dumps(run_semantic()))
        return
    if a.what == "lost1":
        print(json.dumps(run_lost()))
        return
    if a.what == "fp1":
        print(json.dumps(run_fp()))
        return
    if a.what == "speed1":
        print(json.dumps(run_speed(a.worker_impl, a.speed_n, None)))
        return

    impls = a.impl or ALL_IMPLS[:]
    if a.legacy_path and not a.impl:
        impls.insert(1, "idemkey-legacy")
    impls = [i for i in impls if available(i)]
    redis_proc = start_redis() if "latch-redis" in impls else None
    out = {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "cpus": os.cpu_count()}
    try:
        if a.what in ("all", "crash"):
            out["crash"] = {}
            for i in impls:
                if i == "idemkey-legacy":
                    r = run_crash("idemkey-legacy:default", a.ops, a.legacy_path, a.claim_ttl)
                else:
                    r = run_crash(i, a.ops, a.legacy_path, a.claim_ttl)
                out["crash"][i] = r
            table("Crash: SIGKILL a worker, retry in a fresh process (duplicate / lost per op)",
                  [(i, {"duplicate_rate": r["duplicate_rate"], "lost_rate": r["lost_rate"],
                        "dup after_effect": f'{r["after_effect"]["duplicate"]}/{r["after_effect"]["ops"]}',
                        "retry errors": sum(r[p]["retry_errors"] for p in CRASH_POINTS)})
                   for i, r in out["crash"].items()],
                  ["duplicate_rate", "lost_rate", "dup after_effect", "retry errors"])
        if a.what in ("all", "race"):
            out["race"] = {}
            for i in impls:
                if i == "naive":
                    continue
                name = "idemkey-legacy:default" if i == "idemkey-legacy" else i
                out["race"][i] = run_race(name, a.racers, a.keys, a.procs, a.legacy_path)
            table(f"Race: {a.racers} racers x {a.keys} keys, {a.procs} processes",
                  list(out["race"].items()),
                  ["keys_with_duplicate_effect", "extra_effects", "keys_never_executed", "errors",
                   "p50_ms", "p99_ms", "wall_s"])
        if a.what in ("all", "speed"):
            out["speed"] = {}
            for i in impls:
                if i == "naive":
                    continue
                name = "idemkey-legacy:default" if i == "idemkey-legacy" else i
                env = dict(os.environ)
                env["PYTHONPATH"] = a.legacy_path if i == "idemkey-legacy" else ROOT
                cp = subprocess.run(
                    [sys.executable, os.path.abspath(__file__), "speed1", "--worker-impl",
                     name.replace("idemkey-legacy", "idemkey"), "--speed-n", str(a.speed_n)],
                    env=env, capture_output=True, text=True, check=True)
                out["speed"][i] = json.loads(cp.stdout.strip().splitlines()[-1])
            table("Speed (ops/s, one host)", list(out["speed"].items()),
                  ["new_keys_per_s", "replays_per_s", "threads8_new_keys_per_s"])
        if a.what in ("all", "semantic"):
            out["semantic"] = {}
            versions = ([("idemkey-legacy", a.legacy_path)] if a.legacy_path else []) + [("idemkey", ROOT)]
            for name, path in versions:
                env = dict(os.environ)
                env["PYTHONPATH"] = path
                cp = subprocess.run([sys.executable, os.path.abspath(__file__), "semantic1"],
                                    env=env, capture_output=True, text=True, check=True)
                out["semantic"][name] = json.loads(cp.stdout.strip().splitlines()[-1])
            table("Semantic duplicates: cosmetically different retries of the same call",
                  [(k, {"same-call pairs deduped (defaults)": f'{v["merged_default"]}/{v["same_total"]}',
                        "same-call pairs deduped (ignore=/normalize= set)": f'{v["merged_configured"]}/{v["same_total"]}',
                        "different calls wrongly merged": f'{v["wrongly_merged"]}/{v["diff_total"]}'})
                   for k, v in out["semantic"].items()],
                  ["same-call pairs deduped (defaults)", "same-call pairs deduped (ignore=/normalize= set)",
                   "different calls wrongly merged"])
        for what, label in (("lost", "Lost response inside one call (retry loop re-runs the tool)"),
                            ("fp", "Fingerprint probes (large / hostile arguments)")):
            if a.what not in ("all", what):
                continue
            out[what] = {}
            versions = ([("previous round", a.prev_path)] if a.prev_path else []) + [("this tree", ROOT)]
            for name, path in versions:
                env = dict(os.environ)
                env["PYTHONPATH"] = path
                cp = subprocess.run([sys.executable, os.path.abspath(__file__), what + "1"],
                                    env=env, capture_output=True, text=True, check=True)
                out[what][name] = json.loads(cp.stdout.strip().splitlines()[-1])
            print(f"\n## {label}\n")
            for name, v in out[what].items():
                print(f"- {name}: {json.dumps(v)}")
    finally:
        if redis_proc:
            redis_proc.terminate()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
