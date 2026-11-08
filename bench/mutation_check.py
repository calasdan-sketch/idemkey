"""Mutation-style sanity check: break the library on purpose, one line at a time,
and confirm the test suite notices. A mutant that survives means a safety rule
has no test behind it.

    python bench/mutation_check.py            # all mutants, ~1-2 minutes
    python bench/mutation_check.py --list

Works on a throw-away copy; the repository is never modified. Needs pytest.
"""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (name, file, old text (must occur exactly once), new text)
MUTANTS = [
    ("claim: INSERT OR IGNORE -> INSERT OR REPLACE (a second caller steals the key)",
     "store.py", "INSERT OR IGNORE INTO idem_keys", "INSERT OR REPLACE INTO idem_keys"),
    ("claim: stale-takeover test inverted (live owner gets taken over)",
     "store.py", "if now - row[\"heartbeat\"] > claim_ttl:", "if now - row[\"heartbeat\"] <= claim_ttl:"),
    ("settle: owner fence dropped on complete/fail (a stale owner can overwrite)",
     "store.py", "set_, set_ + \" AND owner = ? AND status = 'pending'\",\n            (result_json, 1 if replayable else 0, now, now), key, owner,",
     "set_, set_ + \" AND ? IS NOT NULL AND status = 'pending'\",\n            (result_json, 1 if replayable else 0, now, now), key, owner,"),
    ("forget: pending rows can be deleted without --force",
     "store.py", "DELETE FROM idem_keys WHERE key = ? AND status != 'pending'", "DELETE FROM idem_keys WHERE key = ?"),
    ("purge: deletes pending rows",
     "store.py", "f\"{verb} idem_keys WHERE status != 'pending' AND updated_at < ?\"", "f\"{verb} idem_keys WHERE updated_at < ?\""),
    ("keys: blank key accepted",
     "guard.py", "if not key.strip():", "if False:"),
    ("keys: padded key accepted (round 5)",
     "guard.py", "if key != key.strip():", "if False:"),
    ("keys: placeholder 'null' accepted (round 5)",
     "guard.py", "if key.lower() in PLACEHOLDER_KEYS:", "if False:"),
    ("keys: control characters accepted",
     "guard.py", "    bad = _bad_char(key)\n    if bad is not None:", "    bad = None\n    if bad is not None:"),
    ("keys: length limit off by a lot",
     "guard.py", "if len(key) > MAX_KEY_LENGTH:", "if len(key) > MAX_KEY_LENGTH * 100:"),
    ("namespace: scope|tool|prefix dropped from the stored key",
     "guard.py", "return NS_SEP.join((scope, tool, prefix, key))", "return key"),
    ("fingerprint: numbers all look alike",
     "keys.py", "return f\"num:{x}\"", "return \"num:0\""),
    ("migrate: renames without checking the new name is free",
     "store.py", "if \"|\" in scope or \"|\" in tool or conn.execute(", "if \"|\" in scope or \"|\" in tool or False and conn.execute("),
    ("busy store: error swallowed into a retry forever is not allowed to run the tool",
     "store.py", "raise _busy_error(self.path) from exc\n                with self._lock:", "return 'claimed', None\n                with self._lock:"),
    ("cli: doctor ignores unknown keys",
     "cli.py", "if counts[\"unknown\"]:", "if False:"),
]


def run(mutant, workdir):
    _name, fname, old, new = mutant
    target = workdir / "idemkey" / fname
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        return "BROKEN-MUTANT", f"pattern occurs {text.count(old)} times"
    target.write_text(text.replace(old, new), encoding="utf-8")
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
                            "--ignore=tests/test_round5_properties.py"],
                           cwd=workdir, capture_output=True, text=True, timeout=900, check=False,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    finally:
        target.write_text(text, encoding="utf-8")
    return ("KILLED" if p.returncode != 0 else "SURVIVED"), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for m in MUTANTS:
            print(m[0])
        return 0
    survived = 0
    with tempfile.TemporaryDirectory(prefix="idemkey-mut-") as td:
        work = pathlib.Path(td)
        shutil.copytree(ROOT / "idemkey", work / "idemkey", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        for m in MUTANTS:
            verdict, note = run(m, work)
            survived += verdict != "KILLED"
            print(f"{verdict:<13} {m[0]} {note}")
    print(f"\n{len(MUTANTS) - survived}/{len(MUTANTS)} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
