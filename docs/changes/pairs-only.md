# Changes: pairs-only branch (pair tools hardening)

- `tune`: `--budget S` (default 90 s x selected pairs, 180 s x selected `--match` groups), `--stall N` (default 8 DRC-guarded tries in a
  row without an accepted step, then the pair/net is left), `--report` (default `OUT.tune.json`). Budget end, SIGTERM or SIGINT write the
  best ACCEPTED board to `--out` (the input when nothing was accepted), remove `*.work.kicad_pcb`, `*.try.*`, `.drc.json`, `.kicad_prl`
  and KiCad lock files, and exit 2; the report has `"stopped": "budget" | "stall" | "signal" | null`. Zone refill is skipped after a signal.
  Pairs whose legs run on different layer sequences / via counts, or have an open leg, are `needs_reroute` up front (no tries spent) and
  named in the summary line; exit 1 when any pair is still over limit or needs a re-route.
- `pairs --only A,B`: re-route named pairs whole on a routed board, guarded and restorable (see `docs/commands/pairs.md`), `--allow-shove`, `--budget`.
- `qualaudit.not_done`: a net over an explicit via limit (`[nets.X] max_vias` or `[rf] max_vias`) counts under `rf` with cause `via_count`
  unless `[review] waive` has `RF_VIA_COUNT:^NET:`. Report levels (WARN/FAIL) unchanged. `rf_rows(..., nets=[...])` filters by net; `rf_rows`
  is a whole-board call (one row per finding, each with its `net`), not a per-net call.
- `pairs.crosscheck_external(pairs, summary_or_log)`: pairs an external pair router did not recognise or did not route as a pair.
