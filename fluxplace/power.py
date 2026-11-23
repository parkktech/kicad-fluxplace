"""Power sizing from current, voltage and the fab's copper: ONE place the router, rules cascade, pairs/fanout and
review read rail widths from (constraints.power_width_mm / pour_nets and rules._legacy delegate here).

Per `[power."NET"]` (fields: max_current_ma | load_a | current_a, max_v | nominal_v, pour, width_mm, temp_rise_c):
  * track width   IPC-2221 (k = 0.048 external, 0.024 internal; IPC-2152 is NOT implemented, IPC-2221 is the
                  conservative of the two), for the layer's copper weight and a temperature rise
                  (`[env] trace_rise_c`, default DEFAULT_RISE_C = 10 C)
  * copper weight the stackup's `weight_oz` per layer when `[[stackup.layers]]` states it, else the cheapest
                  weight the `[fab] profile` row offers (fabcaps: outer min(outer_oz), inner min(inner_oz)), else 1 / 0.5 oz
  * vias          plated barrel ampacity: area = pi/4 ((d + 2t)^2 - d^2), t = plating (fab_capabilities.json plating_um,
                  IPC-6012 Class 2, unverified), I = 0.024 dT^0.44 A^0.725 (A in mil^2); n = ceil(I / I_via)
  * clearance     IPC-2221B table 6-1 (B1 internal, B2 external uncoated, A5 external coated; TRANSCRIBED FROM
                  MEMORY, unverified against the standard): voltage -> minimum spacing, applied only when above the default
  * pour policy   POUR when `pour = true`, or when the required width exceeds `[route] power_pour_over_mm`
                  (default POUR_OVER_MM = 2.0 mm, ~10x the 0.2 mm default signal track); otherwise a track at the
                  computed width, rounded UP to 0.05 mm. Pours cost nothing extra to fabricate: prefer them when area allows.
A rail with no current figure is UNKNOWN (width None, falls back to the caller's default): review lists it.
"""
import math
import os

DEFAULT_RISE_C = 10.0
POUR_OVER_MM = 2.0
STUB_EXEMPT_MM = 2.0            # narrow copper shorter than this per net+layer is a pad neck-down, not a power track
K_EXT, K_INT = 0.048, 0.024
MIL = 0.0254

# IPC-2221B table 6-1: (max volts, B1 internal, B2 external uncoated <3050 m, A5 external coated), mm
CLEARANCE_TABLE = ((15, 0.05, 0.1, 0.13), (30, 0.05, 0.1, 0.13), (50, 0.1, 0.6, 0.13), (100, 0.1, 0.6, 0.13),
                   (150, 0.2, 0.6, 0.4), (170, 0.2, 1.25, 0.4), (250, 0.2, 1.25, 0.4), (300, 0.2, 1.25, 0.8),
                   (500, 0.25, 2.5, 0.8))
CLEARANCE_SOURCE = "IPC-2221B table 6-1 (B1/B2/A5), transcribed from memory - unverified against the standard"


def ipc2221_width_mm(current_a, rise_c=DEFAULT_RISE_C, oz=1.0, internal=False):
    """Conductor width (mm). A[mil^2] = (I / (k dT^0.44))^(1/0.725); w = A / (1.378 mil/oz * oz)."""
    if not current_a or current_a <= 0:
        return 0.0
    k = K_INT if internal else K_EXT
    area = (current_a / (k * rise_c ** 0.44)) ** (1 / 0.725)
    return area / (1.378 * oz) * MIL


def via_current_a(drill_mm, plating_um=20.0, rise_c=DEFAULT_RISE_C):
    t = plating_um / 1000.0
    area_mm2 = math.pi / 4.0 * ((drill_mm + 2 * t) ** 2 - drill_mm ** 2)
    return K_INT * rise_c ** 0.44 * (area_mm2 / MIL ** 2) ** 0.725


def clearance_for_v(volts, kind="external"):
    """Minimum conductor spacing (mm) for `volts`; kind internal | external | coated."""
    col = {"internal": 1, "external": 2, "coated": 3}[kind]
    for row in CLEARANCE_TABLE:
        if volts <= row[0]:
            return row[col]
    return CLEARANCE_TABLE[-1][col] * (volts / 500.0)       # above the table: scaled, flagged by the caller


def _ceil05(x):
    return math.ceil(x / 0.05 - 1e-9) * 0.05


