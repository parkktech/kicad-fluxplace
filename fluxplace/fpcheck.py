"""Footprint verification: FP_* geometry (pcbnew) and the LANDPATTERN_PAD_* pad table.

A footprint defect is fixed in the footprint, from the manufacturer's land pattern. It is never waived:
`review._waived` refuses FP_* / LANDPATTERN_* waivers (route-ready and review both), and the parts gate
(`partsgate.gate`) blocks on them even with `--allow-undocumented`. The one exception is a spec
`landpattern.allow` entry that cites the manufacturer's own drawing for the measured dimension (`apply_allow`).

Measurement is pcbnew's shape API: `SHAPE.Collide(other, clearance)` is bisected to the exact edge-to-edge
distance, so rotated and rounded pads are measured on their real outline (no rectangle shortcut).

  fp-check --board B            every footprint instance on a board
  fp-check --lib DIR.pretty     every footprint of a library (or --footprint NAME)
  --constraints C               clearance floor from [fab]/[route]; --json; --report-dir D (writes files only then)

Codes (FAIL unless noted): FP_PAD_CLEARANCE, FP_PAD_HOLE_CLEARANCE, FP_KEEPOUT_SELF, FP_PAD_OUTSIDE_COURTYARD (WARN),
FP_UNREADABLE, FP_UNCHECKED (pcbnew missing: the geometry is NOT CHECKED, which is a blocker, not a pass),
LANDPATTERN_PAD_MISMATCH / _MISSING / _EXTRA, LANDPATTERN_VENDOR_MISSING.
"""
import glob
import hashlib
import os
import shutil
import tempfile

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
GUARD_PREFIXES = ("FP_", "LANDPATTERN_")
ALLOWABLE = ("FP_PAD_CLEARANCE", "FP_PAD_HOLE_CLEARANCE")
DEFAULT_CLEARANCE_MM = 0.127
DEFAULT_HOLE_MM = 0.25
TOL_MM = 0.03
REFUSED = "footprint defects are fixed in the footprint from the manufacturer's land pattern, not waived"


def is_geometry(code):
    """True for the codes that block the parts gate and are never waived."""
    return code.startswith("FP_") or code.startswith("LANDPATTERN_PAD_") or code == "LANDPATTERN_VENDOR_MISSING"


def _f(level, code, msg, refs=(), **extra):
    d = {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs)), "value": None, "limit": None}
    d.update(extra)
    return d


def floors(cons=None, board=None):
    """-> (clearance_mm, hole_mm, source). Clearance: [fab] / [route] when given, else the board's minimum
    clearance, else 0.127. Hole: the board's hole-clearance rule, else 0.25."""
    import pcbnew
    fab, route = (cons or {}).get("fab") or {}, (cons or {}).get("route") or {}
    for v in (fab.get("min_clearance_mm"), route.get("min_clearance_mm"), route.get("clearance_mm")):
        if v:
            clr, src = float(v), "constraints"
            break
    else:
        m = board.GetDesignSettings().m_MinClearance if board is not None else 0
        clr, src = (pcbnew.ToMM(m), "board") if m > 0 else (DEFAULT_CLEARANCE_MM, "default")
    h = board.GetDesignSettings().m_HoleClearance if board is not None else 0
    hole = pcbnew.ToMM(h) if h > 0 else DEFAULT_HOLE_MM
    return clr, hole, src


def _gap(pcbnew, sa, sb, cap):
    """Edge-to-edge distance in mm of two SHAPEs, exact below `cap`; returns cap when not closer than cap."""
    hi = pcbnew.FromMM(cap)
    if not sa.Collide(sb, hi):
        return cap
    if sa.Collide(sb, 0):
        return 0.0
    lo = 0                                   # invariant: no collision at lo (gap >= lo), collision at hi
    while hi - lo > 100:                     # 0.1 micron
        mid = (lo + hi) // 2
        if sa.Collide(sb, mid):
            hi = mid
        else:
            lo = mid
    return pcbnew.ToMM(lo)


class _P:
    """One pad, measured once: copper shape per copper layer, hole shape, bounding box, label."""

    def __init__(self, pcbnew, pad, cu_ids):
        self.pad = pad
        self.num = pad.GetNumber()
        self.label = self.num if self.num else "NPTH"
        self.npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
        self.cu = [L for L in pad.GetLayerSet().Seq() if L in cu_ids]
        self.shape = {L: pad.GetEffectiveShape(L) for L in self.cu}
        self.hole = pad.GetEffectiveHoleShape() if pad.HasHole() else None
        bb = pad.GetBoundingBox()
        self.box = (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())


