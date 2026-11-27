"""Kickoff stamp and inputs checklist (T4, T5): `ready` records the sha of every input it judged; a later placement or route stage
compares the stamp to the inputs and re-opens `ready` (its checklists) when one changed.  Board-agnostic: inputs are files, not nets.

Inputs checklist (T4): every path made absolute at entry and listed (`manifest` in <stem>.ready.json), a .kicad_pro beside the board,
`derived_from` on a generated constraints file, `doctor` (the suite's requirements) called from ready.
`derived_from = "<source path>#<sha256 or >=12-char prefix>"` at the top level of a generated file; a source path is relative to that file."""
import hashlib
import json
import os
import time

from . import ready as RDY

GATED = ("modules", "floorplan", "fanout", "pour-tie", "placetour", "lookahead", "pairs", "place", "critroute", "plan-route", "route")
# input kind -> the ready checklists its change re-opens (names are ready line names / prefixes)
REOPENS = {"spec": ("parts", "mech", "pin", "placement", "budgets"), "constraints": ("config", "budgets", "electrical", "netclasses"),
           "schematic": ("pin", "electrical"), "datasheets": ("parts", "pin"), "parts": ("parts", "mech", "pin", "placement"),
           "project": ("inputs", "netclasses"), "mpn_map": ("parts",)}


def _sha(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
    except OSError:
        return None
    return h.hexdigest()


def _sha_dir(d):
    if not d or not os.path.isdir(d):
        return None
    h = hashlib.sha256()
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if os.path.isfile(p):
            h.update(n.encode() + (_sha(p) or "").encode())
    return h.hexdigest()


def _parts_sha(facts):
    rows = sorted((r, str(p.get("value")), str(p.get("footprint"))) for r, p in ((facts or {}).get("parts") or {}).items())
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def manifest(board, spec, cons_path, cons, ds_dir, mm_path):
    """Absolute path of every named input ({kind: path|None}); nothing is resolved through the cwd later."""
    ab = lambda p: os.path.abspath(p) if p else None            # noqa: E731
    pro = os.path.splitext(board)[0] + ".kicad_pro" if board else None
    return {"board": ab(board), "spec": ab(spec), "constraints": ab(cons_path), "project": ab(pro) if pro and os.path.isfile(pro) else None,
            "schematic": ab(((cons or {}).get("happy") or {}).get("schematic")), "datasheets": ab(ds_dir), "mpn_map": ab(mm_path)}


def shas(man, facts):
    out = {k: (_sha_dir(v) if k == "datasheets" else _sha(v)) for k, v in man.items() if k != "board" and v}
    out = {k: v for k, v in out.items() if v}
    if facts:
        out["parts"] = _parts_sha(facts)
    return out


def derived_line(cons_path):
    """T4: a generated constraints file must say which file it came from and still match it."""
    if not cons_path or not os.path.isfile(cons_path):
        return None
    try:
        text = open(cons_path, encoding="utf-8", errors="replace").read()
        import tomllib
        d = tomllib.loads(text)
    except Exception:                                           # noqa: BLE001 - an unparsable file fails in the load, not here
        return None
    gen = "GENERATED" in "\n".join(text.splitlines()[:5]).upper()
    ref = d.get("derived_from")
    if not ref:
        return RDY._line("derived_from", RDY.WARN, f"{os.path.basename(cons_path)} is a generated copy with no derived_from (legacy): the writer should add derived_from = \"<source>#<sha256>\"") if gen else None
    src, _, want = str(ref).partition("#")
    src = src if os.path.isabs(src) else os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(cons_path)), src))
    have = _sha(src)
    if have is None:
        return RDY._line("derived_from", RDY.FAIL, f"source {src} is missing", ["INPUT_STALE_COPY source missing " + src])
    if len(want) < 12 or not have.startswith(want.lower()):
        return RDY._line("derived_from", RDY.FAIL, f"{os.path.basename(cons_path)} is STALE: source {os.path.basename(src)} is now {have[:12]}, copy says {want[:12] or 'nothing'}",
                         ["INPUT_STALE_COPY " + cons_path])
    return RDY._line("derived_from", RDY.PASS, f"{os.path.basename(cons_path)} matches {os.path.basename(src)} ({have[:12]})")


def doctor_line():
    from . import deps
    res = deps.check_all()
    core = deps.blocking(res)
    miss = deps.missing(res)
    if core:
        return RDY._line("doctor", RDY.FAIL, "missing core requirement: " + ", ".join(r["label"] for r in core),
                         ["DOCTOR " + r["label"] for r in core])
    return RDY._line("doctor", RDY.PASS, f"{len(res) - len(miss)}/{len(res)} requirements present" + (f"; absent: {', '.join(r['key'] for r in miss[:6])}" if miss else ""))


