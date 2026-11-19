# Phase 1 recipe: `gaps.py`, pipeline stage `close`, attempt log

Source: `docs/FLOW-REDESIGN.md` sections 2.3-2.7, 2.12, 5 (phase 1). Repo: `/var/www/html/kicad-fluxplace`.
Python: `/usr/bin/python3` (has `pcbnew`). Do not commit. Do not edit any file outside the list below.
Do not touch any board under `/var/kicad`. No new CLI command, no new flag.

## Files

| file | change |
|---|---|
| `fluxplace/gaps.py` | new, about 350 lines |
| `fluxplace/pipeline.py` | `"close"` after `"route"` in `STAGES` and `CONSTRUCTIVE`; `st_close`; `FUNCS["close"]` (about 25 lines) |
| `fluxplace/calllog.py` | add `attempts_path(board)` (5 lines) |
| `tests/test_gaps.py` | new |
| `tests/test_pipeline.py`, `tests/test_calllog.py` | adjust only what the stage list / new function breaks; add one test each (below) |

Existing repair functions are called unchanged. If one cannot be called as written below, stop and report; do not edit it.

## `calllog.attempts_path(board)`

```python
def attempts_path(board):
    """.fluxplace/attempts.jsonl beside the board (same location rule as calls.jsonl)."""
    return os.path.join(os.path.dirname(log_path([board])), "attempts.jsonl")
```
Writes go through `append(rec, path)` (same size cap). Not gated by `enabled()` except under pytest: write when
`enabled()` is true OR when a `path` was passed explicitly to `close` (tests pass one).

## `fluxplace/gaps.py`

Module docstring: one paragraph pointing to FLOW-REDESIGN 2.4-2.7 and 2.12; says phase 1 covers the open, DRC and via
kinds (L0-L1) and uses a local guard `_accept` that phase 2 replaces with `drcguard.accept`.

### Constants

```python
KIND_OF_TYPE = {"shorting_items": "short", "solder_mask_bridge": "short",
                "clearance": "clearance", "hole_clearance": "clearance", "hole_to_hole": "clearance",
                "track_dangling": "dangling", "via_dangling": "dangling",
                "track_not_centered_on_via": "via-offcentre"}
ORDER = ["short", "open-gap", "open-pour", "open-free", "clearance", "dangling", "via-offcentre"]
CLASS_RANK = {"hs-pair": 0, "rf": 1, "power": 2, "pair": 3, "plain": 4}
```
Every other violation type becomes kind `"other"` (classified, counted, never dispatched).

### `measure(board, kicad_cli="kicad-cli") -> dict`

One DRC per board: `d = drcguard.drc_json(board, kicad_cli)` (severity-all, refilled, completed by `drcfull.complete`).
Cache by `(abspath, mtime_ns, size)` in a module dict holding only the last 4 entries. Returns
`{"drc": d, "opens": len(unconnected_items), "shorts": n shorting_items, "errors": n violations with severity "error",
"err_keys": Counter of drcguard.key_of(v) for violations with severity "error"}`.
`errors` excludes unconnected items (same meaning as `progress.measure`/`versions` "errors").

### `classify(d, cons=None) -> list[gap]`

Input: a DRC dict. Gap: `{"id", "kind", "class", "nets", "at", "layer", "evidence"}` (no `tries`; tries live in the memo).
- Unconnected item `u`: `net = drcfull.net_of(u)`. Zone item (any item description matching `drcguard._ZONE_DESC`) ->
  `open-pour`. Else if both items have `pos` and their distance (mm, kicad-cli JSON units) <= `joingaps.GAP` ->
  `open-gap`. Else `open-free`. (Phase 1 does not split `open-sealed` with `closure.run`; the `open-free` rung list
  ends with the sealed rungs. Write this as a one-line comment.)
- Violation `v`: kind from `KIND_OF_TYPE`, else `"other"`. `nets` = every `[NAME]` net in its item descriptions
  (use the same regex as `drcfull.net_of`, all matches, deduplicated, sorted).
