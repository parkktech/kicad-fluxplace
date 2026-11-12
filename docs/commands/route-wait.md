# `route-status` and `route-wait`

## No-poll routing (`route-status`, `route-wait`)

Every router subprocess (KiCadRoutingTools stages in `auto`/adaptive/stall, Freerouting in `finish`)
runs through `fluxplace.routestatus.run_logged`: the raw log goes to `<out>.router.log` and a small
`<out>.status.json` (stage, state, nets_total/routed/failed/ripped/done, phase, current net, elapsed_s,
eta_s, stalled_s, checkpoint, checkpoint_n, pid, rc) is rewritten at most every 3 s. Agents never tail the log:

    fluxplace route-status BOARD_OR_STATUS            # ONE line; exit 0 running/done, 1 failed, 3 none
    fluxplace route-wait STATUS --timeout 900 [--until done|stall|checkpoint] [--stall S]
        # blocks; prints one line + result path (+ read-only diagnosis of unrouted nets on finish)
        # exit 0 condition met, 1 run failed/dead, 2 timeout, 3 no status file

The log parser reuses routediag's start/end regexes and also reads `[28/201 (2 failed) (55 ripped)]`
progress lines and `[REROUTE 329/357 (28 failed)]`; Freerouting pass/unrouted counters are read on a
best-effort basis (no real Freerouting 2.x log was available to verify the wording).
