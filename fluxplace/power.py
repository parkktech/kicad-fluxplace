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
    `[route] power_spine_mm` overrides SPINE_MM. `exclude`: plane nets the caller knows (zones on the board), not rails."""
    spine = float(((cons or {}).get("route") or {}).get("power_spine_mm", SPINE_MM))
    out = {}
    for n, r in plan(cons, layers=layers).items():
        if r is None or r.get("track_mm") is None or n in exclude:
            continue
        need = float(r["track_mm"])
        w = min(spine, need) if r["pour"] else need
        out[n] = round(max(w, float(track_mm)), 2)
    return out


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
            out.append({"level": "FAIL", "code": "POWER_WIDTH", "refs": [net],
                        "msg": f"{net}: {tot:.1f} mm of track narrower than the {r['current_a']:g} A copper needs "
                               f"({r['width_source']}, {r['oz_outer']:g}/{r['oz_inner']:g} oz): {det}"})
        nv = sum(1 for v in facts.get("vias") or [] if v["net"] == net)
        nvn = r.get("vias_needed")
        if nvn and 0 < nv < nvn:
            out.append({"level": "FAIL", "code": "POWER_VIAS", "refs": [net],
                        "msg": f"{net}: {nv} via(s) carry {r['current_a']:g} A, needs {nvn} x {r['via_drill_mm']:g} mm "
                               f"({r['via_amps']:g} A each, dT {r['rise_c']:g} C)"})
    unk = unknown_rails(cons, facts.get("net_pads"))
    if unk:
        out.append({"level": "WARN", "code": "POWER_CURRENT_UNKNOWN", "refs": [n for n, _v, _w in unk],
                    "msg": f"{len(unk)} rail(s) with no current figure (fill [power.NET] max_current_ma from the regulator "
                           f"rating / load datasheets): " + ", ".join(f"{n}" + (f" {v:g}V" if v else "") for n, v, _w in unk[:24])
                           + (f" (+{len(unk) - 24})" if len(unk) > 24 else "")})
    return out


def pour_rails(board_path, cons, out, layers=None):
    """D71cj D: a [power] rail with `pour = true` (or a required width over the pour limit) gets a copper pour after routing:
    one zone per outer copper layer that carries its tracks; outline = bounding box of the rail's tracks and pads on that
    layer grown by `[route] power_pour_over_mm`; priority 1, thermal relief; filled with planes.refill and saved to `out`.
    Guard: DRC shorts rise -> nothing is written (`dropped`). -> {added: [net/layer], dropped, shorts_before, shorts_after}"""
    import pcbnew
    from . import planes as PL
    from . import drcguard as DG
    rec = {"added": [], "dropped": False, "shorts_before": None, "shorts_after": None}
    rails = sorted(n for n, r in plan(cons, layers=layers).items() if r and r["pour"] and r.get("track_mm"))
    if not rails:
        return rec
    over = float(((cons or {}).get("route") or {}).get("power_pour_over_mm", POUR_OVER_MM))
    b = pcbnew.LoadBoard(board_path)
    rec["shorts_before"] = DG.measure(board_path)["shorts"]
    zones = []
    for net in rails:
        ni = b.FindNet(net)
        if ni is None:
            continue
        nc = ni.GetNetCode()
        trk = [t for t in b.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == nc]
        pads = [p for fp in b.GetFootprints() for p in fp.Pads() if p.GetNetCode() == nc]
        for L in (pcbnew.F_Cu, pcbnew.B_Cu):
            on_l = [t for t in trk if t.GetLayer() == L]
            if not on_l:
                continue
            boxes = [t.GetBoundingBox() for t in on_l] + [p.GetBoundingBox() for p in pads if p.IsOnLayer(L)]
            x1 = min(bx.GetLeft() for bx in boxes) - pcbnew.FromMM(over)
            y1 = min(bx.GetTop() for bx in boxes) - pcbnew.FromMM(over)
            x2 = max(bx.GetRight() for bx in boxes) + pcbnew.FromMM(over)
            y2 = max(bx.GetBottom() for bx in boxes) + pcbnew.FromMM(over)
            z = pcbnew.ZONE(b)
            z.SetLayer(L)
            z.SetNetCode(nc)
            z.SetIsFilled(False)
            z.SetAssignedPriority(1)
            z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
            poly = pcbnew.SHAPE_POLY_SET()
            poly.NewOutline()
            for (x, y) in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
                poly.Append(int(x), int(y))
            z.SetOutline(poly)
            b.Add(z)
            zones.append((z, poly))
            rec["added"].append(f"{net}/{b.GetLayerName(L)}")
    if not zones:
        return rec
    PL.refill(b)
    b.Save(out)
    rec["shorts_after"] = DG.measure(out)["shorts"]
    if rec["shorts_after"] > rec["shorts_before"]:
        rec.update(dropped=True, added=[])
        if os.path.exists(out):
            os.remove(out)
    return rec

