"""KiCad is the authority for routed net length (the number its own skew DRC reports: all copper of the net, vias by stackup height,
in-pad copper included).  `pair_lengths` writes one `(constraint skew)` rule per pair into a temp .kicad_dru next to a temp copy of the
board, runs `kicad-cli pcb drc` and parses `target net length X (from NET); actual Y`.  Nothing here knows a board."""
import os
import re
import subprocess
import sys

_RL = re.compile(r"rule 'ln(\d+)'.*?actual ([\d.]+) mm")
_RE = re.compile(r"rule '(sk\d+)'.*?actual (-?[\d.]+) mm; target net length ([\d.]+) mm \(from ([^)]+)\); actual ([\d.]+) mm")
CACHE = {}


def _q(net):
    return net.replace("\\", "\\\\").replace("'", "\\'")


def pair_lengths(board, pairs, kicad_cli="kicad-cli", timeout=300):
    """{(p, n): {"p": mm | None, "n": mm | None, "skew": P - N signed mm}} for the pairs of `board` (a path), from KiCad's DRC.
    A pair KiCad reports no skew for (equal within 0.0001 mm) has skew 0.0 and lengths None.  None when the CLI fails (the caller
    falls back to its own measure and warns)."""
    pairs = [tuple(p) for p in pairs]
    if not pairs or not board or not os.path.isfile(board):
        return {} if not pairs else None
    st = os.stat(board)
    key = (os.path.abspath(board), st.st_mtime_ns, st.st_size, tuple(pairs))
    if key in CACHE:
        return CACHE[key]
    try:
        nets = sorted({x for pr in pairs for x in pr})
        rules = "(version 1)\n" + "".join(
            f"(rule \"sk{i}\" (condition \"A.NetName == '{_q(p)}' || A.NetName == '{_q(n)}'\") (constraint skew (max 0.0001mm)))\n"
            for i, (p, n) in enumerate(pairs)) + "".join(
            f"(rule \"ln{i}\" (condition \"A.NetName == '{_q(x)}'\") (constraint length (max 0.0001mm)))\n" for i, x in enumerate(nets))
        from . import drcguard as DG
        try:
            rep = DG.drc_json(board, kicad_cli, timeout, rules=rules, only_rules=["skew_out_of_range", "length_out_of_range"], complete=False)
        except RuntimeError as e:
            print(f"netlen: kicad-cli drc failed: {e}", file=sys.stderr)
            return None
        res = {pr: {"p": None, "n": None, "skew": 0.0} for pr in pairs}
        tot = {}
        for v in rep.get("violations", []):
            ml = _RL.search(v.get("description", ""))
            if ml:                                           # the net's total copper length (reported for every net, equal or not)
                tot[nets[int(ml.group(1))]] = max(tot.get(nets[int(ml.group(1))], 0.0), float(ml.group(2)))
        for (p, n) in pairs:
            if p in tot and n in tot:
                res[(p, n)].update(p=tot[p], n=tot[n], skew=round(tot[p] - tot[n], 4))
        for v in rep.get("violations", []):
            m = _RE.search(v.get("description", ""))
            if not m:
                continue
            p, n = pairs[int(m.group(1)[2:])]
            tgt, frm, oth = float(m.group(3)), m.group(4), float(m.group(5))
            lp, ln = (tgt, oth) if frm == p else (oth, tgt)
            res[(p, n)] = {"p": lp, "n": ln, "skew": round(lp - ln, 4)}
        CACHE[key] = res
        return res
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"netlen: kicad-cli drc unavailable: {e}", file=sys.stderr)
        return None



# ---------------------------------------------------------------------------------------------- D71cx: delay skew
RECON_TOL_MM = 0.05            # D71cx: own per-leg decomposition must equal KiCad's DRC total length within this


def _rows(cons, board_file):
    """(stackup rows, planes): the constraints' template with the board's epsilon_r, else the board's own stackup."""
    from . import channel as CH
    rows, planes, _src = CH.stack_rows(cons or {}, board_file)
    if not rows:
        from . import review as RV
        rows = [dict(r, roughness_rms_um=CH.DEFAULT_ROUGHNESS_UM) for r in RV.parse_stackup_text(open(board_file, errors="replace").read())]
        planes = []
    return rows, planes


