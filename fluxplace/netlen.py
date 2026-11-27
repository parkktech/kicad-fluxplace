"""KiCad is the authority for routed net length (the number its own length / skew DRC reports: all copper of the net, vias by stackup
height; a track inside a pad is clipped to the shortest path through the pad).  `pair_lengths` writes one `(constraint skew)` rule per pair into a temp .kicad_dru next to a temp copy of the
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
    Lengths come from one `length` rule per net (every net with copper reports, equal or not).  None when kicad-cli fails or the rule file
    is ignored (the fxcanary rule did not fire): the pair is unmeasured and cannot pass, there is no fallback.  skew = P - N for the pair as
    given (p, n)."""
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
            rep = DG.drc_json(board, kicad_cli, timeout, rules=rules, only_rules=["skew_out_of_range", "length_out_of_range"], complete=False, refill=False)
        except RuntimeError as e:
            print(f"netlen: kicad-cli drc failed: {e}", file=sys.stderr)
            return None
        res = {pr: {"p": None, "n": None, "skew": 0.0} for pr in pairs}
        tot = {}
        for v in rep.get("violations", []):
            ml = _RL.search(v.get("description", ""))
            if ml:                                           # the net's total copper length (reported for every net, equal or not)
                tot[nets[int(ml.group(1))]] = max(tot.get(nets[int(ml.group(1))], 0.0), float(ml.group(2)))
        for (p, n) in pairs:                                 # the rule reports every net with copper: a missing net has none (length 0)
            lp, ln = tot.get(p, 0.0), tot.get(n, 0.0)
            res[(p, n)].update(p=lp, n=ln, skew=round(lp - ln, 4))
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
RECON_JOINT_MM = 0.01          # + this per via and per pad past the second of the worse leg (a 3-section chain with vias sums more per-piece rounding)
RECON_TOL_MM = 0.05            # D71cx: own per-leg decomposition must equal KiCad's DRC total length within this


def _rows(cons, board_file):
    """(stackup rows, planes): the constraints' template with the board's epsilon_r, else the board's own stackup."""
    from . import channel as CH
    rows, planes, _src = CH.stack_rows(cons or {}, board_file)
    if not rows:
        from . import review as RV
        rows = [dict(r, roughness_rms_um=CH.DEFAULT_ROUGHNESS_UM) for r in RV.parse_stackup_text(open(board_file, errors="replace").read())]
        planes = []
    if not rows:
        rows = _default_rows(board_file)
    return rows, planes


def _default_rows(board_file, copper_mm=0.035):
    """A board with no stackup: KiCad spreads the board thickness evenly between the copper layers (its via height on such a board is
    thickness less half a copper layer, which is what leg_decomposition counts).  Copper rows carry names; no planes, so no delay."""
    try:
        import pcbnew
        from . import kicad_io as IO
        b = IO.load(board_file)
        cu = [b.GetLayerName(i) for i in b.GetEnabledLayers().CuStack()]
        t = b.GetDesignSettings().GetBoardThickness() / 1e6
    except Exception:                                      # noqa: BLE001 - no pcbnew: no stackup, no via barrels
        return []
    if len(cu) < 2:
        return []
    die = max(0.0, (t - copper_mm * len(cu)) / (len(cu) - 1))
    rows = []
    for k, name in enumerate(cu):
        rows.append({"type": "copper", "name": name, "thickness": copper_mm})
        if k < len(cu) - 1:
            rows.append({"type": "core", "thickness": die, "epsilon_r": 4.5})
    return rows


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
    npads = sum(1 for p in board.GetPads() if str(p.GetNetname()) == net)
    return {"n_vias": len(vias), "n_pads": npads, "layer_mm": {k: round(v, 4) for k, v in lmm.items()}, "via_mm": round(via_mm, 4), "total_mm": round(tot + via_mm, 4),
            "delay_ps": None if unknown else round(delay, 3), "ps_per_mm": psmm, "unknown": unknown}


def _pad_max(cons, master, default=16.0):
    """Longest P-to-N pad distance that still mates two pads into a terminal: `[pairs.F] terminal_max_mm` (a wide connector), else 16 mm."""
    from . import budget as BG
    fam = BG.family_of((cons or {}).get("pairs"), master)
    return float(((cons or {}).get("pairs", {}).get(fam) or {}).get("terminal_max_mm", default)) if fam else default


