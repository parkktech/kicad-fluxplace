# progress

Tells "progressing" from "looping" in a long AI-driven repair session, and recovers the accepted board after the agent's
context was compacted. State: `<workdir>/.fluxplace/progress.json` (`accepted`, `history`, `blocked`); the call log
`<workdir>/.fluxplace/calls.jsonl` ([calls.md](calls.md)) is read for the verdict.

    fluxplace progress checkpoint BOARD [--note T] [--metric k=v ...] [--measure] [--workdir W]
    fluxplace progress block ITEM --reason T [--workdir W]
    fluxplace progress show [WORKDIR] [--json]
    fluxplace progress brief [WORKDIR] [--max-chars 2500] [--json]

**Use `brief` after a context compaction or at the start of a new session**; it prints the accepted board, metrics,
blockers, verdict, commands not to retry and the last checkpoints, so no log needs re-reading.

- `checkpoint` appends `{ts, board, rev, metrics, note}` and sets `accepted` (board path, `rev` = first 12 hex of the file's
  sha256, ts, note). `--measure` runs `kicad-cli pcb drc --severity-error --format json --refill-zones` on a temp COPY
  (the `.kicad_pro`/`.kicad_dru` are copied beside it; the original is never refilled) and stores `unconnected` and `errors`;
  a failed measure writes nothing. Any `--metric key=value` number is stored (lower is better: `pairs_not_done`, ...).
  It ends with `brief: fluxplace progress brief <workdir>`.
- `show` prints the accepted board path first, the metric table, blockers, then the verdict. Exit 0 progressing, 1 stalled, 2 looping.
- Verdict (thresholds `--loop-n` 3, `--stall-k` 5, `--max-calls` 60; calls are those since the last checkpoint):
  - looping: the same argv failed (non-zero rc) N+ times; a metric went up and down twice+ over the last 6 checkpoints with no
    net gain; a blocked item's text appears in more than N calls' argv.
  - stalled: no lower metric across the last K checkpoints, or more than M calls since the last checkpoint.
  - progressing otherwise; no history and no call log: `progressing - no data`.
- `brief` order: accepted board/rev/note, metrics and change since the first checkpoint, blockers, verdict + evidence, failed
  commands to not retry (top 10), last 5 checkpoints, then the hand-route ledger tail (`<board>.session/ledger.txt`, read only,
  if present). Over the cap the oldest history goes first; items 1-3 are never cut.
- `rev` is the same definition as `boardrev.rev` (first 12 hex of sha256); progress.py has a local copy.
