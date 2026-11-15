"""`fluxplace relieve`: placement-side congestion relief driven by the plan-route predictor (docs/commands/relieve.md).

Nothing here is a new placer.  The move engine is `planplace.plan_place` (PlanPlacer: moves proposed from the predictor's findings
- sealed-pin blockers, overflow neighbours, over-budget ends - each legalised by `legality.Legal`, the support-part guard, and kept
only when the predictor's lexicographic key improves).  This module adds:

  * the hard gate that the move engine lacks: a board is kept only when the collision gate (`legality.collision_gate`) and the
    intent hard rows (`intent.check`) are no worse than the input's, verified on the WRITTEN board (reload + full predictor);
  * never-worse selection among input / moved / grown boards by `planroute.rank_key`;
  * growth, only when `[relieve] may_grow_mm > 0` and `may_grow_sides` allow it: the side nearest the residual congestion is
    chosen, the smallest passing size is found by bisection (the predicate is assumed monotone in size);
  * the options table (prediction only): per-side growth probes, layer variants (`planroute.run_variants`), and sealed pins whose
    blockers are fixed or locked (owner decisions);
  * a connectivity guard: same refs, same pad->net assignment, same segments and vias.

Determinism: no clock limits (`max_evals` bounds the move engine), sorted iteration, no unordered set output.
"""
import json
import math
import os
import shutil

from . import boardwrite as BW
from . import intent as IT
from . import legality as LGL
from . import planopts as PO
from . import planplace as PP
from . import planroute as PL
from . import routable as RB
from . import routediag as RD

EDGES = ("L", "R", "T", "B")
DEFAULTS = {"enable": False, "rounds": 20, "may_grow_mm": 0.0, "may_grow_sides": [], "max_evals": 400}


def settings(cons, rounds=None, may_grow=None, sides=None):
    """`[relieve]` of the constraints, overridden by the CLI flags when given."""
    r = dict(DEFAULTS)
    r.update(((cons or {}).get("relieve")) or {})
    if rounds is not None:
        r["rounds"] = int(rounds)
    if may_grow is not None:
        r["may_grow_mm"] = float(may_grow)
    if sides is not None:
        r["may_grow_sides"] = list(sides)
    r["may_grow_sides"] = [s for s in EDGES if s in set(r["may_grow_sides"])]
    r["may_grow_mm"] = float(r["may_grow_mm"])
    return r


def hard_state(B, cons, spec):
    """What a relieve move must not worsen: collision FAILs and failing hard intent rows (names, sorted)."""
    _ok, cf = LGL.collision_gate(B, cons)
    rows = IT.check(B, cons, spec) if IT.enabled(cons) else []
    return {"collisions_fail": sum(1 for x in cf if x["level"] == "FAIL"),
            "intent_hard_failed": sorted({f"{r['kind']}:{r['name']}" for r in rows if r["hard"] and not r["met"]})}


def hard_ok(h, ref):
    return h["collisions_fail"] <= ref["collisions_fail"] and set(h["intent_hard_failed"]) <= set(ref["intent_hard_failed"])


def guard(path_in, path_out):
    """Connectivity guard: same refs, same pad->net map per ref, same segments and vias.  -> (ok, [problems])."""
    A, Bo = RD.load(path_in), RD.load(path_out)
    probs = []
    if set(A["parts"]) != set(Bo["parts"]):
        probs.append(f"ref set differs by {len(set(A['parts']) ^ set(Bo['parts']))}")
    for r in sorted(set(A["parts"]) & set(Bo["parts"])):
        pa = sorted((str(q.get("num")), str(q.get("net"))) for q in A["parts"][r]["pads"])
        pb = sorted((str(q.get("num")), str(q.get("net"))) for q in Bo["parts"][r]["pads"])
        if pa != pb:
            probs.append(f"{r}: pad/net map changed")
    if len(A["segs"]) != len(Bo["segs"]) or len(A["vias"]) != len(Bo["vias"]):
        probs.append("segment or via count changed")
    return not probs, probs


def _read(p):
    with open(p, errors="surrogateescape") as fh:
        return fh.read()