def _layer_name(pcbnew, L):
    try:
        return pcbnew.BOARD().GetLayerName(L)
    except Exception:
        return str(L)


def _near(a, b, m):
    return not (a[2] + m < b[0] or b[2] + m < a[0] or a[3] + m < b[1] or b[3] + m < a[1])


def check_footprint(pcbnew, fp, ref="", clr=DEFAULT_CLEARANCE_MM, hole=DEFAULT_HOLE_MM):
    """All FP_* checks for one pcbnew FOOTPRINT -> [finding]. `ref` ('' for a library footprint) names the refs."""
    name = fp.GetFPID().GetLibItemName() if fp.GetFPID() else ""
    refs = [ref] if ref else []
    cu_ids = set(pcbnew.LSET.AllCuMask().Seq())
    P = [_P(pcbnew, p, cu_ids) for p in fp.Pads()]
    who = (f"{ref} ({name})" if name else ref) if ref else name
    out = []
    m = pcbnew.FromMM(max(clr, hole))
    for i, a in enumerate(P):
        for b in P[i + 1:]:
            if not _near(a.box, b.box, m) or not (set(a.cu) & set(b.cu)):
                continue
            worst = min(((_gap(pcbnew, a.shape[L], b.shape[L], clr), L) for L in set(a.cu) & set(b.cu)),
                        key=lambda t: t[0])
            if worst[0] >= clr:
                continue
            layer = _layer_name(pcbnew, worst[1])
            if a.num and a.num == b.num:
                out.append(_f(INFO, "FP_PAD_CLEARANCE",
                              f"{who} pads {a.label} and {b.label} (same pad number: merged copper by design, exempt) "
                              f"on {layer}: {worst[0]:.3f} mm, floor {clr:.3f} mm", refs, pads=[a.label, b.label],
                              actual=round(worst[0], 4), floor=clr))
                continue
            out.append(_f(FAIL, "FP_PAD_CLEARANCE",
                          f"{who} pads {a.label} and {b.label} on {layer}: copper edge-to-edge {worst[0]:.3f} mm, "
                          f"floor {clr:.3f} mm", refs, pads=[a.label, b.label], actual=round(worst[0], 4), floor=clr))
    for a in P:                               # copper of a against the hole of b (b's NPTH or drill)
        if not a.cu:
            continue
        for b in P:
            if b is a or b.hole is None or not _near(a.box, b.box, m):
                continue
            if a.num and a.num == b.num:
                continue                       # same pad number: the drill is part of that pad (merged by design)
            d = min(_gap(pcbnew, a.shape[L], b.hole, hole) for L in a.cu)
            if d < hole:
                out.append(_f(FAIL, "FP_PAD_HOLE_CLEARANCE",
                              f"{who} pad {a.label} copper to {'NPTH ' if b.npth else ''}hole of pad {b.label}: "
                              f"{d:.3f} mm, hole clearance floor {hole:.3f} mm", refs, pads=[a.label, b.label],
                              actual=round(d, 4), floor=hole))
    out += _keepout(pcbnew, fp, P, refs, who)
    out += _courtyard(pcbnew, fp, P, refs, who)
    return out


def _keepout(pcbnew, fp, P, refs, who):
    out = []
    for z in fp.Zones():
        if not (z.GetIsRuleArea() and z.GetDoNotAllowPads()):
            continue
        outline = z.Outline()
        zl = set(z.GetLayerSet().Seq())
        hits = []
        for a in P:
            if any(L in zl and outline.Collide(a.shape[L], 0) for L in a.cu):
                hits.append(a.label)
            elif a.hole is not None and outline.Collide(a.hole, 0):
                hits.append(a.label + " (hole)")
        if hits:
            out.append(_f(FAIL, "FP_KEEPOUT_SELF",
                          f"{who} rule area forbids pads, and contains its own pad(s)/hole(s): {', '.join(hits)}",
                          refs, pads=[h.split(" ")[0] for h in hits]))
    return out


