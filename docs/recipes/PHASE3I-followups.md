# PHASE3I follow-ups: finish B1/B2/B5/B6 and fix the review findings (Fable review 2026-11-22 of c3b81ce..eca56bd)

Worktree /var/kicad/fluxplace-wt/phase3g (branch phase3g, base eca56bd). Board-agnostic: no refs, nets or coordinates in code.
One flow: no new command and no new CLI flag; behaviour comes from constraints tables and the gap -> repair loop.
G = fluxplace/gaps.py, H = fluxplace/handroute.py, P = fluxplace/pipeline.py, K = fluxplace/krtchain.py, LP = fluxplace/lockpolicy.py,
PO = fluxplace/pairsonly.py, PFB = fluxplace/pairfb.py, D = fluxplace/drcguard.py. Line numbers are approximate: re-find by name.
Tests: one file at a time, `/usr/bin/python3 -m pytest -q -p no:cacheprovider tests/<file>`. Never run a router-launching test.
One commit per step, message "PHASE3I-F<n>: <title>". Each step is at most ~300 changed lines with its tests. Do the steps in order.
If a step cannot be done as written, stop, write what you found under "Open" at the end of this file, and go on to the next step.

Rules for the tests in this recipe: build a real small board with pcbnew (copy the helper of tests/test_power_lay.py or
tests/test_edgesliver.py); do not monkeypatch the function under test or the guard it calls (`G._accept`, `D.accept`, `H.lay`),
only the routers (`G._call_rung`, `PO` lay callbacks).

## Findings fixed here (file:line at eca56bd)

| # | Where | Sev | Finding |
|---|---|---|---|
| R1 | K:323 `_route_nets` | high | KRT (the first router) routes `("*",)`: an open gated pair leg is laid as a plain net; `diff` step also takes it. Only `_freerouting` excludes (P:903). |
| R2 | LP:10 `level`, H:1336 `shove` | high | Hard set is only `[rf].nets` + name-pairs. RF found by `review.is_rf`, `[route] controlled`, `controlled_patterns`, `[nets]` impedance/max_vias, `[pairs.*]` family legs and `not_pairs` are ignored -> their locked copper is "free". With no constraints every locked item is free (before B5: refused). |
| R3 | H:1336 `shove` | high | Soft and free locked copper is ripped with no re-lay and no record; the plan's "re-lay in the same guarded edit" is not enforced. |
| R4 | G:1924 memo key, G:1457 `window_hash` | high | Window = 3 mm square at `at`: copper freed anywhere else on the net's path never re-opens the rung (good repair skipped for the whole work dir). Track crossing the window with both ends outside, moved parts and zones are not in the hash. |
| R5 | G:1919 | high (speed) | `window_hash` calls `pcbnew.LoadBoard` once per target per attempt (hundreds of loads per rung); this time also runs the stall clock. |
| R6 | G:1919 | med | Gaps with `at: None` (all pair-*, power-*, pin-escape) fall back to the whole-board fingerprint; the per-pair `pairgroup` key (PHASE3E 7) was removed, so every accept re-runs `pairgroup` on every pair. |
| R7 | G:2011 `memo.add` | med | An "error" result (crash, timeout) is persisted like a refusal and now survives accepts and passes. |
| R8 | G:2090 stall | med | "stalled" stops before the `ai` rung writes a packet: exit 2, nothing for the orchestrator to answer; at a gate this is "fail" instead of exit 5. |
| R9 | G:1845/1933/1955 `_scope_gaps` | med | Scoping filters `m["gaps"]` itself, so `_accept` no longer sees rf/power gaps: a gate repair may worsen R6 `rf`, `power`, `power_mag`, `pin_escape`. Scope must select targets, not blind the guard. |
| R10 | P:1417 | med | `led.after()` runs before the gate: the stage ledger says "pass" for a stage whose gate failed. A resume from a later stage never re-checks the gate. A re-run after packets re-runs the whole stage and drops the gate's accepted repairs. |
| R11 | H:1411 `session_commit` | med | `GP._remeasure(start)` / `(S.work)` refill and SAVE the session start board and the live session board. Measure copies. |
| R12 | H:1268 `replay` | med | Replays at default width/via (explicit `width`, `via`, `any_angle`, `lock` of the original lay are lost); no R6 `_accept` (plan B4). |
| R13 | fluxplace/edgesliver.py:31 | med | Band test is inverted: every pad-less fill outline that meets the inner box is a "sliver"; `limit` then becomes the largest such island and is saved in the zone. `edgesliver` is first for `open-pour`, so real islands are deleted instead of stitched. |
| R14 | PO:300 group re-lay | med | Sub-call passes `budget=None` (a fresh full budget); `unlock` given for the named pair also unlocks the unnamed blocker; the blocker's row is a stub (`before`/`after` empty); order of rip is not asserted (blocker re-laid with the blocked pair's copper still down gives the same path). |
| R15 | H:1077 `lay` | low | No neck-down: a `[power]` net cannot be laid narrower into a fine-pitch pad (plan B4 "within a pad-pitch of a pad"). |
| R16 | fluxplace/adaptive.py:34 | low | `report_slot` file is never unlinked (one leaked `.drc.json` per call). |
| R17 | G `_rank` | low | A `packet` result counts as a failed `ai` try, so `ai` sinks below rungs that never ran. Count only applied answers. |
| R18 | tests/test_pairgroups.py | low | 2 failures predate c0e6f18 (same 2 fail there): stale tests (`pair_legs` kwarg; `pair-skew` dispatch gained `tune` in PHASE3H B8). |
| R19 | fluxplace/breakout.py:36,517; H:115-121 | high | Every breakout track/via UUID starts with `b4e0a7c1`; H prints 8-char ids and `_resolve` takes any prefix match, so an id resolves to the wrong item. |