def inputs_lines(board, spec, cons_path, cons, ds_dir, mm_path, with_doctor=True):
    """-> (checklist lines, manifest).  FAIL: a named input missing, no .kicad_pro beside the board, a stale generated copy, a core requirement absent."""
    man = manifest(board, spec, cons_path, cons, ds_dir, mm_path)
    gone = [f"{k}={v}" for k, v in man.items() if v and not os.path.exists(v)]
    lines = [RDY._line("inputs", RDY.FAIL if gone else RDY.PASS,
                       ("missing: " + "; ".join(gone)) if gone else "absolute: " + ", ".join(f"{k}" for k, v in man.items() if v),
                       [f"INPUT_MISSING {g}" for g in gone])]
    if board and man["project"] is None:
        lines.append(RDY._line("project file", RDY.FAIL, f"no {os.path.splitext(os.path.basename(board))[0]}.kicad_pro beside the board: KiCad would run default rules",
                               ["INPUT_NO_PROJECT " + board]))
    d = derived_line(cons_path)
    if d:
        lines.append(d)
    if with_doctor:
        lines.append(doctor_line())
    return lines, man


def stamp_path(board_or_stem):
    return os.path.splitext(board_or_stem)[0] + ".kickoff.json"


def write_stamp(path, man, hashes, lines, extra=None):
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "manifest": man, "inputs": hashes,
           "checklists": {l["name"]: l["status"] for l in lines}, **(extra or {})}
    with open(path, "w") as fh:
        json.dump(row, fh, indent=1)
    return row


def annotate_ready_json(path, man):
    try:
        d = json.load(open(path))
        d["manifest"] = man
        json.dump(d, open(path, "w"), indent=1)
    except (OSError, ValueError):
        pass


def stale(stamp, hashes):
    """-> [(kind, checklists)] for every input whose sha differs from the stamp's (added, removed or changed)."""
    old = (stamp or {}).get("inputs") or {}
    return [(k, REOPENS.get(k, ())) for k in sorted(set(old) | set(hashes)) if old.get(k) != hashes.get(k)]


def load_stamp(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def current_hashes(stamp, spec, cons_path, board, cons=None, facts=None):
    """The inputs of a stamp, re-measured from the paths the stamp recorded (a stage's board copy only supplies the parts list)."""
    man = dict(stamp.get("manifest") or {})
    man["spec"] = man.get("spec") or (os.path.abspath(spec) if spec else None)
    man["constraints"] = man.get("constraints") or (os.path.abspath(cons_path) if cons_path else None)
    if facts is None and "parts" in (stamp.get("inputs") or {}) and board and os.path.isfile(board):
        from . import review as R
        facts = R.facts_from_board(board, backend="file")
    return shas(man, facts)


def reopen(c, board, work, log=print):
    """Before a gated stage: if the stamp is stale, ask the decider, then re-run `ready`.  -> (None | the ready stage result)."""
    sp = os.path.join(work, "kickoff.json")
    stamp = load_stamp(sp)
    if not stamp:                                               # first run, or entry from a stored board: stamp the inputs as they are now
        write_first_stamp(c, board, sp)
        log(f"pipeline: kickoff stamp written from the current inputs ({sp}); no earlier ready verdict to compare")
        return None
    ch = stale(stamp, current_hashes(stamp, c.spec_path, c.cons_path, board, c.cons))
    if not ch:
        return None
    from . import decider as DC
    names = ", ".join(f"{k}({'/'.join(v)})" for k, v in ch)
    row = DC.ask("kickoff-stale", "kickoff stamp", f"inputs changed since ready passed: {names}",
                 {"reopen": "re-run the ready checklists before this stage (stops on a FAIL)",
                  "keep": "continue on the old verdicts (the stamp stays stale; recorded)"},
                 "reopen", "a changed part, spec or constraint can invalidate parts, mech, pin and placement verdicts",
                 evidence=[f"{k}: {stamp['inputs'].get(k, '-')[:12]} -> now changed" for k, _ in ch], cons=c.cons, log_dir=work)
    log(f"pipeline: kickoff stamp stale ({names}); decided {row['choice']} ({row['by']})")
    return {"changed": ch, "choice": row["choice"]}


def write_first_stamp(c, board, path):
    from . import parts as P, review as R
    ds = P.resolve_dir((c.cons.get("docs") or {}).get("datasheets"), [x for x in (c.cons_path, c.spec_path, board) if x])
    facts = R.facts_from_board(board, backend="file") if board and os.path.isfile(board) else None
    man = manifest(board, c.spec_path, c.cons_path, c.cons, ds, (facts or {}).get("mpn_map_path"))
    try:
        write_stamp(path, man, shas(man, facts), [], {"source": "pipeline entry, no ready verdict"})
    except OSError:
        pass


def stock_only(ready_json):
    """True when every FAIL line of a ready report fails only because stock/distributor data was not checked (offline): a WARN for the
    stale re-check, never for `ready` itself."""
    try:
        fails = [l for l in json.load(open(ready_json))["checklist"] if l["status"] == RDY.FAIL]
    except (OSError, ValueError, KeyError):
        return False
    return bool(fails) and all(l["blockers"] and all("STOCK_UNCHECKED" in b or "NOT CHECKED" in b for b in l["blockers"]) for l in fails)