def _courtyard(pcbnew, fp, P, refs, who):
    crt = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    if crt.OutlineCount() == 0:
        return []
    out = []
    for a in P:
        if not a.cu:
            continue
        # every vertex of the pad's real outline (circles and rounded pads are polygons here, so no bbox shortcut)
        verts = []
        for L in a.cu[:1]:
            poly = a.pad.GetEffectivePolygon(L)
            for k in range(poly.OutlineCount()):
                line = poly.Outline(k)
                verts += [(line.CPoint(i).x, line.CPoint(i).y) for i in range(line.PointCount())]
        if verts and not all(crt.Collide(pcbnew.VECTOR2I(x, y), 0) for x, y in verts):
            out.append(_f(WARN, "FP_PAD_OUTSIDE_COURTYARD",
                          f"{who} pad {a.label} is not inside its courtyard", refs, pads=[a.label]))
    return out


def check_board(board_path, cons=None, board=None):
    """FP_* findings for every footprint instance (refs set on each). -> [finding]"""
    import pcbnew
    B = board if board is not None else pcbnew.LoadBoard(board_path)
    if B is None:                            # pcbnew cannot open it: the geometry is NOT CHECKED (never a pass)
        return [_f(WARN, "FP_UNCHECKED", f"footprint geometry NOT CHECKED: pcbnew could not load {board_path}")]
    clr, hole, _src = floors(cons, B)
    out = []
    for fp in B.GetFootprints():
        out += check_footprint(pcbnew, fp, fp.GetReference(), clr, hole)
    return out


def check_lib(lib_dir, names=None, cons=None):
    """FP_* findings for footprints of a .pretty library (all, or `names`). -> [finding] with 'fp' set."""
    import pcbnew
    clr, hole, _src = floors(cons, None)
    out = []
    for n in (names if names is not None else pcbnew.FootprintEnumerate(lib_dir)):
        fp = pcbnew.FootprintLoad(lib_dir, n)
        if fp is None:
            out.append(_f(FAIL, "FP_UNREADABLE", f"{n}: not readable in {lib_dir}", fp=n))
            continue
        for f in check_footprint(pcbnew, fp, "", clr, hole):
            f["fp"] = n
            out.append(f)
    return out


def pad_tuples(pcbnew, fp):
    """[(num, x, y, w, h)] in mm relative to the footprint anchor (the same shape as facts `pad_geom`)."""
    return [(p.GetNumber(), p.GetFPRelativePosition().x / 1e6, p.GetFPRelativePosition().y / 1e6,
             p.GetSize().x / 1e6, p.GetSize().y / 1e6) for p in fp.Pads()]


def vendor_pad_tuples(path):
    """Pad table of a manufacturer .kicad_mod (any folder: copied into a temporary .pretty)."""
    import pcbnew
    tmp = tempfile.mkdtemp(prefix="fpvendor")
    try:
        lib = os.path.join(tmp, "v.pretty")
        os.mkdir(lib)
        name = os.path.splitext(os.path.basename(path))[0]
        shutil.copy(path, os.path.join(lib, name + ".kicad_mod"))
        fp = pcbnew.FootprintLoad(lib, name)
        return pad_tuples(pcbnew, fp) if fp is not None else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _origin(pts, origin):
    if origin == "pad1":
        o = next((p for p in pts if str(p[0]) == "1"), None)
        if o is None:
            return None
        return [(n, x - o[1], y - o[2], w, h) for n, x, y, w, h in pts]
    return list(pts)


def table_check(ref, pts, cited, origin="footprint", tol=TOL_MM, label="cited drawing"):
    """Pad-by-pad comparison of footprint pads (`pts`) with the cited table. Pads match on number, nearest first.
    Position or size beyond `tol` on any pad is LANDPATTERN_PAD_MISMATCH naming the pad and the delta."""
    out = []
    fpts = _origin(pts, origin)
    if fpts is None:
        return [_f(FAIL, "LANDPATTERN_PAD_MISSING", f"{ref}: pad 1 not on the footprint (origin pad1)", [ref])]
    fpts = [p for p in fpts if str(p[0])]
    cts = [(str(c["num"]), float(c["x"]), float(c["y"]), float(c["w"]), float(c["h"])) for c in cited]
    used = set()
    for num, sx, sy, sw, sh in cts:
        cand = [i for i, p in enumerate(fpts) if str(p[0]) == num and i not in used]
        if not cand:
            out.append(_f(FAIL, "LANDPATTERN_PAD_MISSING",
                          f"{ref} pad {num}: in the {label} at ({sx:.3f},{sy:.3f}) but not on the footprint",
                          [ref], pads=[num]))
            continue
        i = min(cand, key=lambda k: (fpts[k][1] - sx) ** 2 + (fpts[k][2] - sy) ** 2)
        used.add(i)
        _, x, y, w, h = fpts[i]
        dx, dy = x - sx, y - sy
        pos_ok = abs(dx) <= tol and abs(dy) <= tol
        size_ok = (abs(w - sw) <= tol and abs(h - sh) <= tol) or (abs(w - sh) <= tol and abs(h - sw) <= tol)
        if pos_ok and size_ok:
            continue
        what = "position" if not pos_ok else "size"
        out.append(_f(FAIL, "LANDPATTERN_PAD_MISMATCH",
                      f"{ref} pad {num}: {what} off the {label} (tol {tol} mm): footprint ({x:.3f},{y:.3f}) "
                      f"{w:.3f}x{h:.3f} vs ({sx:.3f},{sy:.3f}) {sw:.3f}x{sh:.3f}; delta dx {dx:+.3f} dy {dy:+.3f}",
                      [ref], pads=[num], actual=round(max(abs(dx), abs(dy)), 4)))
    for i, p in enumerate(fpts):
        if i not in used:
            out.append(_f(FAIL, "LANDPATTERN_PAD_EXTRA",
                          f"{ref} pad {p[0]} at ({p[1]:.3f},{p[2]:.3f}) is not in the {label}", [ref], pads=[str(p[0])]))
    return out


