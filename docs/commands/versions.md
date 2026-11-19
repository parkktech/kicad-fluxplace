# versions

Board-version registry: the single answer to "which board are we on". Use it when many trial boards sit in version
folders and an agent could lose track of the current one. Code: [fluxplace/versions.py](../../fluxplace/versions.py).
Measuring reuses [progress](progress.md) (`progress.measure`, a DRC of a temp copy); `accept` writes the progress
checkpoint in-process.

    fluxplace versions status [--brief]
    fluxplace versions step "what starts now"
    fluxplace versions accept BOARD --note N [--next X] [--allow-worse]
    fluxplace versions reject BOARD --note N
    fluxplace versions new VERSION --from BOARD --reason R [--decisions a,b]
    fluxplace versions init --version V --board B [--title T] [--decisions a,b]
    fluxplace versions start-context [--hook-json]

Every action takes `--root DIR` (default: the git toplevel of the cwd, else the cwd).

**Hook:** add `fluxplace versions start-context --hook-json` as a SessionStart hook command. It prints the start rule,
the status, the active version's NOW block and all warnings as `hookSpecificOutput.additionalContext` (under 2000
characters). It never fails: any error becomes a one-line warning and exit 0.

## Registry

Default `<root>/docs/resume/boards.json`; env `FLUXPLACE_VERSIONS` overrides it (relative to the root). Its folder holds
one `RESUME-<version>.md` per version.

    {"config": {"folder_pattern": "W{n}", "links": {"lib": "../hardware/lib"}, "commit": true, "start_rule": "..."},
     "active": "W11",
     "versions": {"W11": {"status": "active", "parent": "W10", "title": "...", "decisions": ["D71ca"],
                          "current": "W11/stageC/r6.kicad_pcb",
                          "history": [{"time": "2026-11-18 16:58", "board": "...", "metrics": {"unconnected": 6,
                                       "shorts": 3, "errors": 61}, "note": "..."}],
                          "rejected": [{"time": "...", "board": "...", "note": "..."}]}}}

- Exactly one version is `active`. Other statuses are free text (`superseded`, `abandoned`, ...).
- `<version>/CURRENT.kicad_pcb` (and `.kicad_pro`) is a symlink to the active version's current board.
- The resume NOW block sits between `<!-- now -->` and `<!-- /now -->`; `step`, `accept`, `reject` and `new` rewrite its
  lines (Board version, Board state, Step in progress, Next step) and its `updated` time.

## config keys (all optional)

| key | default | meaning |
|---|---|---|
| `folder_pattern` | `W{n}` | version folder names; `{n}` = digits. Validates `new`; drives the unregistered-folder warning |
| `links` | `{}` | `name -> relative target` symlinks created inside a new version folder |
| `commit` | `true` | `git add` + commit of the registry and resume dir after each write; silent no-op outside git |
| `start_rule` | a generic rule | text printed first by `start-context` |

## Rules

- `accept` refuses when opens (`unconnected`), shorts or errors rose against the last accepted entry; `--allow-worse`
  overrides (with a note). The board must sit inside the active version folder.
- `accept` compares the copper of the board with CURRENT first (`copper_fingerprint`: tracks, arcs, vias, footprints;
  zones not hashed). Identical copper prints `no change: ...` and exits **4**; nothing is recorded, moved or committed.
- The progress checkpoint refuses new DRC shorts the same way (`--allow-worse` = allow).
- `new` copies the source board to `<version>/start.kicad_pcb`, closes the old resume NOW with a pointer, and
  switches `active`.
- Warnings: CURRENT missing or not linked to the registry's current board; folders matching `folder_pattern` that are not
  registered; no resume file or a NOW block not naming the active version; board files newer than CURRENT that are
  neither accepted nor rejected (`*.session` dirs, `/undo`, `.iN`, `.guard`, `.try` copies are ignored).
