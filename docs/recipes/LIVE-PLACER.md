# Live placer view: run-script lines (F16)

The placer stages (`ready`, `floorplan`, `placetour`) show themselves when the event stream is on. Nothing here is a command
or a flag; with none of these lines a run is unchanged.

```bash
W=$O/run                                   # the stage workdir
export FLUXPLACE_EVENTS=$W/events.jsonl    # the stage stream (the pipeline sets it itself; set it for a single stage)
export FLUXPLACE_OPS=$R/W<n>/ops.jsonl     # the run-level ops feed the dashboard reads: every placer line is written there too
ln -sfn $W $R/W<n>/LIVE                    # the dashboard finds W/live and W/feedback through this link
mkdir -p $W/feedback/inbox                 # owner feedback on: without this folder nothing is read and no gate waits
```

Constraints (all optional):

```toml
[live]
model_base = "../../hardware/<project>"    # KIPRJMOD for the 3D export, relative to the constraints file; needed when
                                           # footprints name models as ${KIPRJMOD}/... (`missing` in glb_ready shows a wrong value)
glb_min_s = 60                             # (a `[live]` table, even empty, turns the 3D export on) least start-to-start time of two leader exports
glb_keep = 6                               # leader files kept (accepted and gate files stay)
[live.gate.PLACE_OK]
wait_min = 10                              # 0 or absent = the gate is shown but the run does not wait
default = "proceed"                        # on timeout: "proceed" or "stop" (exit code 5)
```

What a run leaves: `W/live/place/` (snapshots), `W/live/glb/` (3D), `W/feedback/` (items, rows, gates, log) and the lines of
`docs/EVENTS-CONTRACT.md`, section Placer. A failure of the view is a row in `W/live/errors.jsonl`, never a failed stage.
After a run: `ps -eo pid,args | grep -E "kicad-cli|liveview"` must be empty (the exporter reaps its own child).