def _ozs(cons, layers):
    """-> (outer_oz, inner_oz, source)."""
    sl = [l for l in (((cons or {}).get("stackup") or {}).get("layers") or []) if isinstance(l, dict)
          and l.get("weight_oz") and str(l.get("type", "copper")) == "copper"]
    if sl:
        o = float(sl[0]["weight_oz"])
        inner = [float(l["weight_oz"]) for l in sl[1:-1]]
        return o, (min(inner) if inner else o), "[[stackup.layers]] weight_oz"
    from . import constraints as C
    fab = C.fab_profile(cons, layers=layers)
    hard = fab.get("hard") or {}
    if hard.get("outer_oz") or hard.get("inner_oz"):
        return (float(min(hard["outer_oz"])) if hard.get("outer_oz") else 1.0,
                float(min(hard["inner_oz"])) if hard.get("inner_oz") else 0.5, f"fab profile {fab['profile']} cheapest weights")
    return 1.0, 0.5, "default 1 oz outer / 0.5 oz inner"


def oz_from_stackup(rows):
    """(outer_oz, inner_oz) from stackup rows [{type: copper, thickness mm}] (1 oz = 0.0347 mm, snapped to 0.5 oz steps, min 0.5)."""
    cu = [float(r["thickness"]) for r in rows or [] if r.get("type") == "copper" and r.get("thickness")]
    if not cu:
        return None
    snap = lambda t: max(0.5, round(t / 0.0347 * 2) / 2)
    return snap(cu[0]), (snap(min(cu[1:-1])) if len(cu) > 2 else snap(cu[0]))


def _plating_um():
    from . import fabcaps as FC
    return float(FC.data().get("plating_um", {}).get("value", 20))


def current_a(p):
    """Rail current in amps from a [power.X] table (largest of max_current_ma, current_a, load_a); None = unknown."""
    vals = []
    if p.get("max_current_ma") is not None:
        vals.append(float(p["max_current_ma"]) / 1000.0)
    for k in ("current_a", "load_a"):
        if p.get(k) is not None:
            vals.append(float(p[k]))
    return max(vals) if vals else None


def rail(cons, net, layers=None, board=None, oz=None):
    """Sizing record for one `[power.NET]` (dict) or None when the net has no [power] table."""
    p = ((cons or {}).get("power") or {}).get(net)
    if not isinstance(p, dict):
        return None
    from . import constraints as C
    fab = C.fab_profile(cons, layers=layers, board=board)
    n_layers = fab.get("layers") or layers
    rise = float(p.get("temp_rise_c", ((cons or {}).get("env") or {}).get("trace_rise_c", DEFAULT_RISE_C)))
    amps = current_a(p)
    volts = max([float(p[k]) for k in ("max_v", "nominal_v") if p.get(k) is not None] or [0.0])
    oz_o, oz_i, oz_src = (oz[0], oz[1], "board stackup") if oz else _ozs(cons, n_layers)
    pour_over = float(((cons or {}).get("route") or {}).get("power_pour_over_mm", POUR_OVER_MM))
    r = {"net": net, "current_a": amps, "max_v": volts or None, "rise_c": rise, "oz_outer": oz_o, "oz_inner": oz_i,
         "oz_source": oz_src, "layers": n_layers, "pour_over_mm": pour_over}
    if volts:
        r["clearance_mm"] = {k: clearance_for_v(volts, k) for k in ("internal", "external", "coated")}
    if "width_mm" in p:
        r.update(width_outer=None, width_inner=None, track_mm=float(p["width_mm"]), width_source="[power] width_mm")
    elif amps:
        wo = ipc2221_width_mm(amps, rise, oz_o, False)
        wi = ipc2221_width_mm(amps, rise, oz_i, True)
        r.update(width_outer=round(wo, 3), width_inner=round(wi, 3), track_mm=round(_ceil05(max(wo, wi, 0.0)), 2),
                 width_source=f"IPC-2221, {amps:g} A, dT {rise:g} C")
        drill = float(fab.get("min_drill_mm") or 0.2)
        iv = via_current_a(drill, _plating_um(), rise)
        r.update(via_drill_mm=drill, via_amps=round(iv, 2), vias_needed=max(1, math.ceil(amps / iv - 1e-9)))
    else:
        r.update(width_outer=None, width_inner=None, track_mm=None, width_source="no current figure")
    need = r.get("track_mm")
    explicit = "width_mm" in p                       # the owner stated the width: it never converts to a pour by itself
    r["pour"] = bool(p.get("pour")) or (need is not None and need > pour_over and not explicit)
    r["pour_reason"] = ("pour = true" if p.get("pour") else f"required width {need:g} mm > {pour_over:g} mm" if r["pour"] else
                        "track" if need is not None else "unknown current")
    return r