def table_findings(facts, spec, ds_dir=None):
    """landpattern.pads / landpattern.vendor_footprint for every spec component on the board."""
    out = []
    parts = (facts or {}).get("parts") or {}
    for c in (spec or {}).get("components", []):
        ref, lp = c.get("ref"), c.get("landpattern") or {}
        if not ref or ref not in parts or not (lp.get("pads") or lp.get("vendor_footprint")):
            continue
        origin = lp.get("origin", "footprint")
        tol = float(lp.get("tol", TOL_MM))
        pts = parts[ref].get("pad_geom") or []
        if lp.get("pads"):
            out += table_check(ref, pts, lp["pads"], origin, tol, label=f"cited drawing {lp.get('source', '')}".strip())
        if lp.get("vendor_footprint"):
            vp = lp["vendor_footprint"]
            path = vp if os.path.isabs(vp) else os.path.join(ds_dir or ".", vp)
            vpts = vendor_pad_tuples(path) if os.path.isfile(path) else None
            if vpts is None:
                out.append(_f(FAIL, "LANDPATTERN_VENDOR_MISSING",
                              f"{ref}: vendor_footprint {vp} is not a readable .kicad_mod", [ref]))
            else:
                cited = [{"num": n, "x": x, "y": y, "w": w, "h": h} for n, x, y, w, h in _origin(vpts, origin) or []]
                out += table_check(ref, pts, cited, origin, tol, label=f"vendor footprint {vp}")
    return out


def apply_allow(findings, spec, ds_dir=None):
    """landpattern.allow: a FP_PAD_CLEARANCE / FP_PAD_HOLE_CLEARANCE the manufacturer's drawing specifies.
    Accepted (INFO) only when the cited file exists in the datasheets dir and value_mm matches the measured value
    within 0.03 mm; otherwise the FAIL stays and says why."""
    from . import partdocs as PD
    allows = {}
    for c in (spec or {}).get("components", []):
        for a in (c.get("landpattern") or {}).get("allow") or []:
            allows.setdefault(c.get("ref"), []).append(a)
    out = []
    for f in findings:
        ref = (f.get("refs") or [None])[0]
        ent = None
        if f["code"] in ALLOWABLE and f.get("level") == FAIL and ref:
            ent = next((a for a in allows.get(ref, []) if a.get("code") == f["code"]
                        and sorted(map(str, a.get("pads", []))) == sorted(f.get("pads", []))), None)
        if ent is None:
            out.append(f)
            continue
        src = ent.get("source") or ""
        fname, _pages = PD.parse_source(src)
        path = os.path.join(ds_dir or "", fname) if fname else ""
        if not fname or not os.path.isfile(path):
            out.append(dict(f, msg=f["msg"] + f"; landpattern.allow refused: cited drawing {src!r} not in the datasheets dir"))
        elif abs(float(ent.get("value_mm", -99)) - float(f.get("actual", 0))) > TOL_MM:
            out.append(dict(f, msg=f["msg"] + f"; landpattern.allow refused: value_mm {ent.get('value_mm')} vs measured "
                                    f"{f.get('actual')} (tol {TOL_MM})"))
        else:
            out.append(dict(f, level=INFO, msg=f"{f['msg']} -- allowed: {src} specifies {ent['value_mm']} mm"))
    return out


