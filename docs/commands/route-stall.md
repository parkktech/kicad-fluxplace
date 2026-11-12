# Routing stalls

### 7. Routing stalls — `auto` / `compact` (`fluxplace/stall.py`)

* Per-stage wall-clock limit: `--route-timeout`, else `[route] stage_timeout_s`, default **900**
  (was a fixed 1800 s). On a timeout the router is not retried.
* On a timeout or any failed net: `route-diagnose` runs on the routed board and its text/JSON
  is always written (`OUT/route-diagnose-N.{txt,json}`). With `--replace-on-stall` (<=
  `--stall-rounds`, 3) the move list is applied to UNLOCKED parts (support parts re-seated
  via clusters, others moved to within ~3 mm of their target pad and legalised), only the
  tracks/vias of nets touching a moved part (plus the failed ones) are removed
  (`boardwrite.strip_nets`), and only those nets are re-routed. What moved is printed.
* Escape-first: nets of fine-pitch ICs (pitch <= 0.5 mm, >= 5 pads) are routed in a first
  router pass, everything else (power included, wide) in a second with that copper kept
  (`--no-escape-first` turns it off). **Implemented as net order, not as escape stubs or
  temporary lane keep-outs:** neither external router takes a per-net keep-out, and no
  router was run in this work to verify one.
* Verified with a stub router in `tests/test_stall.py`; a live KiCadRoutingTools run of
  `auto` was not done.
