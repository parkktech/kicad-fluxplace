# Phase 2b recipe: KRT + AI repair loop inside `close` (D7 guide only, D2 packet mode, D8)

Repo `/var/www/html/kicad-fluxplace`, `/usr/bin/python3`. Edit only `fluxplace/gaps.py`, `tests/test_gaps.py`, and a new
`tests/test_gaps_ai.py`. Do not commit. No boards, no kicad-cli in tests. Read each callee's def line + docstring first.
Reuse `critroute._safe`, `critroute._fit` (or copy its clipping logic) rather than re-inventing.

## 1. KRT summary capture (`_r_krt`)

Insert `["--json-out", js]` just before the `"--nets"` element of the command (`js = out + ".krt.json"`). After the
run, if `js` exists, `ctx["krt_last"] = js`. Return also `"failed": len(failed_single)`, `"open": len(open_single)` read
from it (missing keys -> 0).

## 2. New rung `ai` (LEVEL "L1"), last in `DISPATCH["open-free"]` and `DISPATCH["open-pour"]`

ctx gains `ai_mode` (`cons["ai"]["mode"]`, default `"packet"`; `"off"` -> the rung returns `{"status": "ai off"}`),
`ai_dir = os.path.join(work, "ai")`, `attempts` (the attempts file path), `cons_path`.

`_r_ai(base, out, gaps, ctx)` works on ONE gap: the first of `gaps`. `sid = critroute._safe(gap["id"])[:80]`.
- `act = ai_dir/sid.action.json`. If it does not exist: write `ai_dir/sid.packet.json` = `gap_packet(base, gap, ctx)`
  and return `{"status": "packet"}` (no board written).
- If it exists: `a = load_ai_action(act, base)`; rename it to `sid.action.used.json` (overwrite); apply (section 4);
  return `{"status": "applied", "commit": bool, "why": first reason or "", "tokens": a["tokens"]}`.

`close` changes:
- result `"packet"` for a rung that returned status `packet`: attempt line `result "packet"`, not memoised.
- `ai_tokens` of the attempt line = summary `tokens` when present.
- New stop `"packets"`: a round with no accepted rung but at least one packet written -> stop `packets`, `exit 5` in
  stop.json (before the `flat` check). The module `__main__` exits with `stop["exit"]`.
- At most 2 action files applied per gap id per work dir (count `*.action.used.json` renames recorded in gaps.json
  under `"ai_used": {gap_id: n}`); after 2 the rung returns `{"status": "ai budget"}`.

## 3. `gap_packet(base, gap, ctx) -> dict` (<= 4096 bytes, keys exactly)

```json
{"v": 1, "gap": {"id", "kind", "class", "net"},
 "conn": {"i", "a": {"x","y","layer","pin","id"}, "b": {...}, "straight_mm", "rules": {...}},
 "window": {"box": [...], "layers": [...], "tracks": [...], "vias": [...], "via_sites": [...]},
 "sealed_by": [...], "krt": {"failed": [...], "open": [...]},
 "tried": [{"method", "result", "why"}],
 "actions": "rip {net, ids}; nudge {ref, dx, dy, rot}; route {nets, layers}; give_up {why}. No copper coordinates."}
```
- `conn`: from `handroute.open_nets(base, constraints=cons_path)["connections"]`, the first record whose `net` is the
  gap's net (its index `i` in that list). No record -> packet with `conn: null`.
- `window`: `handroute.window(base, conn=i, constraints=cons_path, margin=2.0)` -> keep `window`, `layers`; `tracks`
  and `vias` nearest the connection midpoint, at most 20 each, each with `id, net, layer, locked` and endpoints/pos
  rounded to 0.01; `via_sites` at most 10.
- `sealed_by`: `handroute.suggest(base, net, conn=i, constraints=cons_path).get("sealed_by")` -> its
  `limiting_obstacles` (at most 8, keys `id, net, layer, locked, border_cells`).
- `krt`: from `ctx["krt_last"]` json if present: `failed_single`, `open_single` filtered to the gap's net (lists, at
  most 5).
- `tried`: lines of the attempts file with this gap id (last 6).
- Clip: drop `krt`, then halve `tracks`/`vias`, until <= 4096 bytes.

## 4. Applying an action file (one transaction)

`load_ai_action(path, base) -> {"actions": [...], "tokens": int, "why": str}`: JSON `{"actions": [...], "tokens": n,
"why": "..."}`. Valid actions, anything else dropped with a log line (never raises):
- `{"do": "rip", "net": str, "ids": [8+ hex chars]}` (at most 12 ids)
- `{"do": "nudge", "ref": str, "dx": num, "dy": num, "rot": num}` with |dx|,|dy| <= 1.0 mm
- `{"do": "route", "nets": [str], "layers": [copper layer names]}` (at most 6 nets)
- `{"do": "give_up", "why": str}`

Apply:
1. `cur = base`. Nudges first (any position in the list): for each, refuse unless the part has <= 3 pads, is not in
   `[placement] fixed` (read via `routable._fixed_set` or the constraints list) and not locked; then
   `boardwrite.move_file(cur, tmp, {ref: (x+dx, y+dy, angle+rot)})` with absolute values read via pcbnew;
   `legality.Legal(RD.load(tmp), cons).part_issues(ref)` must be empty, else drop the nudge. `cur = tmp`.
2. `w = ai_dir/sid.work.kicad_pcb`; remove stale `w` and `w + ".session"`; `handroute.session_begin(cur, w)`.
3. Rips: `handroute.shove(w, net, ids=ids, op="unlay", constraints=cons_path)` (session mode, `out=None`).
4. Routes: for each net in `route.nets`: run the KRT command of `_r_krt` with src = `w`, dst = `w + ".krt.kicad_pcb"`,
   only = [net], layers = `route.layers` or `signal_layers(w)`; if dst exists, `shutil.copy(dst, w)`. Then for each
   connection of that net still in `handroute.open_nets(w)`: `suggest(w, net, conn=i, layers=...)` and if found
   `lay(w, net, path, constraints=cons_path)` (session mode).
5. `give_up` -> skip steps 2-4, return status `"give_up"` with the why (attempt result refused, why = the AI's why).
6. `r = handroute.session_commit(w, out, constraints=cons_path)`; return `commit = r["accepted"]`,
   `why = (r["reasons"] or [""])[0]`. `finally`: remove `w`, its session dir and temps.
`close` then measures `out` and runs `_accept` exactly as for every other rung.

## Tests (`tests/test_gaps_ai.py`, fakes only)

1. No action file -> packet written, keys exactly as section 3, <= 4096 bytes; attempt `result "packet"`; stop `packets`
   with exit 5 when nothing else was accepted.
2. Packet not memoised: a second close in the same work dir writes the packet again.
3. `load_ai_action` drops: unknown `do`, a rip with no ids, a nudge with dx 2.0, a route with a non-copper layer.
4. Apply order: nudge before session_begin; rip calls shove with `op="unlay"`; route calls KRT then suggest/lay;
   session_commit decides; temps removed (fake every callee, record the call order).
5. A nudge on a fixed part or a part with 4 pads is dropped.
6. `ai budget` after two used action files for the same gap.
7. `_r_krt` adds `--json-out` before `--nets` and fills `ctx["krt_last"]`.
8. `[ai] mode = "off"` -> rung status `ai off`, no packet.

## Done when

Full suite passes. Report diff stat, the pytest line, and every recipe point that did not match the code.