def _write(p, text):
    with open(p, "w", errors="surrogateescape") as fh:
        fh.write(text)


class Pass:
    """One move pass on a board file: result text, its predictor run, hard state, ledger."""

    def __init__(self, text, path, B, R, H, moves, log, skipped_flips, accepted, key):
        self.text, self.path, self.B, self.R, self.H = text, path, B, R, H
        self.moves, self.log, self.skipped_flips, self.accepted, self.key = moves, log, skipped_flips, accepted, key


def _pass(path_in, work, name, cons, spec, rc, ref_hard, log):
    """Move pass on `path_in`.  Keeps the moved board only when it is strictly better on the predictor key AND no hard check got worse
    (both verified on the written file).  Otherwise the result is the input text."""
    text0 = _read(path_in)
    B0 = RD.load(path_in)
    R0 = PL.run(B0, cons, spec, ignore_routing=True)
    key0 = PL.rank_key(R0)
    B = RD.load(path_in)
    mv, st = PP.plan_place(B, cons, spec, log=log, max_evals=rc["max_evals"], rotation=True, max_rounds=rc["rounds"])
    flips = set(st.get("flips", []))
    moves = {r: v for r, v in mv.items() if r not in flips}
    text1 = BW.apply_moves(text0, moves)[0] if moves else text0
    path1 = os.path.join(work, name + ".kicad_pcb")
    _write(path1, text1)
    B1 = RD.load(path1)
    R1 = PL.run(B1, cons, spec, ignore_routing=True)
    H1 = hard_state(B1, cons, spec)
    key1 = PL.rank_key(R1)
    ok_hard = hard_ok(H1, ref_hard)
    accepted = bool(moves) and ok_hard and key1 < key0
    if not accepted:
        return Pass(text0, path_in, B0, R0, ref_hard, [], log, sorted(flips), False, key0)
    return Pass(text1, path1, B1, R1, H1, st.get("log", []), log, sorted(flips), True, key1)


def _edge_dist(cand, side):
    """Distance from the residual congestion's centroid to the outline edge `side` (mm)."""
    R = cand["R"]
    xs = [f["at"] for f in R["findings"] if f["code"] in ("PLAN_PIN_SEALED", "PLAN_OVERFLOW") and f.get("at")]
    if not xs:
        return 0.0
    cx = sum(p[0] for p in xs) / len(xs)
    cy = sum(p[1] for p in xs) / len(xs)
    x0, y0, x1, y1 = RD.load(cand["path"])["bbox"]
    return {"L": cx - x0, "R": x1 - cx, "T": cy - y0, "B": y1 - cy}[side]


def _grow(text, path, cons, side, mm, work, name):
    B = RD.load(path)
    g = tuple(mm if e == side else 0.0 for e in EDGES)
    text2 = PO.grow_text(text, B, g, cons)
    p = os.path.join(work, name + ".kicad_pcb")
    _write(p, text2)
    return p


def _moved_rows(moves):
    rows = []
    for e in moves:
        for r in e["refs"]:
            rows.append({"ref": r, "kind": e["kind"], "reason": e["reason"], "from": e["from"][r], "to": e["to"][r],
                         "score_before": e["before"], "score_after": e["after"]})
    return rows


def _owner_rows(R, B, cons):
    """Sealed pins: blockers that are fixed or locked need an owner decision; movable blockers with no accepted move are reported."""
    fixed = set(RB._fixed_set(B, cons))
    out = []
    for f in PL.rank(R["findings"]):
        if f["code"] != "PLAN_PIN_SEALED":
            continue
        blk = f.get("blocker")
        if blk and (blk in fixed or B["parts"].get(blk, {}).get("locked")):
            out.append({"kind": "owner", "text": f"pin {f['part']}.{f.get('pin')} ({f.get('net')}) sealed by fixed/locked {blk}: "
                                                 f"owner decision (free {blk} or re-place {f['part']})"})
        else:
            out.append({"kind": "open", "text": f"pin {f['part']}.{f.get('pin')} ({f.get('net')}) still sealed; blocker "
                                                f"{blk or 'self'} movable, no accepted move"})
    return out