def leg_decomposition(board, net, rows, planes, tol=0.005):
    """All copper of `net` (what KiCad's length rule sums): {layer_mm: {layer: raw mm incl. copper inside pads}, via_mm, total_mm,
    delay_ps, ps_per_mm: {layer: ps/mm}, unknown: [layers without a reference plane]}.  `board` is a pcbnew BOARD.
    A via counts its barrel between the outermost and innermost layer the net's copper uses at it (stackup thickness top copper to bottom copper, lengthmatch.via_mm, less half the top copper: matches KiCad's length within 0.01 mm per via on W15);
    the barrel's delay is the mean delay per mm of the net's copper (channel.path_metrics rule)."""
    import math
    import pcbnew
    from . import channel as CH, lengthmatch as LM
    cu = [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]
    lmm, lw, ends, vias = {}, {}, [], []
    for t in board.GetTracks():
        if str(t.GetNetname()) != net:
            continue
        if t.GetClass() == "PCB_VIA":
            vias.append(t)
        elif t.GetClass() in ("PCB_TRACK", "PCB_ARC"):
            lyr = board.GetLayerName(t.GetLayer())
            mm = t.GetLength() / 1e6
            lmm[lyr] = lmm.get(lyr, 0.0) + mm
            lw[lyr] = lw.get(lyr, 0.0) + mm * t.GetWidth() / 1e6
            for pt in (t.GetStart(), t.GetEnd()):
                ends.append((lyr, pcbnew.ToMM(pt.x), pcbnew.ToMM(pt.y)))
    via_mm = 0.0
    for v in vias:
        pos = v.GetPosition()
        vx, vy, r = pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), v.GetWidth() / 2e6
        used = {cu.index(l) for l, x, y in ends if l in cu and math.hypot(x - vx, y - vy) <= r + tol}
        if len(used) >= 2:
            via_mm += max(0.0, (LM.via_mm(rows, cu[min(used)], cu[max(used)]) or 0.0) - CH.copper_t_mm(rows, cu[min(used)]) / 2)   # calibrated on W15 vs KiCad (+-0.01 mm/via)
    psmm, unknown, delay, tot = {}, [], 0.0, sum(lmm.values())
    for lyr, mm in lmm.items():
        w = lw[lyr] / mm if mm else None
        d = CH.delay_ps_mm(rows, planes, lyr, w)
        if d is None:
            unknown.append(lyr)
            continue
        psmm[lyr] = d
        delay += mm * d
    if tot > 0 and via_mm and not unknown:
        delay += via_mm * delay / tot
    return {"layer_mm": {k: round(v, 4) for k, v in lmm.items()}, "via_mm": round(via_mm, 4), "total_mm": round(tot + via_mm, 4),
            "delay_ps": None if unknown else round(delay, 3), "ps_per_mm": psmm, "unknown": unknown}


def joined(B, p, n):
    """True when the pair is multi-terminal by topology: some part carries more than two pads of the pair's two nets (the two rows of a
    connector), so a signal path runs between terminals and a branch hangs off the trunk.  No ref-prefix rule."""
    refs = {}
    for q in (B or {}).get("pads") or []:
        if q["net"] in (p, n) and not q.get("np"):
            refs.setdefault(q["ref"], set()).add((q["net"], q["num"]))
    return any(len(v) > 2 for v in refs.values())