def plan(cons, layers=None, board=None, oz=None):
    return {n: rail(cons, n, layers, board, oz) for n, p in ((cons or {}).get("power") or {}).items() if isinstance(p, dict)}


def track_width_mm(cons, net, default_mm=None):
    """Routed track width for a power net: explicit width_mm > computed > default_mm."""
    r = rail(cons, net)
    if r is None or r.get("track_mm") is None:
        return default_mm
    return r["track_mm"]


SPINE_MM = 1.0      # D71cj: a pour rail is routed as a spine this wide, then poured


def route_widths(cons, layers=None, track_mm=0.15, exclude=()):
    """{net: router width mm} for every [power.*] rail with a current figure: pour rails -> min(SPINE_MM, need),
    other rails -> their IPC width; never below `track_mm` (the signal width). Rails with no current figure are left out.
    `[route] power_spine_mm` overrides SPINE_MM; `[route] power_spine = "ipc-outer"` gives pour rails their IPC outer width
    (rounded up to 0.05 mm) instead of the spine (the caller routes them on outer layers only, see rail_layers). `exclude`: plane nets the caller knows (zones on the board), not rails."""
    rt = (cons or {}).get("route") or {}
    spine = float(rt.get("power_spine_mm", SPINE_MM))
    ipc_outer = rt.get("power_spine") == "ipc-outer"     # D71cl run 2: pour rails at their IPC outer width (outer layers only)
    out = {}
    for n, r in plan(cons, layers=layers).items():
        if r is None or r.get("track_mm") is None or n in exclude:
            continue
        need = float(r["track_mm"])
        if r["pour"] and ipc_outer and r.get("width_outer"):
            w = _ceil05(float(r["width_outer"]))
        else:
            w = min(spine, need) if r["pour"] else need
        out[n] = round(max(w, float(track_mm)), 2)
    return out


def rail_layers(cons, sig):
    """Layers the rails step routes on: outer copper only under `[route] power_spine = "ipc-outer"`, else `sig`."""
    if ((cons or {}).get("route") or {}).get("power_spine") == "ipc-outer":
        outer = [x for x in sig if x in ("F.Cu", "B.Cu")]
        return outer or list(sig)
    return list(sig)


def is_pour(cons, net):
    r = rail(cons, net)
    return bool(r and r["pour"])


def signal_nets(cons):
    """Nets declared `[nets.NAME] role = "signal"`: sense / reference / logic nets that look like rails but carry no power."""
    return {n for n, v in ((cons or {}).get("nets") or {}).items() if isinstance(v, dict) and v.get("role") == "signal"}


def unknown_rails(cons, net_pads):
    """Rail-looking board nets (and [power.X] tables) with no current figure: [(net, volts_or_None, why)]."""
    from . import comprehend as CP
    declared = {n: p for n, p in ((cons or {}).get("power") or {}).items() if isinstance(p, dict)}
    signal = signal_nets(cons)
    out = []
    for n, p in sorted(declared.items()):
        if n not in signal and "width_mm" not in p and current_a(p) is None:
            out.append((n, float(p["max_v"]) if p.get("max_v") is not None else None, "[power] table without a current"))
    seen = set(declared) | signal
    for c in CP._power_nets(net_pads or {}):
        if c["net"] not in seen:
            out.append((c["net"], c["volts"], "no [power] table"))
    return out


