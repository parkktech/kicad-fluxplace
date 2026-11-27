# Slow-tier replays (tests/replay)

Real inputs, real CLI, no mocks. Skipped everywhere unless `GATE_REPLAY=1`; `tools/merge-gate.sh` ignores `tests/replay` in its parallel run.

- Run by hand: `GATE_REPLAY=1 /usr/bin/python3 -m pytest -s tests/replay` (needs the router venv / freerouting jar; paths overridable with REPLAY_ROUTER_DIR, REPLAY_ROUTER_PY, REPLAY_FREEROUTING_JAR; route replays skip without them).
- In the gate (lead, once per batch): `GATE_REPLAY=1 tools/merge-gate.sh WORKTREE`. After the suite it runs tests/replay serially (timeout 7200 s) and judges failures against docs/TEST-BASELINE.txt; prints `merge-gate: replay REPLAY ...; CANARY ...` lines.
- Data (tests/replay/data, ~2.5 MB, xz boards): ParkkRouter 1.5.1 / 1.5.2 (re-net, route) / 1.5.3 (incremental floorplan) update inputs with constraints copied AWAY from the project (relative `[docs] datasheets`, resolved via the project dir), and the W16 pour-tie board of route-7 for the canary.
- test_update_replay.py: one test per stopgap (datasheet path, `--from silk` on an inherited gap, title block rev, route gate on pre-existing gaps, DRC under the project .kicad_pro no worse, no <0.2 mm dangling stub with `[finish] enabled`, opens not higher).
- test_pairs_canary.py: pairs stage alone on the W16 board; complete pairs >= 13 (route-7) and no "time budget reached before this pair was searched". Change MIN_COMPLETE only with a ledger row.