## F1. Gated pairs never reach KRT as plain nets (R1) - do this first

1. PFB: add `gated_open_legs(cons, pair_legs, open_names, extra=())` -> set. Pair partners from `graph.diff_pairs(pair_legs,
   exclude=constraints.not_pairs(cons))`; a pair is gated when `review.pair_gated(cons, master)`; return both legs of every gated
   pair with a leg in `open_names | set(extra)`.
2. K stage (~K:296): `hs_block = PFB.gated_open_legs(c.cons, pn, names, hand)`. Remove `hs_block` from `diff` before
   `diff_groups` (KRT's `diff` step is a router, not the pair builder). Keep the `pairgroups` step: it is the pair builder.
3. K `_route_nets`: move the set logic to a pure function `route_net_set(allnets, failed_legs, hs_block, still_open)`:
   returns `("*",)` only when nothing is excluded, else `sorted(allnets - failed_legs - (hs_block & still_open))`.
   `still_open` = open nets re-read after the `pairgroups` step (a pair that step completed is no longer excluded).
4. K stage result: `res["hs_excluded"] = sorted(...)` and one line "N gated pair leg(s) left to the pairs gate: ...".
5. P `_freerouting` (~P:902): replace the report-based `handoff_hs_nets` union by `PFB.gated_open_legs(c.cons, <pair legs in todo>,
   <open nets from conn(board)>, hand)`; keep `handoff_hs_nets` for its test. A resume without pairs state is then covered.
Tests (tests/test_stage_gates.py): `gated_open_legs` (gated pair open -> both legs; plain pair -> none; `not_pairs` respected);
`route_net_set` (no exclusion -> `("*",)`; excluded legs absent; completed pair back in). Fix R18 in tests/test_pairgroups.py in
this commit: give the lambda `**kw`, and expect `["pairgroup", "tune", "ai"]` for `pair-skew`.

## F2. Gate scope selects targets, the guard sees everything (R9, R10, gate budget, impedance)

1. G `close`: delete the three `_scope_gaps(...)` calls. Add inside `close`: `def vis(gs): return gs if kinds is None else
   [g for g in gs if in_scope(g, kinds)]`. Use `vis(m["gaps"])` everywhere targets are chosen: the answer-drain loops, the
   round loop `targets = ...`, `sub_attempt`'s `cur`, the `clean` test, and `left`/`packets` of stop.json. `_accept`, `score`,
   `relax_nets`, `_protect` keep the full `m["gaps"]`.
2. Clean test with a scope: `stop = "clean"` only when `vis(m["gaps"])` is empty (a scoped gap with no DISPATCH rung keeps the
   gate open: stop "flat"). Without a scope the test stays as it is.
3. Impedance: where review pair findings become gaps (~G:105-110), add kind `pair-impedance` for a PAIR_IMPEDANCE_OFF finding
   (class hs-pair / pair by `pair_gated`, same row shape). Add it to `in_scope`'s pair-kind tuple, to `relax_nets`' kinds, to
   `score_terms` only through the existing `kind.startswith("pair-")` (give it `sev`), and to `GATE_DEFAULTS["pairs"]`.
   DISPATCH `"pair-impedance": ["pairgroup", "ai"]`; add to ORDER after `pair-skew`.
4. Gate budget: P `_gate_check` passes `budget_min=float((c.cons.get("flow") or {}).get("gate_budget_min", 20))`.
5. P `run` (R10): move the gate block above `led.after(...)` so the ledger records the gated verdict. Store `res["gate"]` in the
   stage's state entry. At start, when `i0 > 0`: for every earlier stage with `gate_kinds` whose state has no
   `gate.stop == "clean"`, print the gate's stop.json path and return exit 5 without running a stage.
6. P `run`, re-run after packets: when `<n>-gate/stop.json` has `stop == "packets"` and `<n>-gate/best.kicad_pcb` exists, do not
   call `FUNCS[name]`: copy best (+ `.kicad_pro`/`.kicad_dru`) to `<n>-gate/resume.kicad_pcb` and run `_gate_check` on it.
7. `GATE_DEFAULTS`: add `"power": ["power-width", "power-vias"]` only if a stage of that name is in `STAGES_`; else add the
   stage name that lays rails and say which in the commit message. `open-pour` is in no default gate (B6: judged at the end).
Tests (tests/test_stage_gates.py): real small board with one rf gap and one hs pair gap, `kinds=["pair-open"]`: a stub rung that
closes the pair but adds an rf gap is refused with "R6 rf worse"; scoped undispatchable gap -> not clean; ledger verdict after a
failed gate is not "pass"; resume past an unclean gate exits 5; packets re-run does not call the stage function.

## F3. Memo and watchdog (R4-R8, R17)

1. G: `_board_rows(board)` cached on `(abspath, mtime_ns, size)` (keep 2 entries): ONE `pcbnew.LoadBoard`, returns tracks/arcs
   (net, layer, start, end, width), vias, pads (net, ref, pos), footprints (ref, pos, rot, layer). `window_hash(board, gap)`:
   box = bbox of the pads of the gap's nets, padded `WINDOW_MM`; no nets -> `at` +- `WINDOW_MM`; neither -> whole-board
   fingerprint. Hash = sorted rows of every track/arc whose segment bbox meets the box (not only its ends), vias, and
   footprints whose position is in the box. This covers R4, R5, R6 (pair gaps get a per-pair window again).
2. `attempt`: `wins = sorted(window_hash(best, g) for g in targets)`.
3. R7: results "error" go to an in-memory `memo_err` set (checked like memo, never written to gaps.json).
4. R8: when the stall test fires, call `attempt(r, kind, "ai", vis-targets)` once for every kind in ORDER that has gaps and "ai"
   in DISPATCH, then `stop = "packets"` if `n_packets` rose, else "stalled".
5. R17: in the yields block, skip `result == "packet"` (neither a try nor an accept).
Tests (tests/test_gaps_memo.py, real board): copper added far from the box -> key unchanged; a track crossing the box with ends
outside -> key changes; a blocker removed on the net's path 10 mm from `at` -> key changes; 50 targets -> `LoadBoard` called once
(count via monkeypatch of `pcbnew.LoadBoard` wrapper only); error result not in saved memo; stall -> packet written, stop "packets".

## F4. KRT-first B2: `[ai] mode = "session"`, briefs and exit 5 (part 1 of 2)

Files in `<work>/ai/`: `sessions.json` (list of regions), `<rid>.brief.json`, `<rid>.done.json` (written by the agent).
1. G `_r_ai`: when `ctx["ai_mode"] == "session"` call `_r_ai_session(base, out, gaps, ctx)` (new, below) and return its result.
   `close` already puts `ai_mode` in ctx; check and add if missing.
2. `_r_ai_session`, brief phase (no pending `<rid>.done.json`): take the gaps with AI budget left that are in no region with
   status "open"; `cluster_regions(...)`; at most `_ai_packets(cons)` regions per call. Per region: `rid` =
   `critroute._safe("region-" + first gap id)[:60]`; session board `ai/sessions/<rid>-<n>.kicad_pcb` with n = first free
   integer; `H.session_begin(base, that_path)`; brief = `{"v": 1, "region": rid, "session": path, "ledger": <Sess dir>/ledger.txt,
   "box": ..., "nets": ..., "gaps": [{"id", "kind", "class", "nets", "at"}], "packets": [gap_packet(base, g, ctx) path for the
   first 3 gaps], "rejected": <the gaps' feedback notes>, "tactics": ["suggest", "lay", "lay --stitch", "shove (unlocked only)",
   "undo"], "done": "write <rid>.done.json = {\"ledger\": <path>} when finished"}`. Append the region to sessions.json with
   status "open", gap ids, nets, box, session path, `base_fp` = copper fingerprint of base.
   Return `{"status": "packet", "n": <regions>, "briefs": [paths]}` (the same status the packet mode returns; read how `close`
   maps a rung summary to result "packet" and match it).
3. `_packet_rows`: add one row per open region: `{"kind": "session", "region": rid, "brief": path, "session": path}`.
   P: where exit 5 prints packet paths, print the brief paths too.
4. `_ai_pending_names(ai_dir, gaps)`: also return `<rid>.done.json` for every open region in sessions.json that holds one of
   `gaps` and whose done file exists (so the answers-first drain and the memo's content hash work unchanged).
Tests (tests/test_gaps_session.py, real board with 4 open nets in two corners): two regions with disjoint nets, two briefs,
two session dirs with unique paths, result "packet", stop.json lists both, a second call writes no duplicate region.

## F5. B2 part 2: merge on the next run of the same command

1. `_r_ai_session`, merge phase (a pending `<rid>.done.json` exists): take ONE region (oldest). Merge dir `ai/merge/<rid>/`
   with `m0.kicad_pcb` = copy of `base`. `r = H.replay(ledger, merge_dir, constraints=ctx["cons"])`.
   Nothing applied -> region status "refused", feedback note with the requeue reasons, rename done file to `.used.<n>.json`,
   return `{"status": "refused", "why": first reason}`. Else copy `r["board"]` to `out`, region status "merged", write the
   requeue rows to the gaps' feedback notes (`_feedback_note(..., refused=...)`), charge `ai_used` for the region's gap keys,
   rename the done file, return `{"status": "applied", "applied": len, "requeue": len, "gap_id": first gap id}`.
   The close guard judges `out` like any rung; a guard refusal leaves base untouched and the gaps open for a new brief.
2. H `replay` (R12): ledger rows carry the lay arguments. In `_write_text_ledger` add a line
   `    args: {"width":..,"via":..,"any_angle":..,"lock":..}` for op `lay` (store them in the ledger entry in `lay`/`_edit`);
   `accepted_lays` returns them; `replay` passes them to `lay`. Old ledgers without the line replay at defaults.
3. H `session_commit` (R11): measure copies. `tmp = tempfile.mkdtemp(dir=S.dir)`; copy `start` and `S.work` (+ `.kicad_pro`,
   `.kicad_dru`) there, run `_remeasure`/`_classify_into`/`_accept` on the copies, `shutil.rmtree(tmp)` in `finally`.
4. H `replay`: after the rows of one ledger, run the same R6 check once (`m0`-of-this-ledger vs current, on copies, via a shared
   helper `_r6_refusal(base, cand, cons, kicad_cli, protect)` used by `session_commit` too). Refused -> current board goes back
   to the board before this ledger, every row is requeued with the reason.
Tests (tests/test_replay.py, real board, real `H.lay`): two sessions lay disjoint nets -> both merge, opens fall by the sum; a
second session's lay crossing the first's copper is refused and requeued, the rest of it applies; a lay made with `width=0.4`
replays at 0.4; `session_commit` leaves the bytes of `start.kicad_pcb` unchanged; an R6-worse ledger is rolled back whole.
Remove the `DG.accept` / `G._accept` monkeypatches from tests/test_power_lay.py and assert the refusal on a real 2-net board.

## F6. Lock policy: complete hard set, soft re-lay rule, levels in the loop (R2, R3)

1. LP `level(net, cons, names)`: HARD when the net is in `D.protected_nets(cons, names)` and is (a) rf (`[rf].nets` or
   `review.is_rf`), or (b) a leg of a gated pair (`graph.diff_pairs(names, exclude=not_pairs)` or `krtchain.pair_nets(cons,
   names, None)` with `review.pair_gated`), or in `rip.controlled_nets(None, cons, names)` with a reason other than
   "plain copper". SOFT: other pair legs, audio. FREE: the rest. `names is None` or `cons` empty/None -> HARD (fail closed).
2. H `shove`: hard -> as now (explicit `allow_locked`, every item named). Soft, no `allow_locked`: allowed only inside a
   session; record each removed item exactly as the `allow_locked` path does (`_removed_track_record`, "unlocked-and-removed"
   ledger line) with `"level": "soft"`. Outside a session: refused "soft-locked copper is ripped only in a session, where
   commit checks the re-lay". Free locked: allowed, recorded the same way.
3. H `session_commit`: for every net with a soft record: refuse unless the net's open count is not above the session start,
   the pair audit of its pair is `complete`, and its new copper is locked. Reuse `_active_locked_reescapes`.
4. G: `_protect_levels(board, cons) -> {net: level}`; `ctx["lock_levels"]`. `_r_handrepair` passes the SOFT nets to handrepair
   as rippable blockers; a candidate that ripped a soft net is written only when the same rung re-laid it (else no output).
   `_r5_set` is unchanged: the guard still refuses any trade that opens a protected net.
5. Pin escape at lock time: expose the PIN_ESCAPE_BLOCKED check of review.py as `review.pin_escape_blocked(board, cons) ->
   set of "REF.pad"`. In `pairs.py`'s lock writer and in `H.lay(lock=True)`: compute before and after; new blocked pads ->
   the lay is refused ("blocks the last escape of ...") / the pair is not locked and gets a `pin_escape` note in its row.
Tests (tests/test_lock_policy.py): `is_rf`-named net, `[route] controlled` net, `[pairs.X]` family leg -> HARD; `not_pairs`
net -> FREE; no constraints -> HARD; soft rip outside a session refused; soft rip + re-lay + lock commits, soft rip without
re-lay is refused at commit; a locked lay across the only escape of a neighbour pad is refused.

## F7. Pair group re-lay and lay neck-down (R14, R15)

1. PO group block: pass the remaining seconds of `bud` as `budget` (0 or less -> skip the group). A blocker with locked items
   is grouped only when its level is not HARD, or `unlock` is set AND the blocker's audit after the re-lay is `complete` with
   skew and via count not above its `before`; else `group_refused = "blocker hard-locked"`. Use the sub-report's full row for
   the blocker (no stub). In the sub-call both pairs are ripped before either is laid, and the blocked pair is laid first:
   read `reroute`'s order and make it explicit for `force` pairs.
2. H `lay`: explicit `width < need` is accepted when the path is at most `max(1.0, 2 * need)` mm long and one end is within
   that distance of a pad of the net (neck-down); the result carries `"neck": true`. Else the refusal stays.
Tests (tests/test_pairsonly_group.py, tests/test_power_lay.py): sub-call budget <= remaining; hard-locked blocker without
`unlock` -> kept_original with the reason; order of lay callbacks is blocked, then blocker; neck-down accepted at a pad,
refused mid-rail.

## F8. Edge sliver, DRC slot leak, pours at the end (R13, R16, B6)

1. edgesliver `sliver_islands`: depth `d = clearance_mm + SLIVER_DEPTH_MM` (constant 1.0, `[flow] sliver_depth_mm`). An outline
   is a sliver only when its bbox lies wholly inside one edge strip: `bb.right <= left + d or bb.left >= right - d or
   bb.bottom <= top + d or bb.top >= bottom - d` (edge box coordinates), it holds no pad AND no via of the net, and its area is
   at most `[flow] sliver_max_mm2` (default 2.0). `repair` never raises a zone's min island area above that cap.
2. adaptive.py `drc_unrouted`: unlink the report after reading (try/finally).
3. B6: signal-layer pours are judged at the end: no gate default lists `open-pour` (assert in a test), and the final `close`
   (kinds None) dispatches it. DISPATCH `open-pour` stays `["edgesliver", "pourisland", "region", "ai"]`.
Tests (tests/test_edgesliver.py): an island in the board middle is not a sliver and the zone setting is unchanged; an edge
sliver is removed; an edge-strip island with a via is kept.

## F9. Item ids that are unique (R19)

1. H: `_id_len(M)` = shortest n >= 8 at which every `o.id[:n]` in `M.obs` is unique (cache on the model). `_sid(i, M=None)`
   prints `i[:_id_len(M)]`; pass M at every call site that has one (18 call sites; ledger and packet rows included, also
   G `gap_packet` obstacle ids). `_resolve(M, ident)`: exact full id wins; else prefix matches; more than one match ->
   `ValueError("id <ident> is not unique: " + up to 8 matches as "<full id> <kind> <net> <layer> at x,y")`. `shove`, rip and
   every other `--ids` consumer go through `_resolve` (grep `startswith(` on ids in H, hrgeom.py, handrepair.py, G `_apply_ai`).
2. breakout.py: the marker moves to the tail: `str(uuid.uuid4())[:-8] + MARK` (random head, last 8 hex = MARK).
   fanout_helper.py `already`: a breakout item is one whose uuid ends with `BREAKOUT_UUID` or (legacy boards) starts with it.
3. Preflight inside the flow (no new command): `boardtext`-level function `reuuid_collisions(board) -> {"collisions": n,
   "changed": m}`: group every item uuid by its first 8 hex; in a group of more than one, give all but the first a new
   uuid4-headed uuid, keeping a `MARK` tail if the old one had the MARK head or tail; rewrite every reference to the old uuid
   in the file (text replace of the full 36-char string). Call it in `H.session_begin` and at the start of `G.close` on the
   work copy only (never on the input board); log one line when n > 0.
Tests (tests/test_handroute_ids.py): a board with 3 items sharing an 8-hex head: `_sid` prints longer ids; `shove --ids <8 hex>`
is refused and lists 3 matches, the full id works; new breakout uuids differ in the first 8 and are still found by
`already`; `reuuid_collisions` leaves 0 collisions, the same item count per net, and an unchanged DRC key set.

## F10. End-to-end smoke without routers

New file tests/smoke_gate.py, run by hand: `/usr/bin/python3 tests/smoke_gate.py BOARD CONSTRAINTS` (not collected by pytest;
no board path in the file). Suggested run (about 2-4 min, kicad-cli DRC only):
`/usr/bin/python3 tests/smoke_gate.py /var/kicad/utv-comms-bridge/W15/stageB/G1-final.kicad_pcb /var/kicad/utv-comms-bridge/hardware/utv-comms/constraints-v20.toml`
The script:
1. copies BOARD (+ `.kicad_pro`, `.kicad_dru`) to a `tempfile.mkdtemp()`; records the sha1 of the original; loads constraints
   and sets `cons["ai"] = {"mode": "session"}` in memory;
2. replaces `G._call_rung` by a recorder that returns `{}` for every rung except `ai` (so no router, KRT or Freerouting starts);
3. `kinds = G.gate_kinds(cons, "pairs")`; `r = G.close(copy, work, CONSTRAINTS, cons, kinds=kinds, budget_min=5)`;
4. prints: gaps by kind (all / in scope), `r["stop"]`, rungs recorded, brief files written, `_id_len`, prefix collisions before
   and after `reuuid_collisions`, the HARD nets count from `LP`, and `PFB.gated_open_legs` for the board's pair legs;
5. asserts: stop is "clean" when no scoped gap exists, else "packets" with one brief per region and exit code 5 semantics;
   no recorded rung name is a router rung unless stubbed; every leg of every gated pair is HARD; `route_net_set` holds no leg
   of `gated_open_legs`; the original BOARD sha1 is unchanged; the temp dir is removed.
Paste the printed block into the commit message of F10. Run it on one corpus board from `fluxplace/corpus.py` too.

## Not in this recipe

Per-gap memo keys (a batch refusal does not prove each gap refused; the split in `sub_attempt` covers it). B7-B11.

## Open