def joined(B, p, n, cons=None):
    """True when the pair is multi-terminal by topology: some part carries more than two pads of the pair's two nets (the two rows of a
    connector) or the pair has three or more P/N terminals (a tap), so a signal path runs between end terminals and a branch hangs off the
    trunk.  No ref-prefix rule."""
    refs = {}
    for q in (B or {}).get("pads") or []:
        if q["net"] in (p, n) and not q.get("np"):
            refs.setdefault(q["ref"], set()).add((q["net"], q["num"]))
    if any(len(v) > 2 for v in refs.values()):
        return True
    try:                                                    # a tap (protection part, test point) makes a third terminal on a 2-pad-per-part pair
        from . import pairs as PRS
        return len(PRS.terminals(B, p, n, _pad_max(cons, p))[0]) >= 3
    except Exception:                                       # noqa: BLE001 - no terminals: not joined
        return False


def orient(B, p, n, meas, cons=None):
    """Signal paths of a multi-terminal pair, independent of net order.  `meas` = channel.measure_paths(.., mode="all") for p and n.
    Terminals are the P/N pad pairs of pairs.terminals (same part first, then nearest); a part with several terminals is one part (the two
    rows of a connector).  The END parts are the two parts farthest apart (P + N path length between their nearest terminals);
    their terminals are the endpoints, and a path runs between an end-part terminal of one and one of the other, P leg against its
    own N mate.  Every other part on the pair (protection, test point, a series part's tap) is a TAP: it is never an endpoint, and
    the copper from it to the trunk is a stub.  No ref prefix, no part kind: topology and geometry only.
    -> {"ors": [(P path, N path, "a>b")], "taps": [{"at", "mm"}], "spurs": [{"at", "mm"}], "ends": (refX, refY)} or None."""
    from . import pairs as PRS
    terms = PRS.terminals(B, p, n, _pad_max(cons, p))[0]
    lab = lambda pad: f"{pad['ref']}.{pad['num']}"                                    # noqa: E731
    if len(terms) < 2:
        return None
    L = lambda q: q["length_mm"] + q["via_mm"]                                         # noqa: E731
    dist = {leg: {frozenset((q["a"], q["b"])): q for q in meas[leg] if q.get("found")} for leg in (p, n)}
    byref = {}
    for t in terms:
        byref.setdefault(t["p"]["ref"], []).append(t)

    def d2(ta, tb):                                                                    # P + N path length between two terminals
        a, b = dist[p].get(frozenset((lab(ta["p"]), lab(tb["p"])))), dist[n].get(frozenset((lab(ta["n"]), lab(tb["n"]))))
        return None if a is None or b is None else L(a) + L(b)

    best = None
    for x, y in ((x, y) for i, x in enumerate(sorted(byref)) for y in sorted(byref)[i + 1:]):
        ds = [d for ta in byref[x] for tb in byref[y] for d in [d2(ta, tb)] if d is not None]
        if ds and (best is None or min(ds) > best[0] + 1e-9):
            best = (min(ds), x, y)
    if best is None:
        return None
    _d, X, Y = best
    ors = []
    for ta in sorted(byref[X], key=lambda t: lab(t["p"])):
        for tb in sorted(byref[Y], key=lambda t: lab(t["p"])):
            qp = dist[p].get(frozenset((lab(ta["p"]), lab(tb["p"]))))
            qn = dist[n].get(frozenset((lab(ta["n"]), lab(tb["n"]))))
            if qp is None or qn is None:
                return None
            # a path is labelled with the P pad order it was measured in
            ors.append((qp, qn, f"{qp['a']}>{qp['b']}"))
    ends = byref[X] + byref[Y]
    half = lambda leg, a, j, c: (dist[leg][frozenset((a, j))]["length_mm"] + dist[leg][frozenset((a, c))]["length_mm"]
                                 - dist[leg][frozenset((j, c))]["length_mm"]) / 2          # noqa: E731
    taps, spurs = [], []
    for t in terms:
        if t["p"]["ref"] in (X, Y):
            continue
        v = []
        for leg, key in ((p, "p"), (n, "n")):
            c = [half(leg, lab(t[key]), lab(a[key]), lab(b[key])) for a in byref[X] for b in byref[Y]
                 if all(frozenset(z) in dist[leg] for z in ((lab(t[key]), lab(a[key])), (lab(t[key]), lab(b[key])), (lab(a[key]), lab(b[key]))))]
            if c:
                v.append(min(c))
        taps.append({"at": f"{lab(t['p'])}/{t['n']['num']}", "mm": round(max([0.0] + v), 3)})
    for part, grp in ((X, byref[X]), (Y, byref[Y])):
        if len(grp) < 2:
            continue
        other = byref[Y if part == X else X]
        for t in grp:
            v = []
            for leg, key in ((p, "p"), (n, "n")):
                c = [half(leg, lab(t[key]), lab(j[key]), lab(o[key])) for j in grp if j is not t for o in other
                     if all(frozenset(z) in dist[leg] for z in ((lab(t[key]), lab(j[key])), (lab(t[key]), lab(o[key])), (lab(j[key]), lab(o[key]))))]
                if c:
                    v.append(min(c))
            spurs.append({"at": f"{lab(t['p'])}/{t['n']['num']}", "mm": round(max([0.0] + v), 3)})
    return {"ors": ors, "taps": taps, "spurs": spurs, "ends": (X, Y)}