def check(facts, cons):
    """Review findings (level, code, msg, refs): POWER_WIDTH, POWER_VIAS (FAIL), POWER_CURRENT_UNKNOWN (WARN)."""
    out = []
    layers = facts.get("copper_layers")
    inner = set(facts.get("plane_layers") or ())
    zone_nets = set(facts.get("zone_nets") or ())
    plan_ = plan(cons, layers=layers, oz=oz_from_stackup(facts.get("stackup")))
    copper_names = set()
    for s in facts.get("track_geom") or []:
        copper_names.add(s["layer"])
    outer = {"F.Cu", "B.Cu"}
    for net, r in sorted(plan_.items()):
        if r is None or r.get("track_mm") is None:
            continue
        # tracks narrower than needed, by layer kind (outer vs inner weights differ)
        short = {}
        for s in facts.get("track_geom") or []:
            if s["net"] != net or s.get("kind") != "track":
                continue
            need = r["width_outer"] if s["layer"] in outer else r["width_inner"]
            if need is None:
                need = r["track_mm"]
            if s["width"] < need - 1e-6:
                d = short.setdefault(s["layer"], {"len": 0.0, "min_w": s["width"], "need": need})
                d["len"] += s.get("len", 0.0)
                d["min_w"] = min(d["min_w"], s["width"])
        bad = {l: d for l, d in short.items() if d["len"] >= STUB_EXEMPT_MM and not (net in zone_nets and r["pour"])}
        if bad:
            tot = sum(d["len"] for d in bad.values())
            det = "; ".join(f"{l} {d['len']:.1f} mm at {d['min_w']:g} < {d['need']:.2f}" for l, d in sorted(bad.items()))
            out.append({"level": "FAIL", "code": "POWER_WIDTH", "refs": [net], "value": tot,
                        "msg": f"{net}: {tot:.1f} mm of track narrower than the {r['current_a']:g} A copper needs "
                               f"({r['width_source']}, {r['oz_outer']:g}/{r['oz_inner']:g} oz): {det}"})
        nv = sum(1 for v in facts.get("vias") or [] if v["net"] == net)
        nvn = r.get("vias_needed")
        if nvn and 0 < nv < nvn:
            out.append({"level": "FAIL", "code": "POWER_VIAS", "refs": [net], "value": nvn - nv,
                        "msg": f"{net}: {nv} via(s) carry {r['current_a']:g} A, needs {nvn} x {r['via_drill_mm']:g} mm "
                               f"({r['via_amps']:g} A each, dT {r['rise_c']:g} C)"})
    unk = unknown_rails(cons, facts.get("net_pads"))
    if unk:
        out.append({"level": "WARN", "code": "POWER_CURRENT_UNKNOWN", "refs": [n for n, _v, _w in unk],
                    "msg": f"{len(unk)} rail(s) with no current figure (fill [power.NET] max_current_ma from the regulator "
                           f"rating / load datasheets): " + ", ".join(f"{n}" + (f" {v:g}V" if v else "") for n, v, _w in unk[:24])
                           + (f" (+{len(unk) - 24})" if len(unk) > 24 else "")})
    return out


RAIL_ZONE = "fluxplace-rail:"


def rail_outline(segs, over):
    """[(x1, y1, x2, y2, width) mm] of one net on one layer -> [exterior ring [(x, y) mm]] of the union of every segment
    grown by half its width plus `over` (the copper the pour adds beside the spine). PHASE3C E: pcbnew's own geometry
    (SHAPE_SEGMENT outline, BooleanAdd, Simplify), so no shapely is needed; a segment of width w grown by `over` is one
    segment of width w + 2 * over, which is the same outline as shapely's buffer(over + w / 2, round caps)."""
    import pcbnew
    if not segs:
        return []
    poly = pcbnew.SHAPE_POLY_SET()
    for x1, y1, x2, y2, w in segs:
        part = pcbnew.SHAPE_POLY_SET()
        seg = pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)),
                                   pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)),
                                   pcbnew.FromMM(w + 2.0 * over))
        seg.TransformToPolygon(part, pcbnew.FromMM(0.001), pcbnew.ERROR_INSIDE)
        poly.BooleanAdd(part)
    poly.Simplify()
    rings = []
    for i in range(poly.OutlineCount()):
        ol = poly.Outline(i)
        rings.append([(pcbnew.ToMM(ol.CPoint(k).x), pcbnew.ToMM(ol.CPoint(k).y)) for k in range(ol.PointCount())])
    return rings


def rail_priorities(existing, nets_desc):
    """{net: priority} above every existing zone priority, distinct; nets_desc is ordered by descending current, so the
    largest current gets the highest priority (KiCad: the higher priority wins an overlap)."""
    base = max(list(existing) or [0])
    n = len(nets_desc)
    return {net: base + n - i for i, net in enumerate(nets_desc)}


def _spine_segments(b, nc, L):
    """[(x1, y1, x2, y2, width) mm] of net code `nc` on copper layer `L` (plain tracks only)."""
    import pcbnew
    out = []
    for t in b.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == nc and t.GetLayer() == L:
            s, e = t.GetStart(), t.GetEnd()
            out.append((pcbnew.ToMM(s.x), pcbnew.ToMM(s.y), pcbnew.ToMM(e.x), pcbnew.ToMM(e.y), pcbnew.ToMM(t.GetWidth())))
    return out


