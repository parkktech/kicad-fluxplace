#!/bin/bash
# Detached routebench run (ROUTER-C0 B5). Usage: tests/routebench.sh OUTDIR
# Refuses to start while another router runs. Smoke (interf-u, both engines) first; full run only if the smoke has no error row.
set -u
OUT=${1:?usage: routebench.sh OUTDIR}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"
if pgrep -af 'route\.py|freerouting' >/dev/null; then
  echo "ROUTER_RUNNING: not started"; pgrep -af 'route\.py|freerouting'; exit 3
fi
cd "$ROOT" || exit 1
{
  /usr/bin/python3 tests/routebench.py --out "$OUT/smoke" --work "$OUT/work" --board interf-u \
    --engine krt --engine freerouting --no-jsonl || { echo "SMOKE_CRASHED"; exit 2; }
  if grep -q '"status": "error"' "$OUT/smoke/routebench.json"; then echo "SMOKE_ERROR_ROW"; exit 2; fi
  echo "SMOKE_OK"
  /usr/bin/python3 tests/routebench.py --out "$OUT" --work "$OUT/work" --engine krt --engine freerouting
  echo "FULL_EXIT $?"
} > "$OUT/run.out" 2>&1
