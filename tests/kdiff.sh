#!/bin/sh
# Detached K14 runner: one tests/kdiff.py process per board, each writing <out>/<board>/kdiff.status.json.
# Usage: nohup sh tests/kdiff.sh <out-dir> <seed> <edits-per-board> <board.kicad_pcb>... > <out-dir>/runner.log 2>&1 &
set -u
OUT=$1; SEED=$2; EDITS=$3; shift 3
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"
for b in "$@"; do
  name=$(basename "$b" .kicad_pcb)
  ( cd "$REPO" && PYTHONPATH="$REPO" /usr/bin/python3 tests/kdiff.py "$b" --out "$OUT/$name" --seed "$SEED" --edits "$EDITS" \
      > "$OUT/$name.log" 2>&1 ) &
done
wait