def pair_skew(B, p, n, cons=None, lengths=None, kicad_cli="kicad-cli", grade_ps=False):
    """THE skew of one pair (REFACTOR-PLAN-W16 1.1, D71cx).  -> dict
      status   "ok" | "unmeasured" (kicad-cli failed or an open leg: cannot pass) | "MEASURE_DISAGREE" (a leg's own decomposition misses
               KiCad's net total by more than RECON_TOL_MM: ungraded, FAIL in review)
      basis    "kicad" (2-terminal: KiCad's DRC lengths, no fallback) | "path" (multi-terminal, found by topology: per-orientation
               channel.measure_paths, one row per orientation)
      skew_mm  signed worst P - N in mm (kicad: whole-net lengths; path: worst orientation, pad edge to pad edge, via barrels in)
      p_mm n_mm  KiCad's net totals; skew_ps signed worst P - N delay (None: a layer without a reference plane), p_ps n_ps
      recon    {p, n: own total - KiCad total in mm}; paths [{a, b, p, n, d, skew, p_ps, n_ps, d_ps}]; stub_mm longest spur
    Delay of a leg = sum over layers of (segment length on the layer x stackup ps/mm) + via barrels.
    The reconciliation gates the verdict where own numbers enter it: always on the path basis, on the kicad basis only when grade_ps
    (a ps budget grades the own delay).  A 2-terminal pair graded in mm uses KiCad's number alone; `recon` is then advice."""
    out = {"status": "unmeasured", "basis": None, "skew_mm": None, "skew_ps": None, "p_mm": None, "n_mm": None, "p_ps": None,
           "n_ps": None, "recon": {}, "paths": [], "stub_mm": 0.0, "why": ""}
    f = (B or {}).get("file") if isinstance(B, dict) else None
    if not f or not os.path.isfile(f):
        out["why"] = "no board file"
        return out
    if lengths is not None:                                  # a batch result from the caller: trusted, never re-run per pair
        kl = lengths.get((p, n))
    else:
        kl = (pair_lengths(f, [(p, n)], kicad_cli) or {}).get((p, n))
    if not kl or kl.get("p") is None or kl.get("n") is None:
        out["why"] = "kicad-cli failed: pair not measured (no own-math fallback, D71cx)"
        return out
    out.update(p_mm=kl["p"], n_mm=kl["n"])
    try:
        from . import channel as CH, kicad_io as IO
        rows, planes = _rows(cons, f)
        board = IO.load(f)
        dp, dn = leg_decomposition(board, p, rows, planes), leg_decomposition(board, n, rows, planes)
    except Exception as e:                                  # noqa: BLE001 - no pcbnew / odd board: unmeasured, never a guess
        out["why"] = f"decomposition failed: {type(e).__name__}: {e}"
        return out
    out["recon"] = {"p": round(dp["total_mm"] - kl["p"], 4), "n": round(dn["total_mm"] - kl["n"], 4), "tol": RECON_TOL_MM}
    out.update(p_ps=dp["delay_ps"], n_ps=dn["delay_ps"])
    if joined(B, p, n):
        meas = CH.measure_paths(board, [p, n], rows, mode="all")
        out["basis"] = "path"
        from . import pairs as PRS
        mate = {}
        for t in PRS.terminals(B, p, n, 16.0)[0]:
            a, b = f"{t['p']['ref']}.{t['p']['num']}", f"{t['n']['ref']}.{t['n']['num']}"
            mate[a], mate[b] = b, a
        byend = {frozenset((q["a"], q["b"])): q for q in meas[n] if q.get("found")}
        ors = [(q, byend.get(frozenset((mate.get(q["a"]), mate.get(q["b"]))))) for q in meas[p]
               if q.get("found") and q["a"].split(".")[0] != q["b"].split(".")[0]]
        if not ors or any(b is None for _a, b in ors):
            out["why"] = "an orientation has no matching N path"
            return out
        L = lambda q: q["length_mm"] + q["via_mm"]            # noqa: E731
        for a, b in ors:
            ma, mb = CH.path_metrics(a, rows, planes), CH.path_metrics(b, rows, planes)
            d_ps = None if ma["delay_ps"] is None or mb["delay_ps"] is None else round(ma["delay_ps"] - mb["delay_ps"], 3)
            out["paths"].append({"a": a["a"], "b": a["b"], "p": round(L(a), 3), "n": round(L(b), 3), "d": round(L(a) - L(b), 3),
                                 "skew": round(abs(L(a) - L(b)), 3), "p_ps": ma["delay_ps"], "n_ps": mb["delay_ps"], "d_ps": d_ps,
                                 "stub": max(a["stub_max_mm"], b["stub_max_mm"])})
        w = max(out["paths"], key=lambda r: r["skew"])
        out["skew_mm"] = w["d"]
        dps = [r["d_ps"] for r in out["paths"]]
        out["skew_ps"] = None if None in dps else max(dps, key=abs)
        out["stub_mm"] = max(r["stub"] for r in out["paths"])
    else:
        out["basis"] = "kicad"
        out["skew_mm"] = round(kl["p"] - kl["n"], 4)
        out["skew_ps"] = None if dp["delay_ps"] is None or dn["delay_ps"] is None else round(dp["delay_ps"] - dn["delay_ps"], 3)
        out["paths"] = [{"a": "", "b": "", "p": kl["p"], "n": kl["n"], "d": out["skew_mm"], "skew": abs(out["skew_mm"]),
                         "p_ps": dp["delay_ps"], "n_ps": dn["delay_ps"], "d_ps": out["skew_ps"], "stub": 0.0}]
    bad = [k for k in ("p", "n") if abs(out["recon"][k]) > RECON_TOL_MM] if (out["basis"] == "path" or grade_ps) else []
    out["status"] = "MEASURE_DISAGREE" if bad else "ok"
    if bad:
        out["why"] = "own per-leg decomposition vs KiCad total: " + ", ".join(f"{k} {out['recon'][k]:+.3f} mm" for k in bad)
    return out