def _cand(kind, P, moves):
    return {"kind": kind, "text": P.text, "path": P.path, "R": P.R, "H": P.H, "moves": moves}


def run(board, cons, spec, out, rounds=None, may_grow=None, sides=None, log=print, json_path=None):
    """-> summary dict.  Writes OUT (the best board seen), OUT + '.relieve.json' (ledger, verdict, options) and returns the text table."""
    if os.path.abspath(out) == os.path.abspath(board):
        raise SystemExit("relieve: --out must differ from the input board")
    rc = settings(cons, rounds, may_grow, sides)
    work = out + ".relieve-work"
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "input.kicad_pcb")
    shutil.copyfile(board, src)
    B_in = RD.load(src)
    R_in = PL.run(B_in, cons, spec, ignore_routing=True)
    H_in = hard_state(B_in, cons, spec)
    before = PL.verdict(R_in)
    cur = _cand("input", type("P", (), {"text": _read(src), "path": src, "R": R_in, "H": H_in})(), [])

    # phase 1: moves on the input (kept only if strictly better on the predictor key and no hard check worse)
    p1 = _pass(src, work, "pass1", cons, spec, rc, H_in, log)
    if p1.accepted:
        cur = _cand("moved", p1, p1.moves)
    fail_after = PL.predicate(cur["R"])[0]

    # growth: only when [relieve] allows it and the moves alone do not pass.  Sides are tried nearest-congestion first; each side is
    # probed at the largest allowed size, and only a side that passes there is bisected for the smallest passing size.
    cur0 = cur
    growth = {"probes": [], "chosen": None, "table": []}
    cache = {}
    probe_mm = max(10, int(math.ceil(rc["may_grow_mm"])))

    def grow_probe(sd, mm, phase):
        if (sd, mm) not in cache:
            gp = _grow(cur0["text"], cur0["path"], cons, sd, float(mm), work, f"grow_{sd}_{mm}")
            P = _pass(gp, work, f"grow_{sd}_{mm}_moved", cons, spec, rc, H_in, log)
            rec = {"side": sd, "mm": mm, "phase": phase, "passes": not PL.predicate(P.R)[0], "hard_ok": hard_ok(P.H, H_in),
                   "overflow": P.R["comp"]["overflow"], "sealed": P.R["comp"]["sealed"], "score": P.R["score"]}
            growth["probes"].append(rec)
            cache[(sd, mm)] = (rec, P)
        return cache[(sd, mm)]

    if PL.predicate(cur0["R"])[0]:
        top = int(math.ceil(rc["may_grow_mm"]))
        order = sorted(rc["may_grow_sides"], key=lambda sd: (_edge_dist(cur0, sd), EDGES.index(sd))) if rc["may_grow_mm"] > 0 else []
        hit = None
        for sd in order:
            rec, P = grow_probe(sd, top, "max")
            if rec["passes"] and rec["hard_ok"]:
                lo, hi = 0, top                          # smallest passing size, assuming the predicate is monotone in size
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    r2, _ = grow_probe(sd, mid, "bisect")
                    if r2["passes"] and r2["hard_ok"]:
                        hi = mid
                    else:
                        lo = mid
                hit = grow_probe(sd, hi, "smallest")
                break
        if hit is None and order:                       # no side passes: the best improving largest probe, if any
            cands = [grow_probe(sd, top, "max") for sd in order]
            ok = [(PL.rank_key(P.R), k, (rec, P)) for k, (rec, P) in enumerate(cands) if rec["hard_ok"] and PL.rank_key(P.R) < PL.rank_key(cur0["R"])]
            if ok:
                hit = min(ok, key=lambda t: (t[0], t[1]))[2]
        if hit is not None:
            rec, P = hit
            growth["chosen"] = rec["side"]
            cur = _cand("grown", P, cur0["moves"] + (P.moves if P.accepted else []))
            cur["grow"] = {"side": rec["side"], "mm": rec["mm"]}
            cur["text"], cur["path"] = P.text, P.path
        # options table (prediction only, owner decisions): every side at probe_mm (cached when it is the probe size), layer variants
        for sd in EDGES:
            rec, _P = grow_probe(sd, probe_mm, "table")
            growth["table"].append({"option": f"grow +{probe_mm} mm on side {sd}", "fails": not rec["passes"],
                                    "sealed": rec["sealed"], "overflow": rec["overflow"], "score": rec["score"]})
        for v in PL.run_variants(RD.load(cur0["path"]), cons, spec, True, base=cur0["R"]) or []:
            growth["table"].append({"option": f"layers: {v['variant']}", "fails": bool(v["sealed"] or v["overflow"] > 0),
                                    "sealed": v["sealed"], "overflow": v["overflow"], "score": v["score"]})

    ok_g, probs = guard(board, cur["path"])
    B_out = RD.load(cur["path"])
    rows = _moved_rows(cur["moves"])
    maxd = max([math.hypot(r["to"][0] - r["from"][0], r["to"][1] - r["from"][1]) for r in rows] or [0.0])
    moved_refs = sorted({r["ref"] for r in rows})
    v_out = PL.verdict(cur["R"])
    summary = {"input": {"score": before["score"], "result": before["result"], "components": before["components"],
                         "reasons": PL.predicate(R_in)[1]},
               "output": {"score": v_out["score"], "result": v_out["result"], "components": v_out["components"],
                          "reasons": PL.predicate(cur["R"])[1]},
               "kind": cur["kind"], "grow": cur.get("grow"),
               "verdict": ("no candidate improved the predictor key: OUT is a copy of the input" if cur["kind"] == "input"
                           else f"best board is '{cur['kind']}'"),
               "parts_moved": len(moved_refs), "max_move_mm": round(maxd, 2), "moves": rows,
               "growth": {"chosen": growth["chosen"], "allowed_mm": rc["may_grow_mm"], "sides": rc["may_grow_sides"],
                          "probes": growth["probes"], "table": growth["table"]},
               "owner_rows": _owner_rows(cur["R"], B_out, cons), "hard_in": H_in, "hard_out": cur["H"],
               "guard": {"ok": ok_g, "problems": probs}, "settings": rc, "fails_output": PL.predicate(cur["R"])[0]}
    _write(out, cur["text"])
    with open(out + ".relieve.json", "w") as fh:
        json.dump(summary, fh, indent=1, default=str)
    if json_path:
        with open(json_path, "w") as fh:
            json.dump(summary, fh, indent=1, default=str)
    summary["text"] = format_text(summary, board, out)
    log(summary["text"])
    return summary


