# Safe-write checks on the hand-route commands (`probe` `suggest` `lay` `shove` `unlay` `session`)

**Plan tied to a board revision.** `probe`, `suggest`, `open-nets`, `window` `--json` gain a top-level `board_rev`
(first 12 hex of sha256 of the board file bytes). `lay` / `shove` / `unlay` accept `--expect-rev R`: if the board's rev
differs, nothing is written, exit 2, stderr `stale plan: board changed since rev R (now X); re-run suggest/probe`.
Without the flag nothing changes.

**One-line verdicts.** `--json` of `probe lay shove unlay suggest` and `session commit` starts with `summary`, e.g.
`lay rejected: probe failed: clearance 0.066 < 0.127 vs BT3_RF track at 156.3,130.6`. `--brief` prints only that line
(exit code unchanged), so an agent need not read the full JSON.

**KiCad-open guard.** KiCad writes `~<name>.kicad_pcb.lck` (JSON `hostname`, `username`) beside a board it has open.
`lay shove unlay` (target `--out` or the session work board), `session begin` (WORK) and `session commit` (`--out`)
refuse with `board is open in KiCad (lock <file>, user U on host H): close it or pass --force-open` (exit 2).
An unreadable lock counts as held; a stale lock after a crash also blocks (use `--force-open`). `pcbnew` Load/Save and
`kicad-cli` create no lock, so fluxplace never blocks itself. Library: `fluxplace/boardlock.py` (`held`, `guard`).