def refused_waivers(waivers):
    """INFO findings for each waiver entry that names an FP_* / LANDPATTERN_* code (the waiver is not applied)."""
    out = []
    for w in waivers or ():
        code, _, rx = str(w).partition(":")
        if code.startswith(GUARD_PREFIXES):
            out.append(_f(INFO, "FP_WAIVER_REFUSED", f"waiver {w!r} refused: {REFUSED}"))
    return out


def gate_findings(spec, cons, facts, ds_dir=None, board_path=None):
    """Everything the parts gate needs from footprint geometry: board instances (or drawn libs when no board),
    pad tables. -> [finding] after landpattern.allow. pcbnew missing -> one FP_UNCHECKED FAIL (never a pass)."""
    from . import review as R
    out = []
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        return [_f(FAIL, "FP_UNCHECKED", "pcbnew is not importable: footprint geometry NOT CHECKED")]
    board_path = board_path or (facts or {}).get("board_path")
    board_names = set()
    if board_path and os.path.isfile(board_path):
        out += check_board(board_path, cons)
        board_names = {(p.get("footprint") or "").split(":")[-1] for p in (facts or {}).get("parts", {}).values()}
    docs = (cons or {}).get("docs", {})
    drawn = R.project_drawn(docs.get("project_libs", ()), ds_dir or docs.get("datasheets"))
    for name, d in sorted(drawn.items()):
        if name in board_names:
            continue                       # placed on the board: checked as an instance above
        for f in check_lib(d, [name], cons):
            refs = [r for r, p in (facts or {}).get("parts", {}).items() if (p.get("footprint") or "").split(":")[-1] == name]
            out.append(dict(f, refs=refs or []))
    out += table_findings(facts, spec, ds_dir or docs.get("datasheets"))
    return apply_allow(out, spec, ds_dir or docs.get("datasheets"))


def summary(findings):
    n = {FAIL: 0, WARN: 0, INFO: 0}
    for f in findings:
        n[f["level"]] += 1
    return n


def add_parser(sub):
    p = sub.add_parser("fp-check", help="footprint geometry: pad/pad, pad/hole, keep-out, courtyard; pad table vs the drawing")
    p.add_argument("--board", default=None, help="board .kicad_pcb: every footprint instance")
    p.add_argument("--lib", default=None, help="footprint library DIR.pretty")
    p.add_argument("--footprint", default=None, help="with --lib: one footprint name")
    p.add_argument("--constraints", default=None, help="[fab]/[route] clearance floor")
    p.add_argument("--json", action="store_true", help="print the findings as JSON")
    p.add_argument("--out-dir", dest="out_dir", default=None, help="write fp-check.txt/.json here (none by default)")
    p.add_argument("--top", type=int, default=10, help="findings shown (default 10)")
    p.set_defaults(fn=cmd_fp_check)


def cmd_fp_check(a):
    import json
    from . import constraints as C
    if not (a.board or a.lib):
        raise SystemExit("fp-check: give --board B and/or --lib DIR.pretty")
    cons = C.load(a.constraints)
    f = []
    if a.board:
        f += check_board(a.board, cons)
    if a.lib:
        names = [a.footprint] if a.footprint else None
        f += check_lib(a.lib, names, cons)
    n = summary(f)
    bad = n[FAIL] or any(x["code"] == "FP_UNCHECKED" for x in f)
    verdict = f"fp-check: {'FAIL' if bad else 'PASS'} - {n[FAIL]} FAIL, {n[WARN]} WARN, {n[INFO]} INFO"
    lines = [verdict] + [f"  {x['level']:4s} {x['code']:26s} {x['msg']}" for x in
                         sorted(f, key=lambda x: (x["level"] != FAIL, x["code"], x["msg"]))]
    txt = "\n".join(lines)
    if a.json:
        print(json.dumps(f, indent=1))
    else:
        print("\n".join(lines[:a.top + 1]))
        if len(lines) > a.top + 1:
            print(f"  ... {len(lines) - a.top - 1} more (--out-dir writes all)")
    if a.out_dir:
        os.makedirs(a.out_dir, exist_ok=True)
        with open(os.path.join(a.out_dir, "fp-check.txt"), "w") as fh:
            fh.write(txt + "\n")
        with open(os.path.join(a.out_dir, "fp-check.json"), "w") as fh:
            json.dump(f, fh, indent=1)
    if n[FAIL] or any(x["code"] == "FP_UNCHECKED" for x in f):
        raise SystemExit(1)
