# Phase 2 recipe: one measure, one guard, trades allowed (FLOW-REDESIGN 2.6, D1)

Repo `/var/www/html/kicad-fluxplace`, `/usr/bin/python3`. Do not commit. No boards under `/var/kicad`, no kicad-cli in
tests. No new command or flag. Read each callee's def line + docstring before calling it.

Scope of this sitting: `drcguard.measure` + `drcguard.accept`, used by `gaps._accept` and `handroute.session_commit`.
Deferred (later sitting, listed in FLOW-REDESIGN phase 2): `cli.py:1661,1764,1833`, `drcfix.py:443`, `repairregion`,
`snapiter`, `patch`.

## `drcguard.measure(board, kicad_cli="kicad-cli") -> dict`

Move the body of `gaps.measure` here (DRC via `drc_json`, cache by `(abspath, mtime_ns, size)`, last 4). Add:
`opens_by_net` (Counter of `drcfull.net_of(u)` over unconnected), `keys` and `err_keys` (the same Counter object).
`gaps` is not computed here. `gaps.measure` returns the plain `drcguard.measure` result; callers set `m["gaps"]`
themselves as today.

## `drcguard.accept(base, cand, mode="repair", target=(), kind=None, base_board=None, cand_board=None, score=None, locked_ok=None) -> (ok, why)`

`base`, `cand` = measure dicts. `score` = a function measure -> list (gaps passes `gaps.score` with the gap lists bound;
default: `[shorts, opens, errors]`). Rules in order, first failure gives `why`:

| rule | modes | test | why text |
|---|---|---|---|
| R0 | all | when both boards given: `audit.connectivity_diff(snap(base_board), snap(cand_board))["ok"]`; `snap` defaults to `audit.snapshot(p, checks=False)` | `R0 <first violation>` |
| R1 | all | `cand["shorts"] <= base["shorts"]` | `R1 shorts up` |
| R2 | repair | `new_keys(base keys, cand keys)` minus keys whose type maps to `kind` (only when `kind is not None`; gaps.KIND_OF_TYPE, import inside the function) is empty. Keys read as `m.get("keys", m.get("err_keys"))` | `R2 new DRC key <type>` |
| R2 | build, edit | not checked | |
| R3 | all | `locked_ok` callable given and returns False | `R3 locked copper changed` |
| R4 | repair | `score(cand) < score(base)` | `R4 score not lower` |
| R4 | build | `cand["opens"] < base["opens"]` | `R4 opens not lower` |
| R4 | edit | not checked | |
| target | repair, when `target` given | at least one target id absent from `cand["gap_ids"]` if that key exists, else skipped | `target gap still there` |

**Trades (D1):** no rule compares opens per net. A candidate that opens one plain net while the score falls is
accepted; the opened net comes back as a gap in the next classify.

## `gaps._accept`

Becomes a wrapper over `drcguard.accept(base_m, cand_m, "repair", target_ids, kind, base_board, cand_board,
score=..., snap=_snapshot)` with `cand_m["gap_ids"] = {g["id"] for g in cand_gaps}` set before the call. `snap` is the
gaps cached snapshot (checks=True), so gaps' R0 is unchanged. The score for the candidate uses `cand_gaps`; the base
uses `base_m.get("gaps") or []`. Keep its signature. Every existing test in `tests/test_gaps.py` must still pass unchanged.

## `handroute.session_commit`

Replace the three checks "unconnected not lower", "new DRC key(s)", "net(s) lost connectivity" with one
`drcguard.accept(A, B, "repair", base_board=start, cand_board=S.work, snap=_snap)` where `A`, `B` come from
`drcguard.measure_from_report(raw, severity="all")` on the two raw reports already in hand (no DRC run again).
`measure_from_report(d, severity="error")` builds the dict: `opens`, `opens_by_net`, `shorts`, `errors`, `keys`,
`err_keys`. `severity="error"` keys every error-severity violation (no zone filter; the old err_keys). `severity="all"`
keys every violation except zone violations (today's session_commit key set). `measure` uses "error".
`snap=_snap` is session_commit's snapshot (checks=False, project path). R0 therefore repeats the connectivity guard
below; both reasons may appear.
Keep every other check in `session_commit` (locked copper, re-escape closure, pairs) exactly as is. Its refusal text
keeps the `reasons` list format; the accept `why` is appended as one reason.

## Tests

`tests/test_drcguard.py` (add): one per rule and mode from the table; a trade case (one net opens, two close, score
falls) accepted in repair mode; `measure_from_report` on a fixture dict.
`tests/test_handroute.py` / `tests/test_handrepair.py`: if an existing test asserts the old "net(s) lost
connectivity" refusal, change it to the trade semantics only where the score falls; report each such change.

## Done when

Full suite passes; report diff stat, the pytest line, and each test whose expectation changed and why.

## Coordinator resolutions (2026-11-19, binding over the text above)

1. `gaps.measure` returns the plain `drcguard.measure` result; callers keep setting `m["gaps"]`.
2. `keys` and `err_keys` are the same Counter; `accept` reads `m.get("keys", m.get("err_keys"))`. No test_gaps change.
3. `accept(..., snap=None)`; default `snap = lambda p: audit.snapshot(p, checks=False)`; snapshot only when both boards given.
4. R1 why text is exactly `R1 shorts up`.
5. R2 exemption only when `kind is not None`.
6. `measure_from_report(d, severity="error")`; `severity="all"` as described in the session_commit section.