_RP = re.compile(r"rule 'pl(\d+)'.*?actual ([\d.]+) ps")


def kicad_delays(board, legs, kicad_cli="kicad-cli", timeout=300):
    """KiCad's own delay in ps per net: {net: ps}.  `legs` = {net: {"ps_per_mm": {layer: ps/mm}, "via_mm": ..., "total_mm": ...}} (the
    leg_decomposition of each net).  A delay-only tuning profile (width and gap 0, so no geometry rule is created) carrying those
    per-layer ps/mm and the via propagation delay goes into a TEMP .kicad_pro, every net gets a netclass naming it, and one
    `(constraint length (max 0ps))` rule per net makes kicad-cli report the net's delay (tracks x ps/mm per layer, via span x
    via_prop_delay, pad-to-die).  None when kicad-cli fails or the rule file is ignored (the fxcanary rule did not fire)."""
    nets = sorted(legs)
    if not nets:
        return {}
    from . import drcguard as DG

    def edit(pd):
        entries, classes, pats = [], pd.setdefault("net_settings", {}).setdefault("classes", []), pd["net_settings"].setdefault("netclass_patterns", [])
        profs = []
        for i, net in enumerate(nets):
            lg = legs[net]
            ps = lg.get("ps_per_mm") or {}
            mean = (sum(ps[l] * lg["layer_mm"].get(l, 0.0) for l in ps) / max(1e-9, sum(lg["layer_mm"].get(l, 0.0) for l in ps))) if ps else 0.0
            profs.append({"profile_name": f"FXD{i}", "type": 0, "target_impedance": 0.0, "enable_time_domain_tuning": True,
                          "layer_entries": [{"signal_layer": l, "top_reference_layer": "", "bottom_reference_layer": "", "width": 0, "diff_pair_gap": 0,
                                             "delay": int(round(v * 1e6))} for l, v in sorted(ps.items())],
                          "via_prop_delay": int(round(mean * 1e6)), "via_overrides": []})
            classes.append({"name": f"FXC{i}", "tuning_profile": f"FXD{i}"})
            pats.append({"netclass": f"FXC{i}", "pattern": net})
        pd["tuning_profiles"] = {"meta": {"version": 0}, "tuning_profiles_impedance_geometric": profs}
    rules = "(version 1)\n" + "".join(
        f"(rule \"pl{i}\" (condition \"A.NetName == '{_q(x)}'\") (constraint length (max 0ps)))\n" for i, x in enumerate(nets))
    try:
        rep = DG.drc_json(board, kicad_cli, timeout, rules=rules, only_rules=["length_out_of_range"], complete=False, pro_edit=edit, refill=False)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"netlen: kicad delay run failed: {e}", file=sys.stderr)
        return None
    out = {}
    for v in rep.get("violations", []):
        m = _RP.search(v.get("description", ""))
        if m:
            out[nets[int(m.group(1))]] = max(out.get(nets[int(m.group(1))], 0.0), float(m.group(2)))
    return out


