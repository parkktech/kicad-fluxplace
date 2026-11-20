# PHASE3A — review findings as gap kinds and score terms (D71cj a, D71ci)

Repo: /var/www/html/kicad-fluxplace. Python: /usr/bin/python3 (pcbnew importable). Tests: `cd /var/www/html/kicad-fluxplace && /usr/bin/python3 -m pytest -q tests/<file>`.
Rule: board-agnostic code only — no board refs, net names or constants from any one project in `fluxplace/`.
Do not commit. Do not touch any `.kicad_pcb` except reading. Keep diffs small; extend existing functions.

## A. not-a-pair list (D71ci)

1. `fluxplace/constraints.py`: add
   ```python
   def not_pairs(cons):
       """Nets the owner declared plain signals although their names look like a pair (top-level `not_pairs = [...]`)."""
       return set((cons or {}).get("not_pairs") or [])
   ```
2. Honour it everywhere a pair is detected. Grep every caller of `graph.diff_pairs`, `krtchain.pair_nets`,
   `netclass._pairs` and `pairaudit.pair_list` (`grep -rn "diff_pairs\|pair_nets\|_pairs(\|pair_list(" fluxplace/`).
   - `graph.diff_pairs(signal_nets, exclude=())`: new keyword `exclude`; drop any mapping where either leg is in `exclude`.
   - `netclass._pairs(names, cons)`: pass `exclude=C.not_pairs(cons)` to `diff_pairs`, and skip the family-suffix
     loop for names in it. A not-a-pair net then gets its non-pair class (whatever `classify` gives a plain net).
   - `krtchain.pair_nets(cons, names, report)`: remove `C.not_pairs(cons)` from both return paths.
   - `drcguard.protected_nets(cons, names)`: pass `exclude=C.not_pairs(cons)` to `graph.diff_pairs`; subtract not_pairs from the result.
   - Any other caller that has `cons` in scope: pass the exclude. Callers without `cons`: leave, list them in your report.
3. Project file (data, not code): `/var/kicad/utv-comms-bridge/hardware/utv-comms/constraints-v20.toml` — add
   `not_pairs = ["SENSE_ADC_P", "SENSE_ADC_N"]   # D71ci: DC divider tap, plain signals` as a top-level key ABOVE the
   first `[table]` line. Also `grep -n SENSE_ADC /var/kicad/utv-comms-bridge/W14/CURRENT.kicad_pro`; if a netclass
   pattern assigns them a diff/pair class, report the line (do not edit the .kicad_pro).
4. Test (`tests/test_gaps.py` or a new `tests/test_notpairs.py`): `netclass._pairs(["A_P","A_N","B_P","B_N"], {"not_pairs":["A_P","A_N"]})`
   has B legs only; `krtchain.pair_nets({"pairs":{"A_":{}}, "not_pairs":["A_P","A_N"]}, ["A_P","A_N"], None) == []`.

## B. review gaps in `fluxplace/gaps.py`

Add `def review_gaps(board, cons):` -> list of gap dicts in the same shape `classify` emits
(`kind, class, nets, at, layer, evidence`; `id` assigned by the caller). Each source in its own try/except so one
failing source never kills the measurement (on exception append nothing and `log` nothing; return what worked).
Sources (reuse, do not reimplement):

| kind | source | class | id base |
|---|---|---|---|
| `power-width` | `power.check(facts, cons)` findings code `POWER_WIDTH`; `facts = boardfile.facts_from_file(board)` | `power` | `power-width:<net>` |
| `power-vias` | same, code `POWER_VIAS` | `power` | `power-vias:<net>` |
| `pair-open` | `qualaudit.pair_rows(B, cls, cons, opn)` status `half` or `open` — build B/cls/opn exactly as `review._pairs_from_audit` does (`routediag.load`, `netclass.classify`, `routeconn.open_by_net` with `text_open` fallback) | see below | `pair-open:<p>/<n>` |
| `pair-asym` | same, status `mismatch` and causes contain `layers` or `vias` | see below | `pair-asym:<p>/<n>` |
| `pair-skew` | same, status `mismatch` and causes == `["skew"]` | see below | `pair-skew:<p>/<n>` |
| `pin-escape` | `preroute.pin_escape_blocked(B)` (B from `routediag.load(board)`; check what B `preroute.run` expects and use the same loader) findings `PIN_ESCAPE_BLOCKED` | `plain` | `pin-escape:<ref>` |

- Pair class: `hs-pair` when `review` would gate the pair — factor review's inline `gated` lambda
  (`m.startswith(fams) or m.upper().split("_")[0] in _HS_FAMILIES`) into `review.pair_gated(cons, master)` and call it
  from both places; else `pair`. Pairs whose legs are in `C.not_pairs(cons)` are skipped.
