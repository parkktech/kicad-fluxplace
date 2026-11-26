#!/usr/bin/env python3
"""Ledger lint (B2): every ONE-SHOT-LEDGER row whose fix/result cells say fixed / done must name a test id that exists.
usage: ledger-lint.py WORKTREE [LEDGER]   -> prints "ledger: N fixed rows, M with a test" (information only: exit 0), then the rows without a test.
A test id is `test_<name>` (a `def test_<name>` in tests/), `tests/<path>.py[::name]` (the file exists) or `golden:<name>`
(a `test_<name>` in tests/golden/)."""
import os
import re
import sys

wt = sys.argv[1] if len(sys.argv) > 1 else "."
ledger = sys.argv[2] if len(sys.argv) > 2 else os.path.join(wt, "docs", "ONE-SHOT-LEDGER.md")
if not os.path.isfile(ledger):
    ledger = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "ONE-SHOT-LEDGER.md")
defs = set()
for root, _d, files in os.walk(os.path.join(wt, "tests")):
    for f in files:
        if f.endswith(".py"):
            defs |= set(re.findall(r"^\s*def (test_\w+)", open(os.path.join(root, f), errors="replace").read(), re.M))
FIXED = re.compile(r"\b(fixed|done)\b", re.I)
ID = re.compile(r"golden:(\w+)|tests/[\w/]+\.py(?:::\w+)?|\btest_\w+")


def has_test(row):
    for m in ID.finditer(row):
        t = m.group(0)
        if m.group(1):
            if "test_" + m.group(1) in defs:
                return True
        elif t.startswith("tests/"):
            if os.path.isfile(os.path.join(wt, t.split("::")[0])):
                return True
        elif t in defs or any(d.startswith(t) for d in defs):          # test_power ... a module-name stem also counts
            return True
    return False


fixed = [(i, l) for i, l in enumerate(open(ledger, errors="replace"), 1)
         if l.startswith("| 20") and FIXED.search("|".join(l.split("|")[6:]))]       # cells 6.. = fix / result / status
bad = [(i, l) for i, l in fixed if not has_test(l)]
print(f"ledger: {len(fixed)} fixed rows, {len(fixed) - len(bad)} with a test")
for i, l in bad[:40]:
    c = l.split("|")
    print(f"  no test id: line {i} {c[1].strip()} {c[2].strip()[:50]}")
sys.exit(0)
