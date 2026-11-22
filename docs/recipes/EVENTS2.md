# EVENTS2 recipe: dashboard requests #1, #2, #4, #5, #7 (ParkkRouter FLUXPLACE-REQUESTS.md)

Branch `events2` (worktree /var/kicad/fluxplace-wt/events2, from la-next 27763f3). Same rules as EVENTS.md: emitting
never raises or slows a stage; tests without pcbnew/router; one commit per item `EVENTS2-Gn: ...`; run tests/test_events*.py,
tests/test_snapshot.py, tests/test_lookahead.py after each. No real-board runs. Update docs/EVENTS-CONTRACT.md with every
new field in the same commit.

G1 snapshot opens: `snapshot.full/delta` fill `opens` with the text connectivity count (`routeconn.text_open(B)` length or
the helper krtchain uses; B is already loaded, do not load twice). Callers that pass opens keep theirs.
G2 progress pct: routestatus progress hook and the lookahead/placetour `_progress` set `pct` when they know it:
lookahead = (round-1 + done candidates / candidates in round) / max_rounds * 100; placetour = rounds done / max_rounds
* 100; pairs = laid pairs / total. Otherwise null (unchanged).
G3 per-candidate iteration: lookahead main process emits `iteration(loop="lookahead", i=<candidate index in run>,
metrics=<fit dict without lists>, note=<tag>)` as each candidate result lands (and loop="ghost" per landed ghost if not
already). On this branch placetour does not exist yet; add a note in EVENTS-CONTRACT.md that placetour emits
`iteration(loop="place-cand", i, metrics=judge key fields, note=<cand id>)` per judged candidate (it will be wired at merge).
G4 board metrics: `snapshot.full` adds `metrics: {vias, track_mm: {layer: mm}, net_mm: {net: mm} (signal nets only),
parts, pads, density}` computed from the same B (arc length from a/mid/b). stage_end gets the same `metrics` (from the forced
stage-end snapshot) as a field.
G5 relative paths: TOURNAMENT.md png paths and `artifact.path` are relative to the stage dir / workdir W (the contract says
which); absolute only as a fallback outside W.
G6 run.json: add `board_in` (absolute input board) and per stage_end `board` stays; document that boards may live outside W.
Report: hashes, tests, deviations.
