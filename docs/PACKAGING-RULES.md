# Packaging rules (owner, 2026-11-27)

Standing rules for every package `fluxplace deliver` writes, on every board, every version. A package is **not ready**
unless all eight hold. The gate runs on every `deliver`, first version included. The deliver gate checks each one and prints one line per rule (`P1 PASS ...` / `P1 FAIL ...`);
any FAIL means `deliver` exits non-zero and writes the folder marked `NOT-READY` (never silently "ready").
A rule is satisfied either by the check passing or by a recorded reason (waiver with source / debt row); a reason
is listed in README-FIRST, never hidden.

| # | Rule | How the gate checks it |
|---|------|------------------------|
| P1 | Every differential pair meets the **hard limit of its interface** (the value where the link or a spec actually breaks), including pairs re-laid by a re-net or incremental run. A tighter project/vendor *target* is reported as WARN with the measured value, never as FAIL. | Pairs measured on the delivered board; over the interface hard limit = FAIL; between target and hard limit = WARN in README-FIRST. Owner 2026-11-27: do not block on unrealistically precise limits (e.g. 1000BASE-T intra-pair skew of 13-25 mm = ~2 % of a symbol: WARN, not FAIL). Hard limits per interface: reports/ETH skew limits line vs PHY side.md (board repo) and the pair-skew report. |
| P2 | Review is judged against the previous shipped version (`--baseline PREV`) with the project's own waivers, 3D lib and datasheets in place: 0 FAIL, WARN count not worse than the baseline. | `review --baseline`: new findings (matched by code + refs, worse severity = new) = FAIL; inherited ones listed. First version of a board: no baseline, plain review must be 0 FAIL. |
| P3 | DRC with every rule evaluated and zones refilled: 0 errors, 0 unconnected. | kicad-cli `pcb drc --severity-all --all-track-errors --refill-zones` under the input project's rules (never relaxed severities). |
| P4 | Both a PCBWay set and a JLCPCB set: BOM + CPL in each house's format, gerbers each house accepts, DNP parts excluded from both and listed, every part with an LCSC pick or marked consigned with a reason. | Both sets present and parse; DNP refs absent from every CPL/centroid and from the JLC BOM; the PCBWay BOM keeps each DNP part only as a line marked DO NOT POPULATE (for the human assembler, see P5); DNP refs listed in README-FIRST; no JLC BOM line without LCSC # or consigned reason. |
| P5 | Previous version's BOM fields (Manufacturer, Notes, DNP lines) carried forward in the human-readable (PCBWay) BOM; changed lines updated; new parts added. | Diff vs `--prev-bom`: no Manufacturer/Notes dropped, every prior DNP line present unless the part was removed (listed). |
| P6 | Package contents: assembly instructions (previous text carried, only changes edited), order worksheet, README-FIRST (version, what changed since PREV, status of every prior KNOWN OPEN item), review/ (schematic pdf, ERC, netlist, review json), renders (top/bottom/iso), datasheets for every MPN, the KiCad project (pcb/sch/pro + project lib). | Manifest check: every item present and non-empty; every prior KNOWN OPEN item has a status line; assembly text never claims a BOM line the BOM lacks. |
| P7 | Title block and silk carry the `[project] version` from the constraints. | Title block rev and silk version text equal `[project] version` (never derived from the file name). |
| P8 | Every footprint has a resolving 3D model, or a recorded model debt row. | Review MODEL_FILE_MISSING = FAIL unless a debt row names the ref + reason. |

Source: owner via ParkkRouter (1.5.3 delivery review, 2026-11-27). Enforcement: `deliver` gate (branch w106-deliverver),
with P1 tuning (w104-renettune), P2 baseline (w105-baseline), P4 JLC set (w107-jlcset).