- `id`: unconnected -> `f"{kind}:{net}:{desc_a}:{desc_b}"` with the two descriptions sorted and passed through
  `drcguard._DECIMAL.sub("#", ...)`; violation -> `kind + ":" + json.dumps(drcguard.key_of(v))`. Duplicate ids get
  `#2`, `#3` suffixes in order.
- `class`: `"rf"` if any net in `cons["rf"]["nets"]`; `"pair"` if any net is in
  `krtchain.pair_nets(cons, all_net_names, None)` (wrap in try/except -> no pairs); else `"plain"`.
- `at`: first item `pos` as `[x, y]` rounded to 0.01, else `None`. `layer`: `None` (phase 1).
- `evidence`: `v["description"]` clipped to 120 chars.
Sort: `ORDER` index (kind `"other"` last), then `CLASS_RANK`, then id.

### `score(m, gaps) -> list`

The R4 vector of 2.6: `[hs_pair, rf, shorts, opens, pair, errors, quality]` where `hs_pair = 0` (phase 3),
`rf` = gaps with class rf, `pair` = gaps with class pair, `shorts = m["shorts"]`, `opens = m["opens"]`,
`errors = m["errors"]`, `quality` = gaps of kind `dangling` + `via-offcentre` + `other`. Compared as Python lists.

### `_accept(base_m, base_board, cand_m, cand_board, target_ids, cand_gaps) -> (ok, why)`

Phase 1 guard, mode `repair`, in this order; the first failing rule gives `why`:
1. R0 `audit.connectivity_diff(audit.snapshot(base_board), audit.snapshot(cand_board))["ok"]` must be True, else
   `why="R0 " + first violation text` (clipped 100). Cache the base snapshot by `(path, mtime_ns, size)`.
2. R1 `cand_m["shorts"] <= base_m["shorts"]`.
3. R2 `drcguard.new_keys(base_m["err_keys"], cand_m["err_keys"])` empty (`why="R2 new DRC key <type>"`).
4. R4 `score(cand) < score(base)` (`why="R4 score not lower"`).
5. Target: at least one id of `target_ids` absent from `cand_gaps` (`why="target gap still there"`).

### Rungs and `DISPATCH`

A rung is `fn(base, out, gaps, ctx) -> dict` (a short summary, at most 6 keys, no paths). It writes a candidate board
at `out` or writes nothing. `ctx` = `{"cons": dict, "cons_path": str|None, "kicad_cli": str, "work": str,
"budget_s": float, "log": callable}`. `nets` below = sorted union of `g["nets"]` over `gaps`.
Before every rung `gaps.close` copies `base`'s `.kicad_pro` (and `.kicad_dru` if present) next to `out`.

