# KiCad 11 without pcbnew

## KiCad 11 — what works without `pcbnew` (2026-11-10)

KiCad deprecated the SWIG `pcbnew` python module in 9.0 and plans to drop it in
11; its replacement (the IPC API, `kicad-python`) needs a running GUI and has no
headless mode yet. As of 2026-11-10 there is **no KiCad 11 release** (latest
stable 10.0.8; the public `kicad/kicad:nightly` image is an 8-month-old
10.0.0-rc1), so nothing here could be run against a real KiCad 11 — it is
verified by removing `pcbnew` from the interpreter instead.

| Capability | Without `pcbnew` | How |
|---|---|---|
| `review` (every gate), `lock`, `fab`/`deliver`'s review step | **works** | `fluxplace/boardfile.py` reads the `.kicad_pcb` S-expressions directly and builds the same `facts` dict. `review.board_backend()` picks `swig` when `pcbnew` imports, else `file`; `FLUXPLACE_BACKEND=file\|swig` forces one. |
| DRC, ERC, gerbers, drill, renders | **works** | already `kicad-cli` |
| `place`, `repair`, `patch`, `finish`, `tune`, `planes`, `verify-models`, DSN export | **needs KiCad <= 10** | ~300 `pcbnew` call sites in 25 modules; the port target is `kicad-python` once KiCad ships a headless API server |

Contract: `tests/test_boardfile.py::test_file_backend_matches_swig` compares the
two backends field by field (set `FLUXPLACE_TEST_BOARD` to add a real board). On
utv-comms V1.5 (150 footprints, 2473 track segments) they agree exactly apart from
footprint bounding boxes (max 0.002 mm), and a full `review` gives identical
findings on both. `fluxplace doctor` prints the active backend.