def recon_tol(dp, dn):
    """mm: RECON_TOL_MM for a plain 2-terminal leg, growing by RECON_JOINT_MM per via and per pad past two on the worse leg (sections)."""
    j = max(d.get("n_vias", 0) + max(0, d.get("n_pads", 0) - 2) for d in (dp, dn))
    return round(RECON_TOL_MM + RECON_JOINT_MM * j, 4)


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
    The reconciliation gates the verdict only where own numbers enter it: the path basis.  A 2-terminal pair is KiCad's number alone (mm, or
    ps through a delay-only tuning profile); `recon` is then advice.  KiCad counts a straight run into a pad in full (a track ending 1 mm inside
    adds that mm) but clips a detour inside a pad to the shortest path through it (OptimiseTracesInPads, tested), so our raw segment sum can
    read longer: D71cx's "lay and tune trim the in-pad glue" is what removes the difference."""
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
    out["recon"] = {"p": round(dp["total_mm"] - kl["p"], 4), "n": round(dn["total_mm"] - kl["n"], 4), "tol": recon_tol(dp, dn)}
    out.update(p_ps=dp["delay_ps"], n_ps=dn["delay_ps"], own_ps=[dp["delay_ps"], dn["delay_ps"]])
    ps_bad = []
    if grade_ps and dp["delay_ps"] is not None and dn["delay_ps"] is not None:
        # D71cx (ps): KiCad's own delay is the number (a delay-only tuning profile with our stackup ps/mm); our sum per layer only reconciles it
        kd = kicad_delays(f, {p: dp, n: dn}, kicad_cli)
        if kd is None or p not in kd or n not in kd:
            out["why"] = "kicad-cli delay run failed or its rule file was ignored: no own-math fallback (D71cx)"
            return out
        out.update(p_ps=kd[p], n_ps=kd[n])
        tol_ps = out["recon"]["tol"] * max(list(dp["ps_per_mm"].values()) + list(dn["ps_per_mm"].values()) + [1.0])
        out["recon"].update(p_ps=round(dp["delay_ps"] - kd[p], 3), n_ps=round(dn["delay_ps"] - kd[n], 3), tol_ps=round(tol_ps, 3))
        ps_bad = [k for k in ("p", "n") if abs(out["recon"][k + "_ps"]) > tol_ps]
        dp = dict(dp, delay_ps=kd[p])
        dn = dict(dn, delay_ps=kd[n])
    if joined(B, p, n, cons):
        meas = CH.measure_paths(board, [p, n], rows, mode="all")
        out["basis"] = "path"
        o = orient(B, p, n, meas, cons)
        if not o:
            out["why"] = "no end-to-end path for both legs"
            return out
        ors = o["ors"]
        L = lambda q: q["length_mm"] + q["via_mm"]            # noqa: E731
        for a, b, _lbl in ors:
            ma, mb = CH.path_metrics(a, rows, planes), CH.path_metrics(b, rows, planes)
            d_ps = None if ma["delay_ps"] is None or mb["delay_ps"] is None else round(ma["delay_ps"] - mb["delay_ps"], 3)
            out["paths"].append({"a": a["a"], "b": a["b"], "p": round(L(a), 3), "n": round(L(b), 3), "d": round(L(a) - L(b), 3),
                                 "skew": round(abs(L(a) - L(b)), 3), "p_ps": ma["delay_ps"], "n_ps": mb["delay_ps"], "d_ps": d_ps,
                                 "stub": max(a["stub_max_mm"], b["stub_max_mm"])})
        w = max(out["paths"], key=lambda r: r["skew"])
        out["skew_mm"] = w["d"]
        dps = [r["d_ps"] for r in out["paths"]]
        out["skew_ps"] = None if None in dps else max(dps, key=abs)
        out["taps"], out["spurs"], out["ends"] = o["taps"], o["spurs"], o["ends"]
        out["stub_mm"] = max([x["mm"] for x in o["taps"] + o["spurs"]] + [0.0])
    else:
        out["basis"] = "kicad"
        out["skew_mm"] = round(kl["p"] - kl["n"], 4)
        out["skew_ps"] = None if dp["delay_ps"] is None or dn["delay_ps"] is None else round(dp["delay_ps"] - dn["delay_ps"], 3)
        out["paths"] = [{"a": "", "b": "", "p": kl["p"], "n": kl["n"], "d": out["skew_mm"], "skew": abs(out["skew_mm"]),
                         "p_ps": dp["delay_ps"], "n_ps": dn["delay_ps"], "d_ps": out["skew_ps"], "stub": 0.0}]
    # the reconciliation gates only where OUR numbers enter the verdict (the per-orientation path basis).  A 2-terminal pair is KiCad's number
    # alone, in mm and in ps (delay-only tuning profile); the own sum is advice there (KiCad clips a detour inside a pad to the shortest
    # path, so such a leg reads longer in our raw sum than in KiCad's number).
    bad = [k for k in ("p", "n") if abs(out["recon"][k]) > out["recon"]["tol"]] if out["basis"] == "path" else []
    ps_bad = ps_bad if out["basis"] == "path" else []
    out["status"] = "MEASURE_DISAGREE" if bad or ps_bad else "ok"
    if bad or ps_bad:
        out["why"] = "own per-leg decomposition vs KiCad total: " + ", ".join([f"{k} {out['recon'][k]:+.3f} mm" for k in bad] +
                                                                              [f"{k} {out['recon'][k + '_ps']:+.2f} ps" for k in ps_bad])
    return out