| name | call |
|---|---|
| `joingaps` | `joingaps.run(base, out, nets=nets)` |
| `pourisland` | chain over `nets`: `pourisland.repair(cur, tmp, net, constraints=ctx["cons_path"])`, `cur` = last written tmp; copy the last to `out` only if any repair wrote a board |
| `patch` | `patch.patch_board(base, out, rip=False, cons=ctx["cons"], log=ctx["log"])` |
| `patch_rip` | `patch.patch_board(base, out, rip=True, cons=ctx["cons"], log=ctx["log"])` |
| `handrepair` | `w = os.path.join(ctx["work"], "hr-work.kicad_pcb")`; remove `w`, `w + ".session"` if present; `handroute.session_begin(base, w, ctx["kicad_cli"])`; `handrepair.repair(w, out, constraints=ctx["cons_path"], budget=min(900, ctx["budget_s"]), layers=signal_layers(base))`; remove the session dir afterwards. `session_commit` (called inside `handrepair.repair`) is the transaction guard for now. |
| `region` | `repairregion.repair(base, out, nets=nets, constraints=ctx["cons_path"], kicad_cli=ctx["kicad_cli"], log=ctx["log"])` |
| `drcfix` | `drcfix.loop(base, out, kicad_cli=ctx["kicad_cli"], cons=ctx["cons"], log=ctx["log"])` (read `drcfix.loop`'s signature; pass only these) |
| `launder` | `launder.launder_board(base, out, kicad_cli=ctx["kicad_cli"], dangling=True, log=ctx["log"])` |
| `snapiter` | `snapiter.run(base, out, log=ctx["log"])` |

Read each callee's signature before writing its adapter; argument names above are from the source at efce7d8.
Exceptions inside a rung are caught by `close` (result `error`, `why` = `type: message` clipped to 120).

```python
DISPATCH = {
    "short": ["drcfix"],
    "open-gap": ["joingaps", "patch"],
    "open-pour": ["pourisland", "region"],
    "open-free": ["patch", "handrepair", "region", "patch_rip"],
    "clearance": ["drcfix", "region"],
    "dangling": ["launder"],
    "via-offcentre": ["snapiter"],
}
LEVEL = {"region": "L1", "patch_rip": "L1"}   # every other rung is L0
```
`RUNGS = {name: fn}` maps names to the adapters. A comment above `DISPATCH`: pour-tie, planetie, pair and RF rungs
join in phase 3 (FLOW-REDESIGN 5).

`signal_layers(board)`: copper layer names of the board (pcbnew `GetEnabledLayers().CuStack()`, names via
`GetLayerName`) minus every layer where one zone's bounding-box area is >= 0.8 x the board edge bounding-box area.
Board-agnostic; no layer names in code.

### `close(board, work, cons_path=None, cons=None, kicad_cli="kicad-cli", budget_min=None, max_rounds=40, attempts=None, log=print) -> dict`

1. `os.makedirs(work)`. `best = work/best.kicad_pcb`; copy `board` (+ `.kicad_pro`, `.kicad_dru`) there.
   `cons = cons or constraints.load(cons_path)`. `budget_min = budget_min or (cons.get("flow") or {}).get("budget_min", 120)`.
   `attempts = attempts or calllog.attempts_path(board)`. `run_id = "r-" + time.strftime("%Y%m%d-%H%M%S")`.
   `tool` = short git hash of the fluxplace repo (`git -C <repo> rev-parse --short HEAD`, `""` on error).
2. `m = measure(best)`; `gaps = classify(m["drc"], cons)`; `start = {opens, shorts, errors, score}`.
3. Memo: `set` of `(kind, rung, fp)` with `fp = versions.copper_fingerprint(best)`. Load/save it inside
   `work/gaps.json` as `{"gaps": gaps, "memo": [...], "round": r}` (rewrite after every attempt).
4. Round loop `r = 0 .. max_rounds-1`:
   - no gaps of a dispatched kind -> stop `clean` (gaps of kind `other` alone also stop `clean`, reported).
   - for `kind` in `ORDER` with gaps, for `rung` in `DISPATCH[kind]`:
     - key in memo -> one attempt line with `result "skipped"`, `why "memo"`; continue.
     - `out = work/close/r{r}-{kind}-{rung}.kicad_pcb`; run it, time it.
     - no board at `out` -> `refused`, `why "no board written"` (or the method's own status if its summary has one).
     - else `cm = measure(out)`, `cg = classify(cm["drc"], cons)`, `_accept(...)`.
     - accepted: replace `best` atomically (`shutil.copy2(out, best + ".tmp")`, `os.replace(best + ".tmp", best)`,
       same for `.kicad_pro`), `m, gaps = cm, cg`; result `closed` if every target id is gone else `partial`;
       break out to the next round (reclassify; no extra DRC).
     - refused: add the key to the memo.
     - one attempt line per try (schema below).
   - a round with no accepted rung -> stop `flat`.
   - elapsed > `budget_min * 60` -> stop `budget` (checked before every rung too).
   - after `max_rounds` -> stop `rounds`.
5. Write `work/stop.json`: `{"stop", "exit": 0 if clean else 2, "level": highest level of an accepted rung or "L0",
   "minutes", "start", "end": {opens, shorts, errors, score}, "attempts": n, "left": [{id, kind, class, last_method,
   why} for every gap left, last_method/why from its kind's last attempt]}`. Write `work/ledger-rows.md`: one markdown
   table row per kind left: `| date | close | <kind> x<n> left | <last why> | open |`.
6. Return the stop dict plus `"best": best`.

Attempt line (append with `calllog.append(rec, attempts)`), keys exactly:
`{"ts", "run", "board_fp", "tool", "stage": "close", "level", "gap": {"id": first target id, "kind", "class": class of
the first target, "n": len(targets)}, "method": rung, "tier": {"scope": "board", "effort": 1}, "in": {"nets": len(nets)},
"result", "why", "score": [before, after or null], "s": seconds rounded 0.1, "drc_runs": 1 or 0, "precheck": null,
"ai_tokens": 0}`. `board_fp` = `versions.copper_fingerprint(best)` before the try, first 8 chars if longer.

### CLI entry for measuring (no new command)

`python3 -m fluxplace.gaps BOARD --work DIR [--constraints C] [--classify-only]` via `if __name__ == "__main__":` with
argparse. `--classify-only`: `measure` + `classify`, print a kinds histogram (`kind class n`) and seconds, write
`DIR/gaps.json`, exit 0. Otherwise `close(...)` and print one line:
`close: opens A->B, shorts A->B, errors A->B, attempts N, stop S, M min, best DIR/best.kicad_pcb`. This is a module
entry, not a `cli.py` command.

## `pipeline.py`

- `STAGES` and `CONSTRUCTIVE`: insert `"close"` right after `"route"`.
- ```python
  def st_close(c, n, board):
      """Gap loop (gaps.py): classify, dispatch, guard; W/best.kicad_pcb, W/gaps.json, W/stop.json."""
      from . import gaps as GP
      res = GP.close(board, c.dir(n, "close"), c.cons_path, c.cons, log=lambda m: None)
      line = (f"close: opens {res['start']['opens']}->{res['end']['opens']}, errors {res['start']['errors']}->"
              f"{res['end']['errors']}, {res['attempts']} attempts, stop {res['stop']}")
      return {"verdict": "pass" if res["stop"] == "clean" else "warn", "line": line, "board": res["best"]}
  ```
- `FUNCS["close"] = st_close`.

## Tests (`tests/test_gaps.py`, no kicad-cli, no pcbnew board needed)

Use a hand-written DRC dict fixture (3 unconnected: one zone item, one 0.1 mm apart, one 5 mm apart; violations:
shorting_items, clearance, track_dangling, track_not_centered_on_via, silk_over_copper). Monkeypatch `gaps.measure`,
`audit.connectivity_diff`, `versions.copper_fingerprint` and the entries of `gaps.RUNGS` with fakes that write a
dummy file.

1. `test_classify_kinds`: kinds = open-pour, open-gap, open-free, short, clearance, dangling, via-offcentre, other.
2. `test_classify_ids_stable`: classify twice -> same ids; ids unique.
3. `test_classify_class`: a net listed in `cons["rf"]["nets"]` -> class `rf`.
4. `test_order`: sorted by ORDER, `other` last.
5. `test_dispatch_registered`: every rung name in `DISPATCH` is in `RUNGS`; every dispatched kind is in `ORDER`.
6. `test_accept_rules`: one case per rule R0, R1, R2, R4, target -> the matching `why`; one passing case.
7. `test_close_accepts_and_stops_clean`: fake rung lowers opens to 0 -> `stop == "clean"`, `best` replaced.
8. `test_close_flat_and_memo`: fake rungs never improve -> `stop == "flat"`; a second `close` call on the same work
   dir, same fingerprint -> attempts are `skipped` with `why "memo"`.
9. `test_attempt_line_schema`: every line in the attempts file has exactly the keys listed above.
10. `test_rung_error_caught`: a rung that raises -> result `error`, loop continues.
11. `test_stop_json`: fields present; `left` lists the remaining gap.

`tests/test_pipeline.py`: one test that `"close"` follows `"route"` in both flows and `FUNCS["close"]` exists.
`tests/test_calllog.py`: `attempts_path` sits beside `calls.jsonl` for a board path.

## Done when

1. `cd /var/www/html/kicad-fluxplace && /usr/bin/python3 -m pytest tests/ -q -x -p no:cacheprovider` passes
   (report the counts line).
2. `git diff --stat` shows only the files above (plus `docs/recipes/PHASE1-gaps.md`, already there).
3. Report: files and line counts, the pytest counts line, anything in this recipe that did not match the code.