def format_text(S, board, out):
    i, o = S["input"], S["output"]
    L = [f"relieve: {'FAIL' if S['fails_output'] else 'ok'} - PLAN_SCORE {i['score']:g} -> {o['score']:g}; sealed {i['components']['sealed']} -> "
         f"{o['components']['sealed']}, overflow {i['components']['overflow']:g} -> {o['components']['overflow']:g}; {S['parts_moved']} part(s) moved, "
         f"max {S['max_move_mm']:g} mm; {S['verdict']}",
         f"  guard: {'ok' if S['guard']['ok'] else 'FAILED ' + '; '.join(S['guard']['problems'])}; collisions FAIL "
         f"{S['hard_in']['collisions_fail']} -> {S['hard_out']['collisions_fail']}; intent hard fails "
         f"{len(S['hard_in']['intent_hard_failed'])} -> {len(S['hard_out']['intent_hard_failed'])}",
         f"  output: {out}; ledger {out}.relieve.json"]
    if S["growth"]["probes"] or S["growth"]["table"] or S["fails_output"]:
        L.append("options (prediction only, owner decisions):")
        for t in S["growth"]["table"]:
            L.append(f"  {t['option']:32s} -> {'FAIL' if t['fails'] else 'ok  '} sealed {t['sealed']}, overflow {t['overflow']:g}, score {t['score']:g}")
        for p in S["growth"]["probes"]:
            L.append(f"  probe grow +{p['mm']} mm on {p['side']} ({p['phase']}) -> {'ok' if p['passes'] else 'FAIL'} overflow {p['overflow']:g} sealed {p['sealed']}")
        for r in S["owner_rows"]:
            L.append(f"  {r['text']}")
    return "\n".join(L)
