# PHASE3H: AI-finish audit fixes (Fable audit 2026-11-20, from the W14 close passes 3-4)

Worktree /var/kicad/fluxplace-wt/phase3g (branch phase3g, base 4d90c4d). Board-agnostic. Do not touch the frozen worktrees (w14-*).
G = fluxplace/gaps.py, H = fluxplace/handroute.py, D = fluxplace/drcguard.py. Line numbers are approximate: re-find by name.
Tests: one file at a time (tests/test_gaps_ai.py, tests/test_gaps.py, tests/test_handroute.py). Never run router-launching tests.

## Part A: loop, loader, budget (commit "PHASE3H-A: ...")

A1 (audit 1) Packet starvation: in `_r_ai` (~G:1289), drop open-* gaps whose net is a leg of a current pair-* gap
(`ctx["pair_gap_legs"]`), sort `todo` by `used[key]` ascending, then packet-file-missing first. Cap from `[ai] packets`
(default 24) instead of AI_PACKETS. Test: 30 gaps; the first 6 are answered and refused; the second call writes packets for other gaps. A pair leg gets no open-free packet.
A2 (audit 2) `_accept` (~G:300): if any target was removed for sev progress, or any target net's `opens_by_net` fell,
pass `target=()` to `drcguard.accept`. Test: 2 pair-open targets, one becomes pair-asym, so the change is accepted.
A3 (audit 5) Drain answers first: before the round loop, for every kind with "ai" in DISPATCH and pending files, call
`attempt(0, kind, "ai", targets)` until none are pending. Recompute targets from `m` each time and continue after accepts.
In the round loop's extra-ai `while`, drop the `not accepted` condition. Add `memo.add(key)` when a rung's result is
"error". Test: 3 pending answers, the first accepted; all 3 are consumed before any non-ai rung runs.
A4 (audit 6) `_ai_action` returns `(action, reason)`. `load_ai_action` collects `dropped=[{do, reason}]`. In `_r_ai`, if
anything was dropped: do not apply, do not charge `used`, write `_feedback_note(dropped=..., refused="action invalid")`
and return status "invalid". Test: rip + 7-net route leaves base untouched, the feedback names "route: nets > 6", `used` is unchanged.
A5 (audit 7) Named constants AI_ROUTE_NETS=6, AI_RIP_IDS=12, AI_NUDGE_MM=1.0, AI_MAX_ACTIONS (read the current
limit), AI_PAIR_WPTS=40, used by `_ai_action`, and `_PACKET_ACTIONS` built from them. The text must also state: route on signal
layers only; one net per rip; nudge needs rot, part <= 3 pads, not fixed; the envelope `{"actions":[...],"why","tokens"}`; the apply
order nudge -> rip -> path -> pair_path -> route; a stitching via is `path "via x,y"`. Test: every constant's value appears in the text.
A6 (audit 10) Rename the file to `.used.<n>.json` (n = next free) and charge `used` only AFTER `_apply_ai` returns. On an
exception, write `_feedback_note(refused="error: ...")`, then rename and re-raise KeyboardInterrupt/SystemExit. Test: `_apply_ai` raises
KeyboardInterrupt, so `action.json` is still pending.
A7 (audit 12) After the drain, if any answer was consumed without an accept, call the ai `attempt` once more (it rewrites
packets with feedback), so the stop is "packets"/exit 5, not "flat". In stop.json add `ignored`: every `*.action.json` left,
with a reason (budget, no such gap, kind has no ai rung). Test: one refused answer gives stop "packets", and the packet has feedback.
A8 (audit 13) For rung "ai" with pending names non-empty, skip the memo lookup and `memo.add` (the rename is the once-only guard).
Test: refuse a.json, rewrite it on the same board, and it is applied.
A9 (audit 14) Charge `used` only on status "applied" with >= 1 executed action. Reset `ai_used` when `st["tool"] != tool`.
Over budget: write feedback "ai budget n/n" and list the file in `ignored`. Test: a give_up does not charge.

## Part B: guard, packets, feedback, tools (commit "PHASE3H-B: ...")

B1 (audit 3) `session_commit` (~H:1125) gets kwarg `mode="repair"` (today's behaviour). `_apply_ai` (~G:1168) passes
`mode="edit"`: only R0/R1/R3/R5 plus the locked checks; close's `_accept` judges R2/R4/R6. Test: a session with equal
opens/errors commits in edit mode and is refused in repair mode.
B2 (audit 4) `gap_packet` (~G:1019): when there is no connection and the gap is a pair, set the window from
`HR.window(base, at=end, size=(4,4))` at the pair end with more free room (tracks/vias of both legs, plus `via_sites`). Add
`pair.short_leg` and `pair.add_mm`. Test: a skew-gap packet has `window.tracks` with ids of both legs.
B3 (audit 8) Pass `open_legs` (nets in `m["opens_by_net"]`) to `load_ai_action`. Skip the HS_STUB_MM cap when the net is in
`open_legs` (R6 still gates). Test: a 4 mm path on an open hs leg is kept; on a closed leg it is dropped.
B4 (audit 9) In `open_nets` (H:175), when both sides are the same zone, take the islands from `M.islands(net)` holding that
zone/layer's fills. Put side a on the smallest island (a point inside its fill), b on the largest, and set `closest` via
`_island_dist`. In `gap_packet`, filter `via_sites` to points inside island a's fill and add `island: {layer, bbox}`.
Test: a two-island zone gives a != b, and every via site is inside the small island.
B5 (audit 11) Feedback: a feed row per rip/nudge/route (ok, why, opens before->after). Join up to 3 commit reasons. Set
`rec["gap"]["id"] = summ.get("gap_id") or targets[0]["id"]` and `refused=None` on accept. Test: an unknown rip id gives a feed row with ok false.
B6 (audit 15) Pair packet: `i` = the first connection whose net is in `gap["nets"]` (not nets[0] only). Test: an open on nets[1] sets conn.
B7 (audit 16) Add "swap_pins" to AI_PAIR_HS_KINDS. Test: the proposal file is written for an hs-pair gap.
B8 (T1) Add a "tune" rung to DISPATCH["pair-skew"] before "ai". `_r_tune` takes the shorter leg (slave) from
`_pair_block(...)["legs"]` and calls `tune.tune_pairs(base, out, {slave: master}, limit_mm=skew_limit_mm, budget=ctx["budget_s"])`
(read tune.py for the exact signature). Test with a stubbed tune_pairs: called with the shorter leg as slave.
B9 (T2) With rip + path in one file, allow an hs path length <= HS_STUB_MM + the length of the ids ripped on that net in the
same file. Test: rip 1.5 mm + path 2.5 mm is kept.
B10 (T4) For pair-asym gaps with no connection, write a `proposals` row ("topology: legs end at different pads") instead of an AI packet.