def rail_inner_mm(board_path, rails):
    """{net: mm} of track length on inner copper layers for each rail (spines that do not reach the outer pour)."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    inner = [x for x in b.GetEnabledLayers().CuStack() if x not in (pcbnew.F_Cu, pcbnew.B_Cu)]
    out = {}
    for net in rails:
        ni = b.FindNet(net)
        if ni is None:
            continue
        mm = sum(pcbnew.ToMM(t.GetLength()) for t in b.GetTracks()
                 if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == ni.GetNetCode() and t.GetLayer() in inner)
        if mm > 0:
            out[net] = round(mm, 2)
    return out


def _strip_rail_zones(src, dst, nets):
    """Fresh-process worker of pour_rails (board.Remove decays pcbnew's bindings: Zones() then yields raw SwigPyObjects, the W15 close
    `railpour` rung died on it): save `src` as `dst` without the `fluxplace-rail:<net>` zones of `nets`.  -> zones removed."""
    import pcbnew
    b = pcbnew.LoadBoard(src)
    names = {RAIL_ZONE + n for n in nets}
    old = [z for z in b.Zones() if (z.GetZoneName() or "") in names]
    for z in old:
        b.Remove(z)
    pcbnew.SaveBoard(dst, b)
    return len(old)


def pour_rails(board_path, cons, out, layers=None, nets=None):
    """D71cj D: a [power] rail that is routed as a spine (no zone of its net on the board) gets a corridor pour: per outer
    copper layer that carries its spine, one zone named `fluxplace-rail:<net>` whose outline is the spine grown by
    `[route] power_pour_over_mm`, pad connection solid, priority above every existing zone (GND/plane zones keep theirs).
    Re-run: zones with the same name are removed first (in a fresh process, on a scratch copy). Geometry is pcbnew's own (PHASE3C E).
    `nets`: only these rails (the close rung passes its gap nets; None = every pour rail).
    Guard: DRC shorts, opens and errors must all be no higher after the pour, else nothing is written (`dropped`).
    -> {added: [net/layer], kept, skipped: {net: reason}, dropped, dropped_reason, before/after measures}"""
    import pcbnew
    from . import drcguard as DG
    from . import gaps as GP
    rec = {"added": [], "kept": [], "skipped": {}, "dropped": False, "dropped_reason": None, "skip_all": None,
           "shorts_before": None, "shorts_after": None, "before": None, "after": None}
    pw = (cons or {}).get("power") or {}
    rails = sorted((n for n, r in plan(cons, layers=layers).items() if r and r["pour"] and r.get("track_mm")
                    and (nets is None or n in nets)),
                   key=lambda n: -(current_a(pw.get(n)) or 0.0))
    if not rails:
        return rec
    over = float(((cons or {}).get("route") or {}).get("power_pour_over_mm", POUR_OVER_MM))
    load = board_path
    b = pcbnew.LoadBoard(board_path)
    if any((z.GetZoneName() or "")[len(RAIL_ZONE):] in rails for z in b.Zones() if (z.GetZoneName() or "").startswith(RAIL_ZONE)):
        from .freshproc import in_fresh_process
        import shutil
        load = out + ".strip.kicad_pcb"
        pro = os.path.splitext(board_path)[0] + ".kicad_pro"
        if os.path.exists(pro):                              # the copy is loaded with the board's own project rules
            shutil.copy(pro, os.path.splitext(load)[0] + ".kicad_pro")
        in_fresh_process("fluxplace.power", "_strip_rail_zones", board_path, load, rails)
        b = pcbnew.LoadBoard(load)
    try:
        zoned = GP._zone_nets(load)
    finally:
        if load != board_path:
            for f in (load, os.path.splitext(load)[0] + ".kicad_pro", os.path.splitext(load)[0] + ".kicad_prl"):
                if os.path.exists(f):
                    os.remove(f)
    others = [z for z in b.Zones() if z.GetNetname() not in rails]
    prio = rail_priorities([z.GetAssignedPriority() for z in b.Zones()], [n for n in rails if n not in zoned])
    ref = others[0] if others else None
    work = []
    for net in rails:
        if net in zoned:
            rec["skipped"][net] = "board already has a zone of this net"
            continue
        ni = b.FindNet(net)
        if ni is None:
            rec["skipped"][net] = "net not on board"
            continue
        for L in (pcbnew.F_Cu, pcbnew.B_Cu):
            segs = _spine_segments(b, ni.GetNetCode(), L)
            if not segs:
                continue
            rings = rail_outline(segs, over)
            if not rings:
                continue
            z = pcbnew.ZONE(b)
            z.SetLayer(L)
            z.SetNetCode(ni.GetNetCode())
            z.SetZoneName(f"{RAIL_ZONE}{net}")
            z.SetIsFilled(False)
            z.SetAssignedPriority(prio[net])
            z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
            if ref is not None:
                z.SetMinThickness(ref.GetMinThickness())
                z.SetLocalClearance(ref.GetLocalClearance())
                z.SetThermalReliefGap(ref.GetThermalReliefGap())
                z.SetThermalReliefSpokeWidth(ref.GetThermalReliefSpokeWidth())
            for ring in rings:                                # AddOutline copies the chain; SetOutline(local poly) left a
                ch = pcbnew.SHAPE_LINE_CHAIN()                # dangling pointer (segfault on the second zone, PHASE3C E)
                for x, y in ring:
                    ch.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
                ch.SetClosed(True)
                z.Outline().AddOutline(ch)
            work.append((f"{net}/{b.GetLayerName(L)}", z))
    rec["attempted"] = [lab for lab, _ in work]          # the zones built before the guard (kept or dropped)
    if not work:
        return rec
    from . import opengate as OG
    m = DG.measure(board_path)                           # the input file is untouched so far
    base = _guard_measure(m, DG)
    rec["before"] = base
    rec["shorts_before"] = base["shorts"]
    src, dst = os.path.splitext(board_path)[0] + ".kicad_pro", os.path.splitext(out)[0] + ".kicad_pro"
    if os.path.exists(src):                              # before the first refill
        import shutil
        shutil.copy(src, dst)

    def trial(z):                                        # zone added to the accepted board, unfilled save, helper refill
        b.Add(z)
        b.Save(out)                                      # unfilled; the zone fill runs in a fresh helper process (opengate.refill):
        OG.refill(out)                                   # in-process ZONE_FILLER segfaults after a second new zone (PHASE3C E)
        return _guard_measure(DG.measure(out), DG)

    def drop(z):                                         # back to the accepted set (kept zones only)
        b.Remove(z)
        b.Save(out)
        OG.refill(out)

    kept, dropped, cur = pour_guard(work, base, trial, drop)
    rec["kept"], rec["added"] = list(kept), list(kept)
    rec["after"] = cur
    rec["shorts_after"] = cur["shorts"]
    rec["zones"] = [{"zone": lab, "kept": True} for lab in kept] + [{"zone": lab, "kept": False, "reason": why}
                                                                  for lab, why in dropped]
    if dropped:
        rec.update(dropped=True, dropped_reason="; ".join(f"{lab}: {why}" for lab, why in dropped))
    if not kept and os.path.exists(out):
        os.remove(out)
    return rec


GUARD_KEYS = ("shorts", "errors", "track_opens")         # a zone is refused when any of these rises (zone islands are not here)


def _guard_measure(m, DG):
    """The pour guard's view of a DRC measure: track/via/pad opens only (zone islands are close()'s pourisland job)."""
    return {"opens": m["opens"], "shorts": m["shorts"], "errors": m["errors"],
            "track_opens": DG.open_split(m.get("drc") or {})["track_opens"]}


def pour_guard(items, base, trial, drop):
    """PHASE3E 2: rails poured ONE ZONE AT A TIME. items = [(label, zone)]; trial(zone) adds it to the accepted board
    and returns the guard measure; drop(zone) removes it again. A zone is kept only when no GUARD_KEY rose against
    the accepted state. -> (kept labels, [(label, reason)] dropped, measure of the final accepted board)"""
    cur, kept, dropped = base, [], []
    for label, z in items:
        m = trial(z)
        worse = [k for k in GUARD_KEYS if m[k] > cur[k]]
        if worse:
            drop(z)
            dropped.append((label, ", ".join(f"{k} {cur[k]} -> {m[k]}" for k in worse)))
        else:
            kept.append(label)
            cur = m
    return kept, dropped, cur


# ---- PHASE3G G4: rail rungs (widen in place, pour, vias) ---------------------------------------------------------
WIDEN_FRACS = (1.0, 0.75, 0.5)       # widths tried as fractions of the layer's need, widest first (never below the current width)
VIA_SITE_TRIES = 12                  # via lay attempts per rail (each one is a probe + guard run)


def _stack_oz(board):
    from . import review as RV
    try:
        return oz_from_stackup(RV.parse_stackup_text(open(board, errors="replace").read()))
    except (OSError, ValueError):
        return None


def _rails_on(board, cons, nets=None):
    """the [power] rails of `board` with a current figure, sized exactly as check() sizes them -> {net: plan row}."""
    from . import gaps as GP
    layers = len(GP.copper_layers(board))
    return {n: r for n, r in plan(cons, layers=layers, oz=_stack_oz(board)).items()
            if r and r.get("track_mm") is not None and (nets is None or n in nets)}


def _need_on(r, layer):
    """the width check() asks on this layer: outer / inner weight, else the rail's track width."""
    need = r["width_outer"] if layer in ("F.Cu", "B.Cu") else r["width_inner"]
    return r["track_mm"] if need is None else need


