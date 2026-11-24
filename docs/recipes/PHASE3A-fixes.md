# PHASE3A fixes — Fable review of b163cc9 + breakout UUID bug

Worktree /var/kicad/fluxplace-wt/phase3a (branch phase3a). Board-agnostic code only. Do not commit. No routers.
Line numbers are from b163cc9; re-find by name.

1. **Protect: two sets** (`gaps.py`).
   - `close()`: `protect = drcguard.protected_nets(cons, drcguard.snap_nets(_snapshot(best)))` again (ALL pair legs + rf),
     passed to rungs and used for target / rip exclusion exactly as before b163cc9.
   - New `relax = legs of incomplete pairs` (pair-open/asym/skew gaps of `m["gaps"]`), recomputed after each accept.
   - In `attempt`: when `kind.startswith("pair-")`, the R5 set handed to `_accept` is `protect - set(legs of the target gaps)`;
     for every other kind it is `protect`. Plain rungs never see a pair leg as a target or rip candidate.
   - `protect_nets` becomes `relax_nets(gaps)` -> set of those legs (no board read). Update its test.
2. **A failed review source never scores as perfect.** `review_gaps` returns `(gaps, failed)` where `failed` is the list
   of source names that raised (`power`, `pairs`, `pin-escape`). `classify(..., board=)` stores `failed` on each call's
   result: return the gap list as before but set the attribute through a wrapper — simplest: `classify` gets a new kwarg
   `failed_out=None` (a list it extends). In `close`, `m["review_failed"]` / `cm["review_failed"]` hold it; `_accept`
   refuses with `why = "review source failed: <names>"` when the candidate failed a source the base did not.
3. **Stable pair ids, severity term.** Pair gap id base = `pair:<p>/<n>` for all three kinds (kind still
   pair-open/asym/skew). Add `"sev"`: open/half 3, asym 2, skew 1. In `score`, the hs_pair and pair terms become the SUM of
   `sev` (not a count). The target rule in `drcguard.accept` ("target gap still there") must, for gaps with `sev`, pass
   when the target's sev in the candidate is lower than in the base: implement in `gaps._accept` by removing from
   `target_ids` the pair ids whose sev dropped (look them up in base `m["gaps"]` and `cand_gaps`) before calling
   `drcguard.accept`, and in `close` treat such a target as "partial".
4. **`review.pair_gated`**: strip trailing digits from the family token before the `_HS_FAMILIES` lookup
   (`re.sub(r"\d+$", "", tok)`), so `USB3_...`, `PCIE2_...` gate. Also make `drcguard.py` (~line 243, its own copy of the
   gated lambda) call `review.pair_gated`. Test: `pair_gated({}, "USB3_M2_TX_P")` is True; `"SENSE_ADC_P"` False.
5. **Power magnitude.** Each power gap carries `"mag"`: POWER_WIDTH -> narrow length mm rounded to 1 mm (parse the
   finding: `power.check` builds it from `short` dicts — add `"value": tot` to the POWER_WIDTH finding dict and
   `"value": nvn - nv` to POWER_VIAS, then read `f["value"]`). `score` gets a new term right after `power`:
   `power_mag = round(sum(mag))`. Vector: `[hs_pair, rf, shorts, opens, power, power_mag, pair, pin_escape, errors, quality]`.
6. **pin-escape on routed boards.** Add `only_nets=None, partners=None` kwargs to `preroute.pin_escape_blocked(B, ...)`:
   when `only_nets` is given, grade only pads whose net is in it; a blocker that is copper of `partners.get(pad_net)` is
   ignored. `review_gaps` passes `only_nets` = open nets and `partners` = {p: n, n: p} from the pair rows.
7. **Cost.** `classify` already has the DRC dict: build the open-net set from `unconnected_items` (`_nets_of` of each
   item) and pass it to `_pair_audit(board, cons, opn=...)` instead of `RC.open_by_net`. Reuse the `B` from `_pair_audit`
   for pin-escape (one `routediag.load`). Measure `review_gaps` time on /var/kicad/utv-comms-bridge/W14/CURRENT.kicad_pcb
   before/after (target ~2.5 s).
8. **not_pairs leaks.** Pass `exclude=C.not_pairs(cons)` at `returnpath.py` (~239) and every other `diff_pairs` caller
   that has `cons` in scope (`route.py` ~380 if it has cons). `comprehend.py` `_diff_pairs` (~128): if `cons` reaches it,
   filter there; else list it. `gaps.py` CLI status (`_main`, ~1285): pass `board=a.board` to `classify`.
9. **Breakout UUIDs** (`breakout.py` ~514, `lay_text` inner `u()`): ids are `uuid5(NAMESPACE_URL, f"breakout/{cnt}")` with
   a per-call counter, so a second lay on the same board duplicates them. Make the seed unique per call and per board:
   `uuid5(NAMESPACE_URL, f"breakout/{sha1(text).hexdigest()[:12]}/{cnt}")` (keeps determinism for the same input).
   Test: two `lay_text` calls on the output of the first produce no duplicate uuids (or the unit nearest to that).

Tests: add/adjust tests for 1-6 and 9 in tests/test_gaps.py / tests/test_notpairs.py / a breakout test. Run
`tests/test_gaps*.py tests/test_notpairs.py tests/test_joingaps.py` and every test file importing gaps, drcguard, review,
preroute, breakout, power, netclass, krtchain, pipeline (skip files that launch Freerouting/KRT; list them).
Re-run the section E measurement of PHASE3A-review-gaps.md with the new `(gaps, failed)` return and print `score`.

Report under 40 lines: `git diff --stat`, tests, timings, new score on CURRENT and pairs-all, deviations.
