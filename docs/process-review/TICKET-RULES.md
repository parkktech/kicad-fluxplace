# Rules for every fluxplace ticket agent (lead: Opus; 2026-11-26, session 6)
- Spec: docs/process-review/PLAN-2026-11-26.md: your ticket row (section 2 or 3) PLUS its revisions in sections 6 and 7 (grep your ticket id). Background: FABLE-REVIEW-2026-11-26.md, read by section only.
- Step 1: grep /var/www/html/kicad-fluxplace/docs/ONE-SHOT-LEDGER.md for your symptom/area; read matching rows; do not repeat a rejected design.
- Worktree: /var/kicad/fluxplace-wt/<branch> (create with `git -C /var/kicad/fluxplace-wt/w16merge worktree add /var/kicad/fluxplace-wt/<branch> -b <branch> 78bc1b5` if missing). Work only there. Never edit w16merge or another agent's worktree. In the main checkout /var/www/html/kicad-fluxplace only append ledger rows to docs/ONE-SHOT-LEDGER.md (commit that file alone).
- Board-agnostic: no ref designators, net names or board constants in code; board intent lives in constraints/spec. One flow: no new CLI commands or flags per fix; extend the gap->repair dispatcher / stage gates.
- Tests: /usr/bin/python3 -m pytest (pcbnew). Targeted tests while working; at the end run `/var/www/html/kicad-fluxplace/tools/merge-gate.sh /var/kicad/fluxplace-wt/<branch>` once (~25 min, run it in the foreground of a background shell and wait for its notification) and report its last lines. Do not merge.
- Shared machine (32 cores, 62 GB). Real-board check only when `free -g` shows >= 6 GB available; one pipeline process at a time per agent. Scratch: /tmp/claude-1000/-var-kicad-utv-comms-bridge/5ae3e714-3ef4-4907-beb4-9f896259f67a/scratchpad/<branch>/ (the old session scratchpad was wiped; recreate what you need). W16 boards: /var/kicad/utv-comms-bridge/W16/stageA/models-1/out.kicad_pcb (route input), /var/kicad/utv-comms-bridge/W16/view/finish2-route.kicad_pcb (old best routed), constraints W16/inputs/constraints-route.toml, spec W16/inputs/spec.json. Copy the board WITH its .kicad_pro/.kicad_prl into scratch.
- Never pkill/killall; kill only explicit PIDs you started. Leave no background process running when you finish.
- Ledger: one row per issue found (date | area | issue | cause | fix | measured result naming the test id that enforces it).
- Commit on your branch; message ends with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01FSTSpBRTp2qW7mF1VZnbVY
- Final reply <= 15 lines: branch + sha, what changed (files), tests added, merge-gate result, real-board measurement vs the ticket's acceptance, open items. Never claim a check you did not run.