def _pad_min_at(pads, net, x, y):
    """the narrowest dimension (mm) of a same-net pad whose box holds (x, y), or None. `pads`: [(net, box, min dim)]."""
    import pcbnew
    pt = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    hit = [m for n, box, m in pads if n == net and box.Contains(pt)]
    return min(hit) if hit else None


def widen(board, cons, out, nets=None, protect=()):
    """PHASE3G G4.1: every unlocked rail track narrower than the need of its layer (the numbers check() uses) is widened to the
    widest of need, 0.75 need, 0.5 need that the clearance probe accepts (handroute.probe_prims, the segment as one prim,
    the segment's own copper skipped; the probe model is built once, so widenings do not see each other: the close guard
    judges the result). A segment that ends in a pad narrower than its new width is split max(W, 0.5 mm) from that pad and
    only the far part is widened. Rails that check() exempts (pour rail with a same-net zone) are left alone.
    PHASE3G G1b d: a LOCKED rail track is a candidate too, unless its net is in `protect` (drcguard.protected_nets: pair legs,
    RF). Its width changes in place and it stays locked; a necked split part inherits the lock.
    Writes `out` only when a track changed. -> {"widened_mm", "left_mm", "by_net": {net: {"widened_mm", "left_mm"}},
    "skipped": {"locked_mm", "pour_zone_mm", "by_net": {net: {"locked_mm", "pour_zone_mm"}}}} (skipped = narrow copper
    left alone: locked copper of a protected net, copper inside a same-net pour zone; mm of track length)."""
    import pcbnew
    from . import gaps as GP, handroute as HR
    rec = {"widened_mm": 0.0, "left_mm": 0.0, "by_net": {}, "skipped": {"locked_mm": 0.0, "pour_zone_mm": 0.0, "by_net": {}}}
    protect = set(protect or ())
    rails = _rails_on(board, cons, nets)
    if not rails:
        return rec
    zone_nets = GP._zone_nets(board)
    b = pcbnew.LoadBoard(board)
    M = HR._model(board)                                 # the board's own rules (constraints: a file path, not used here)
    pads = [(p.GetNetname(), p.GetBoundingBox(), min(pcbnew.ToMM(p.GetSize().x), pcbnew.ToMM(p.GetSize().y)))
            for p in b.GetPads() if p.GetNetname()]
    tot = lambda net: rec["by_net"].setdefault(net, {"widened_mm": 0.0, "left_mm": 0.0})

    def skip(net, kind, mm):
        rec["skipped"][kind] += mm
        rec["skipped"]["by_net"].setdefault(net, {"locked_mm": 0.0, "pour_zone_mm": 0.0})[kind] += mm
    changed = 0
    for t in list(b.GetTracks()):
        net = t.GetNetname()
        r = rails.get(net)
        if t.GetClass() != "PCB_TRACK" or r is None:
            continue
        lay = b.GetLayerName(t.GetLayer())
        need = _need_on(r, lay)
        cur = pcbnew.ToMM(t.GetWidth())
        if cur >= need - 1e-6:
            continue
        s, e = t.GetStart(), t.GetEnd()
        a, z = (pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)), (pcbnew.ToMM(e.x), pcbnew.ToMM(e.y))
        L = math.hypot(z[0] - a[0], z[1] - a[1])
        if r["pour"] and net in zone_nets:
            skip(net, "pour_zone_mm", L)
            continue
        if t.IsLocked() and net in protect:
            skip(net, "locked_mm", L)
            continue
        near = next((k for k, pt in enumerate((a, z)) if (_pad_min_at(pads, net, *pt) or need) < need - 1e-6), None)
        step = max(need, 0.5)
        split = None
        if near is not None:                             # necked: the near part (step long) keeps the old width
            if L < step + 0.05:
                tot(net)["left_mm"] += L
                rec["left_mm"] += L
                continue
            near_pt, far = (a, z) if near == 0 else (z, a)
            u = ((far[0] - near_pt[0]) / L, (far[1] - near_pt[1]) / L)
            split = (near_pt[0] + u[0] * step, near_pt[1] + u[1] * step)
            lo, hi = split, far
        else:
            lo, hi = a, z
        chosen = None
        for f in WIDEN_FRACS:
            W = round(need * f, 4)
            if W <= cur + 1e-6:
                break
            if HR.probe_prims(M, net, [("seg", lay, lo, hi)], width=W, skip_ids=(t.m_Uuid.AsString(),)).get("ok"):
                chosen = W
                break
        if chosen is None:
            tot(net)["left_mm"] += L
            rec["left_mm"] += L
            continue
        wide_mm = math.hypot(hi[0] - lo[0], hi[1] - lo[1])
        if split is not None:
            left_mm = step
            nt = pcbnew.PCB_TRACK(b)                       # the near part: its own track at the old width
            nt.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(near_pt[0]), pcbnew.FromMM(near_pt[1])))
            nt.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(split[0]), pcbnew.FromMM(split[1])))
            nt.SetWidth(pcbnew.FromMM(cur))
            nt.SetLayer(t.GetLayer())
            nt.SetNetCode(t.GetNetCode())
            nt.SetLocked(t.IsLocked())                     # the necked split inherits the lock
            b.Add(nt)
            sp = pcbnew.VECTOR2I(pcbnew.FromMM(split[0]), pcbnew.FromMM(split[1]))
            t.SetStart(sp) if near == 0 else t.SetEnd(sp)
            tot(net)["left_mm"] += left_mm
            rec["left_mm"] += left_mm
        t.SetWidth(pcbnew.FromMM(chosen))
        tot(net)["widened_mm"] += wide_mm
        rec["widened_mm"] += wide_mm
        changed += 1
    for n in list(rec["by_net"]):
        rec["by_net"][n] = {k: round(v, 3) for k, v in rec["by_net"][n].items()}
    sk = rec["skipped"]
    sk["locked_mm"], sk["pour_zone_mm"] = round(sk["locked_mm"], 3), round(sk["pour_zone_mm"], 3)
    for n in list(sk["by_net"]):
        sk["by_net"][n] = {k: round(v, 3) for k, v in sk["by_net"][n].items()}
    rec["widened_mm"], rec["left_mm"] = round(rec["widened_mm"], 3), round(rec["left_mm"], 3)
    if changed:
        pcbnew.SaveBoard(out, b)
        GP._sidecars(board, out)
    return rec


