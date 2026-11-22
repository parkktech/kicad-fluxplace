# fluxplace events contract (v1)

Reference for the ParkkRouter dashboard. fluxplace only WRITES these files; the dashboard tails/reads them.
All paths are under the pipeline workdir `W` (`--workdir`). Board-agnostic. Emitting never raises or slows a stage.

## Files

| path | form | write rule |
|---|---|---|
| `W/events.jsonl` | append-only, one JSON object per line, UTF-8, each line < 64 KiB | `O_APPEND` single write per line |
| `W/snap/NNNNNN.json` | full board snapshot (`seq` = NNNNNN) | written whole |
| `W/snap/NNNNNN.delta.json` | delta against the last full snapshot | written whole |
| `W/snap/latest.json` | copy of the newest full snapshot | atomic replace (`.tmp` + `os.replace`) |
| `W/eta.json` | newest ETA | atomic replace |
| `W/run.json` | run header | atomic replace at start and end |

`W/run.json`: `{run, board, flow, stages, features, started, ended, verdict}` (`ended`/`verdict` null until run_end).

## Common fields (every event)

| field | type | meaning |
|---|---|---|
| `v` | int | schema version, `1` |
| `ts` | float | unix time, seconds |
| `run` | str | run id (`YYYYmmdd-HHMMSS` + pid unless set) |
| `seq` | int | per process, increments by 1 per emitted line |
| `pid` | int | emitting process |
| `stage` | str or null | pipeline stage name, env `FLUXPLACE_STAGE` |
| `kind` | str | one of the kinds below |

## Kinds

| kind | fields |
|---|---|
| `run_start` | board, flow, stages[], features{components, nets, pads, layers, area_mm2, density} |
| `stage_start` | n, name |
| `stage_end` | n, name, verdict, line, secs, board |
| `progress` | step (text), pct (0..100 or null), state |

`pct`: run_logged status sets it from the router's nets done / total; lookahead sets (rounds done + candidates landed
in the round / candidates in the round) / max_rounds x 100, 100 at done; the pipeline/placetour pct is null unless a writer
knows it. Pairs has no progress writer on this branch, so its pct is null (not wired).
| `iteration` | loop (place/pairs/route/close/lookahead/ghost/tune), i, n (or null), metrics{} (opens, shorts, overflow, hpwl, score, pairs_complete, ...), note |
| `log` | level (info/warn/error), text (<= 400 chars), src (router name / module) |
| `snapshot` | path (relative to W), full (bool), reason, opens |

`opens` (snapshot, delta, `stage_end`) = open connections by text connectivity (`routeconn.text_open` on the snapshot's
board, summed over nets; no extra load). A caller that passes its own count keeps it. `null` only if the count fails.
| `parts` | frame: [[ref, x, y, rot, side]] (placer iterations, <= 2 per second) |
| `artifact` | type (png/report/board/table), path, label |
| `eta` | secs_left, done_frac, model, basis |
| `run_end` | verdict, secs, board |

## Examples (one line each)

```
{"v":1,"ts":1763820000.1,"run":"20261122-101500-4242","seq":1,"pid":4242,"stage":null,"kind":"run_start","board":"a5-u500","flow":"default","stages":["ready","place","pairs","route"],"features":{"components":212,"nets":180,"pads":640,"layers":4,"area_mm2":2400.0,"density":0.27}}
{"v":1,"ts":1763820001.0,"run":"20261122-101500-4242","seq":2,"pid":4242,"stage":"ready","kind":"stage_start","n":1,"name":"ready"}
{"v":1,"ts":1763820004.2,"run":"20261122-101500-4242","seq":3,"pid":4242,"stage":"ready","kind":"stage_end","n":1,"name":"ready","verdict":"pass","line":"ready ok","secs":3.2,"board":"W/board.kicad_pcb"}
{"v":1,"ts":1763820010.5,"run":"20261122-101500-4242","seq":4,"pid":4242,"stage":"route","kind":"progress","step":"krtchain pass 2","pct":40.0,"state":"running"}
{"v":1,"ts":1763820011.0,"run":"20261122-101500-4242","seq":5,"pid":4242,"stage":"pairs","kind":"iteration","loop":"pairs","i":3,"n":8,"metrics":{"complete":41,"total":52},"note":""}
{"v":1,"ts":1763820012.0,"run":"20261122-101500-4242","seq":6,"pid":4242,"stage":"route","kind":"log","level":"info","text":"pass 2: 12 nets left","src":"krt"}
{"v":1,"ts":1763820013.0,"run":"20261122-101500-4242","seq":7,"pid":4242,"stage":"route","kind":"snapshot","path":"snap/000007.delta.json","full":false,"reason":"stage_progress","opens":12}
{"v":1,"ts":1763820014.0,"run":"20261122-101500-4242","seq":8,"pid":4242,"stage":"place","kind":"parts","frame":[["U1",10.0,12.5,0,"top"],["C3",11.2,9.0,90,"bottom"]]}
{"v":1,"ts":1763820015.0,"run":"20261122-101500-4242","seq":9,"pid":4242,"stage":"route","kind":"artifact","type":"png","path":"png/lookahead-1.png","label":"lookahead candidate 1"}
{"v":1,"ts":1763820016.0,"run":"20261122-101500-4242","seq":10,"pid":4242,"stage":"route","kind":"eta","secs_left":1200.0,"done_frac":0.45,"model":"table","basis":"route:nets*density"}
{"v":1,"ts":1763820900.0,"run":"20261122-101500-4242","seq":11,"pid":4242,"stage":null,"kind":"run_end","verdict":"pass","secs":900.0,"board":"a5-u500"}
```

## Snapshot (`full`)

```
{v, seq, ts, board, bbox:[x0,y0,x1,y1], layers[], edge:[[x,y],...], nets:{"id": "name"},
 parts:[{ref, x, y, rot, side, crt:[x0,y0,x1,y1], value}],
 pads:[{ref, num, x, y, w, h, rot, shape, layers[], net}],
 tracks:[{id, l, n, w, a:[x,y], b:[x,y], m:[x,y] or null, lk}],
 vias:[{id, x, y, d, n, lk}],
 zones:[{l, n, pts}],   # outline simplified to <= 200 pts
 opens}
```

`id` = first 12 hex chars of sha1 of the item's geometry + net. Stable across snapshots of the same copper.

## Delta (`NNNNNN.delta.json`)

```
{v, seq, base, add:{tracks:[], vias:[]}, del:{tracks:[ids], vias:[ids]}, parts:[changed parts only], opens}
```

`base` = seq of the full snapshot the delta applies to. A full snapshot is written every `full_every` (20) deltas
and at every stage end. Snapshots are throttled to one per `[events] snap_s` (3 s) unless forced.

## ETA (`eta.json`)

`{ts, secs_left, done_frac, model, basis, stage}`. `model` = `table` (no history) or `history`.

## Run header (`run.json`)

`{run, board, flow, stages[], features{...}, started, ended, verdict}`; written at run_start and run_end.
