#!/bin/bash
# Merge gate (owner rule 2026-11-25: no regression with any fix).
# usage: tools/merge-gate.sh WORKTREE [BASELINE]
#   Runs the full suite in WORKTREE with the pcbnew python and compares the failing tests with BASELINE
#   (default docs/TEST-BASELINE.txt: one "path::test" per line, the known failures of mainline).
#   exit 0: no test fails that is not in the baseline (fixed baseline failures are listed: remove them from the file).
#   exit 1: new failures (listed) -> do not merge.  exit 2: the suite did not run.
#   Speed (B9): tests/golden runs first (fail -> abort, exit 1); the rest runs under pytest-xdist
#   (GATE_WORKERS, default 8; --dist loadgroup keeps the xdist_group("serial") tests of tests/conftest.py on one worker).
#   GATE_WORKERS=0 runs the old single-process suite (same fail set; used to prove equivalence).
#   Queue line: "merge-gate: queue: testing HEAD <sha>"; the gate fails (exit 2) if HEAD moved or tracked files changed
#   during the run, so the sha printed is the sha to merge.
set -u
WT=${1:?worktree}; HERE=$(cd "$(dirname "$0")/.." && pwd); BASE=${2:-$HERE/docs/TEST-BASELINE.txt}
PY=${PY:-/usr/bin/python3}
"$PY" "$HERE/tools/ledger-lint.py" "$WT" || true          # B2: information only, never fails the gate (yet)
out=$(mktemp); trap '[ -n "${GATE_KEEP:-}" ] && cp "$out" "$GATE_KEEP"; rm -f "$out"' EXIT   # GATE_KEEP=file: keep the pytest log
# Gate queue (2026-11-26): 5-6 concurrent gates on 32 cores gave load 90-170, timeouts (rc 124, 149 failures) and
# timing flakes. At most GATE_SLOTS gates run at once; the rest wait for a slot (the wait is not part of the timeout).
SLOTS=${GATE_SLOTS:-2}; LOCKDIR=${GATE_LOCKDIR:-/tmp/fluxplace-merge-gate}; mkdir -p "$LOCKDIR"
got=; echo "merge-gate: waiting for a slot ($SLOTS max)" >&2
while [ -z "$got" ]; do
  for i in $(seq 1 "$SLOTS"); do exec {fd}>"$LOCKDIR/slot$i"; if flock -n "$fd"; then got=$i; break; fi; exec {fd}>&-; done
  [ -z "$got" ] && sleep 20
done
echo "merge-gate: slot $got ($(cut -d' ' -f1 /proc/loadavg) load)" >&2
head0=$(git -C "$WT" rev-parse HEAD); dirty0=$(git -C "$WT" status --porcelain --untracked-files=no | wc -l)
echo "merge-gate: queue: testing HEAD $head0 ($WT, $dirty0 modified tracked files) - merge exactly this sha" >&2
W=${GATE_WORKERS:-8}; PT=("$PY" -m pytest -q -p no:cacheprovider -rfE --continue-on-collection-errors)
t0=$(date +%s)
# golden set first (own process): a failure here is a new failure by definition, abort early
IGN=()
if [ -d "$WT/tests/golden" ]; then
  gout=$(mktemp); ( cd "$WT" && timeout 900 "${PT[@]}" tests/golden ) > "$gout" 2>&1; grc=$?
  if [ $grc -ne 0 ]; then echo "merge-gate: GOLDEN SET FAILED (rc=$grc) - aborted before the full suite"; grep -E '^(FAILED|ERROR) ' "$gout" | sed 's/^/  /'; tail -3 "$gout"; rm -f "$gout"; exit 1; fi
  echo "merge-gate: golden set $(tail -1 "$gout")"; rm -f "$gout"; IGN=(--ignore=tests/golden)
fi
if [ "$W" -gt 0 ]; then X=(-n "$W" --dist loadgroup); else X=(); fi
( cd "$WT" && timeout 3600 "${PT[@]}" "${X[@]}" "${IGN[@]}" tests/ ) > "$out" 2>&1
rc=$?
echo "merge-gate: suite wall $(( $(date +%s)-t0 )) s, workers $W"
# Completion evidence (outside review 2026-11-26): pytest rc 0 = all passed, 1 = tests failed (judged against the
# baseline below); anything else (2 interrupted, 3 internal, 4 usage, 5 none collected, 124 timeout) is a failed gate.
case $rc in 0|1) ;; *) echo "merge-gate: suite did not complete (pytest rc=$rc)"; tail -5 "$out"; exit 2;; esac
grep -qE '^=*.*[0-9]+ (passed|failed).* in [0-9.]+s' "$out" || { echo "merge-gate: no completed summary line"; tail -5 "$out"; exit 2; }
grep -qE '[0-9]+ errors? during collection|Interrupted' "$out" && { echo "merge-gate: collection errors / interrupted"; grep -E 'ERROR collecting|Interrupted' "$out" | head; exit 2; }
[ $rc -eq 1 ] && ! grep -qE '^(FAILED|ERROR) ' "$out" && { echo "merge-gate: rc=1 without failure rows"; exit 2; }
head1=$(git -C "$WT" rev-parse HEAD); dirty1=$(git -C "$WT" status --porcelain --untracked-files=no | wc -l)
if [ "$head1" != "$head0" ] || [ "$dirty1" != "$dirty0" ]; then echo "merge-gate: worktree changed during the run (HEAD $head0 -> $head1, modified $dirty0 -> $dirty1): result is for no single sha"; exit 2; fi
now=$(grep -E '^(FAILED|ERROR) ' "$out" | sed -E 's/^(FAILED|ERROR) //; s/ - .*//' | sort -u)
base=$(grep -vE '^\s*(#|$)' "$BASE" 2>/dev/null | sort -u)
new=$(comm -23 <(echo "$now") <(echo "$base") | grep .)
gone=$(comm -13 <(echo "$now") <(echo "$base") | grep .)
echo "merge-gate: $(tail -1 "$out")"
[ -n "$gone" ] && { echo "fixed since the baseline (remove from $BASE):"; echo "$gone" | sed 's/^/  /'; }
if [ -n "$new" ]; then echo "NEW FAILURES - do not merge:"; echo "$new" | sed 's/^/  /'; exit 1; fi
echo "merge-gate: OK (no failure outside the baseline) tested HEAD $head1"; exit 0