def add_vias(board, cons, out, nets=None, cons_path=None):
    """PHASE3G G4.2: a rail short of its parallel vias (check()'s vias_needed) gets the missing same-net vias at free sites
    beside its existing vias (handroute.window at each via, best margin first). Each via is one handroute.lay of
    'via x,y' (its own probe and guard; a refused site is skipped). Writes `out` only when a via was added.
    -> {"missing": {net: n}, "added": {net: n}, "refused": {net: n}}"""
    from . import gaps as GP, handroute as HR, routediag as RD
    rec = {"missing": {}, "added": {}, "refused": {}}
    rails = _rails_on(board, cons, nets)
    B = RD.load(board)
    cur, tmps, k = board, [], 0
    try:
        for net in sorted(rails):
            need_n = rails[net].get("vias_needed")
            have = [(v["x"], v["y"]) for v in B["vias"] if v["net"] == net]
            if not need_n or not have or len(have) >= need_n:
                continue
            missing = need_n - len(have)
            rec["missing"][net] = missing
            sites = []
            for x, y in have:                              # sites beside each existing via, best margin first
                W = HR.window(cur, at=(x, y), size=(4, 4), net=net, constraints=cons_path)
                sites += [(s[0], s[1]) for s in ((W.get("via_sites") or {}).get("sites") or [])]
            seen, done, tries = set(), 0, 0
            for x, y in sites:
                if done >= missing or tries >= VIA_SITE_TRIES:
                    break
                key = (round(x, 2), round(y, 2))
                if key in seen or any(math.hypot(x - hx, y - hy) < 0.3 for hx, hy in seen):
                    continue
                seen.add(key)
                tries += 1
                k += 1
                nxt = f"{out}.via{k}.kicad_pcb"
                tmps.append(nxt)
                res = HR.lay(cur, net, f"via {x:.3f},{y:.3f}", out=nxt, constraints=cons_path) or {}
                if res.get("accepted") and os.path.exists(nxt):
                    cur = nxt
                    done += 1
                else:
                    rec["refused"][net] = rec["refused"].get(net, 0) + 1
            if done:
                rec["added"][net] = done
        if cur != board:                                   # the last accepted board is the output
            os.replace(cur, out)
            tmps.remove(cur)
            GP._sidecars(board, out)
    finally:
        for q in tmps:
            GP._rm(q)
    return rec
