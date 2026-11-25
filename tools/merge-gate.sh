#!/bin/bash
# Merge gate (owner rule 2026-11-25: no regression with any fix).
# usage: tools/merge-gate.sh WORKTREE [BASELINE]
#   Runs the full suite in WORKTREE with the pcbnew python and compares the failing tests with BASELINE
#   (default docs/TEST-BASELINE.txt: one "path::test" per line, the known failures of mainline).
#   exit 0: no test fails that is not in the baseline (fixed baseline failures are listed: remove them from the file).
#   exit 1: new failures (listed) -> do not merge.  exit 2: the suite did not run.
set -u
WT=${1:?worktree}; HERE=$(cd "$(dirname "$0")/.." && pwd); BASE=${2:-$HERE/docs/TEST-BASELINE.txt}
PY=${PY:-/usr/bin/python3}
out=$(mktemp); trap 'rm -f "$out"' EXIT
( cd "$WT" && timeout 3600 "$PY" -m pytest -q -p no:cacheprovider -rfE --continue-on-collection-errors tests/ ) > "$out" 2>&1
grep -qE '[0-9]+ (passed|failed)' "$out" || { echo "merge-gate: suite did not run"; tail -5 "$out"; exit 2; }
now=$(grep -E '^(FAILED|ERROR) ' "$out" | sed -E 's/^(FAILED|ERROR) //; s/ - .*//' | sort -u)
base=$(grep -vE '^\s*(#|$)' "$BASE" 2>/dev/null | sort -u)
new=$(comm -23 <(echo "$now") <(echo "$base") | grep .)
gone=$(comm -13 <(echo "$now") <(echo "$base") | grep .)
echo "merge-gate: $(tail -1 "$out")"
[ -n "$gone" ] && { echo "fixed since the baseline (remove from $BASE):"; echo "$gone" | sed 's/^/  /'; }
if [ -n "$new" ]; then echo "NEW FAILURES - do not merge:"; echo "$new" | sed 's/^/  /'; exit 1; fi
echo "merge-gate: OK (no failure outside the baseline)"; exit 0