- `nets`: the net (power), `[p, n]` (pair), `[]` (pin-escape). `evidence`: the finding msg / row `why`, cut to 120.
  `at`: None (fine; rungs use nets). Add `"refs": [...]` with the finding refs.
- Status `unknown` rows are ignored (not graded).

Wire it in:
1. `classify(d, cons=None, board=None)`: when `board` is given, append `review_gaps(board, cons)` through the same `add()`
   so ids stay unique, before the final sort.
2. `ORDER`: `["short", "mask-opening", "power-width", "power-vias", "pair-open", "pair-asym", "pair-skew", "open-gap",
   "open-pour", "open-free", "clearance", "dangling", "via-offcentre", "pin-escape"]`.
3. `score(m, gaps)` -> `[hs_pair, rf, shorts, opens, power, pair, pin_escape, errors, quality]` where
   hs_pair = gaps with class `hs-pair` and kind starting `pair-`; power = kinds `power-width`/`power-vias`;
   pair = class `pair` and kind starting `pair-`; pin_escape = kind `pin-escape`; rf stays as now (class rf, any kind);
   quality unchanged. Update the docstring. Keep the R4 comparison (`list <`) as is.
4. Every `classify(...)` call inside `gaps.close` and the rung helpers gets `board=<the board that was measured>`
   (grep `classify(` in gaps.py; each one measures a specific file — pass that path). Callers outside gaps.py:
   leave unchanged (board=None keeps old behaviour); list them in your report.
5. DISPATCH: do NOT add rungs for the new kinds (phases 3B/3C do). The loop already skips kinds without a DISPATCH entry.

## C. protect only audit-complete pairs

In `gaps.py` add `def protect_nets(board, cons, gaps):` = `drcguard.protected_nets(cons, drcguard.snap_nets(_snapshot(board)))`
minus the legs of every pair that has a `pair-open`/`pair-asym`/`pair-skew` gap in `gaps` (i.e. keep rf nets and legs of
complete pairs only). In `close()`: replace the `protect = drcguard.protected_nets(...)` line with
`protect = protect_nets(best, cons, m["gaps"])`, and recompute `protect` the same way every time an accepted candidate
replaces `m` (find each assignment to `m` inside `attempt`; `protect` must be `nonlocal` there; ctx is rebuilt per attempt
so it picks up the new value).

## D. Tests (add to `tests/test_gaps.py`)

- `score` on a hand-made gap list with one of each new kind returns the 9-term vector with the right counts.
- `classify(d, cons, board=None)` equals the old output (no review gaps).
- `review_gaps` with monkeypatched `power.check`, `qualaudit.pair_rows`, `preroute.pin_escape_blocked` (and the loaders)
  returns the expected kinds/classes, skips `unknown` rows and not_pairs legs.
- `protect_nets` drops legs of a pair with a `pair-skew` gap and keeps a complete pair.
Then run: `/usr/bin/python3 -m pytest -q tests/test_gaps.py tests/test_gaps_ai.py tests/test_joingaps.py` and every test
file that imports drcguard, netclass, krtchain, graph or review (`grep -l "drcguard\|netclass\|krtchain\|graph\|review" tests/*.py`).
Report pass/fail counts; failures that also fail on `git stash` (pre-existing) are listed separately.

## E. Measurement (report these numbers exactly)

```
cd /var/www/html/kicad-fluxplace && /usr/bin/python3 - <<'EOF'
import time, collections
from fluxplace import gaps, constraints as C
cons = C.load("/var/kicad/utv-comms-bridge/hardware/utv-comms/constraints-v20.toml")
for b in ["/var/kicad/utv-comms-bridge/W14/CURRENT.kicad_pcb", "/var/kicad/utv-comms-bridge/W14/stageB/pairs-all.kicad_pcb"]:
    t = time.time(); g = gaps.review_gaps(b, cons); dt = time.time() - t
    print(b.split("utv-comms-bridge/")[1], f"{dt:.1f}s", collections.Counter((x["kind"], x["class"]) for x in g))
    print("  protect drops:", sorted(set(gaps.drcguard.protected_nets(cons, gaps.drcguard.snap_nets(gaps._snapshot(b)))) - set(gaps.protect_nets(b, cons, g)))[:20])
    print("  SENSE_ADC in protect:", [n for n in gaps.protect_nets(b, cons, g) if "SENSE_ADC" in n])
EOF
```
Also time one full `gaps.measure` + `classify(..., board=...)` on CURRENT.

## Report (under 40 lines)
Files changed with line counts (`git diff --stat`), test counts, the measurement output verbatim, callers left unchanged,
anything in this recipe that did not match the code (with what you did instead).
