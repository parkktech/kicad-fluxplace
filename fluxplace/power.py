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
# D71da (owner, 2026-11-23): a narrow run of a power net is exempt from POWER_WIDTH only if proven for the load:
NECK_MAX_MM = 2.0               # (1) connected run length <= this (judged per run, not summed per net+layer)
NECK_JOIN_MM = 0.02             # segment endpoints closer than this are one junction
CU_RHO_OHM_M = 1.72e-8          # (2) copper resistivity at 20 C
CU_K_W_MK = 390.0               #     copper thermal conductivity
CU_UM_PER_OZ = 35.0             #     copper thickness per oz (D71da)
# (2) heat: dT = I^2 rho L^2 / (8 k A^2) (1-D conduction, both ends held by full-width copper) <= the rail's rise_c;
# (3) the net has a [[dcdrop]] row and channel's DC-drop metric (routed copper incl. the neck-downs) passes it.
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


def neck_heat_c(current_a, length_mm, width_mm, oz):
    """D71da: temperature rise (C) of the hottest point of a constriction, dT = I^2 rho L^2 / (8 k A^2)."""
    a = (width_mm * 1e-3) * (CU_UM_PER_OZ * oz * 1e-6)
    return current_a ** 2 * CU_RHO_OHM_M * (length_mm * 1e-3) ** 2 / (8 * CU_K_W_MK * a * a)


def neck_width_mm(current_a, length_mm, oz, rise_c=DEFAULT_RISE_C):
    """D71da test 2 inverted: the narrowest width (mm) for which a run `length_mm` long holds dT <= rise_c at `current_a`
    (dT = I^2 rho L^2 / (8 k A^2) -> w = I L sqrt(rho / (8 k dT)) / t)."""
    t = CU_UM_PER_OZ * oz * 1e-6
    return current_a * (length_mm * 1e-3) * math.sqrt(CU_RHO_OHM_M / (8 * CU_K_W_MK * rise_c)) / t * 1e3


def neck_drop_mv(current_a, length_mm, width_mm, oz):
    """DC drop (mV) across a neck-down at full rail current: I rho L / (w t)."""
    return current_a * CU_RHO_OHM_M * (length_mm * 1e-3) / ((width_mm * 1e-3) * (CU_UM_PER_OZ * oz * 1e-6)) * 1e3


LADDER_MM = (1.4, 1.0, 0.6)     # W14 measured (ONE-SHOT-LEDGER 2026-11-19 "rails first"): widest first IPC, 1.4, 1.0, 0.6
LADDER_INNER_MAX_MM = 1.0       # inner layers only at the small widths (W14: a wide inner spine eats pair capacity)


def ladder_widths(cons, net, top_mm, track_mm=0.15):
    """Rung widths (mm, widest first) the rails step tries for one pour rail: its routed width `top_mm` (IPC outer), then
    `[route] power_ladder_mm` (default 1.4, 1.0, 0.6) below it, never under the signal width `track_mm`."""
    rt = (cons or {}).get("route") or {}
    rungs = [float(x) for x in rt.get("power_ladder_mm", LADDER_MM)]
    out = [round(float(top_mm), 2)]
    for w in sorted(rungs, reverse=True):
        if w < out[-1] - 1e-6 and w >= float(track_mm) - 1e-6:
            out.append(round(w, 2))
    return out


def ladder_layers(cons, sig, width_mm, fallback=False):
    """Layers of a rung: outer copper for the rail's own (first) rung and for any width above LADDER_INNER_MAX_MM; every
    signal layer for a fallback rung at or below it."""
    if not fallback or width_mm > LADDER_INNER_MAX_MM + 1e-9:
        return rail_layers(cons, sig)
    return list(sig)


def narrow_runs(segs, vias=()):
    """Group narrow segments [{layer, x1, y1, x2, y2, len, width, need}] into connected runs (shared endpoints within
    NECK_JOIN_MM on a layer; narrow ends that meet at one of the net's `vias` [{x, y, dia_mm}] are one run across the
    layers: a via barrel is not full-width copper, so it does not end a constriction). A segment without endpoints is its
    own run. -> [{layer ("A+B" when a via joins layers), layers, len, min_w, need, n}]."""
    par = list(range(len(segs)))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    q = NECK_JOIN_MM
    pts = {}
    for i, g in enumerate(segs):
        if "x1" in g:
            for x, y in ((g["x1"], g["y1"]), (g["x2"], g["y2"])):
                pts.setdefault((g["layer"], round(x / q), round(y / q)), []).append((x, y, i))
    for (lyr, cx, cy), lst in pts.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for x, y, i in lst:
                    for x2, y2, j in pts.get((lyr, cx + dx, cy + dy), ()):
                        if i != j and abs(x - x2) <= q and abs(y - y2) <= q:
                            par[find(i)] = find(j)
    for v in vias or ():
        rr = float(v.get("dia_mm") or 0.0) / 2 + q
        at = [i for lst in pts.values() for x, y, i in lst if math.hypot(x - v["x"], y - v["y"]) <= rr]
        for i in at[1:]:
            par[find(i)] = find(at[0])
    runs = {}
    for i, g in enumerate(segs):
        d = runs.setdefault(find(i), {"layers": set(), "len": 0.0, "min_w": g["width"], "need": g["need"], "n": 0, "members": []})
        d["members"].append(i)
        if "x1" in g and "at" not in d:
            d["at"] = (round(g["x1"], 2), round(g["y1"], 2))
        d["layers"].add(g["layer"])
        d["len"] += g.get("len", 0.0)
        d["min_w"] = min(d["min_w"], g["width"])
        d["need"] = max(d["need"], g["need"])
        d["n"] += 1
    for d in runs.values():
        d["layers"] = sorted(d["layers"])
        d["layer"] = "+".join(d["layers"])
    return sorted(runs.values(), key=lambda d: (d["layer"], -d["len"]))


def dcdrop_status(board_path, cons):
    """{net: {ok, mv, max, why}} from channel's [[dcdrop]] rows (the one DC-drop computation); {} when there are none or the
    board cannot be measured (then nothing is exempt)."""
    if not ((cons or {}).get("dcdrop") and isinstance(board_path, str) and os.path.exists(board_path)):
        return {}
    try:
        from . import channel as CH, kicad_io as IO
        board = IO.load(board_path)
        rows, _p, _s = CH.stack_rows(cons, board.GetFileName())
        out = {}
        for r in CH.dcdrop_rows(board, cons, rows):
            m = (r.get("metrics") or [None])[0]
            ok = r["verdict"] in ("ok", "over-target-pass") and m is not None
            st = {"ok": ok, "mv": m["value"] if m else None, "max": m["max"] if m else None,
                  "why": "" if ok else (r.get("why") or "drop not computed")}
            if r["name"] in out and not out[r["name"]]["ok"]:
                continue
            out[r["name"]] = st
        return out
    except Exception as e:                       # noqa: BLE001 - an unmeasurable drop exempts nothing
        return {"__error__": {"ok": False, "mv": None, "max": None, "why": f"{type(e).__name__}: {e}"}}


def _net_drop_fail(net, dc, has_dcdrop):
    """D71da test 3 is per net: "" when the [[dcdrop]] budget passes, else why not."""
    if not has_dcdrop:
        return f"no [[dcdrop]] for {net} (add one from the load/regulator datasheets)"
    st = dc.get(net)
    if st and st["ok"]:
        return ""
    if st and st["mv"] is not None:
        return f"drop {st['mv']:.0f} > {st['max']:g} mV"
    return f"drop not computed ({(st or dc.get('__error__') or {}).get('why') or 'no result'})"


def _run_oz(run, r, outer, stackup):
    """Copper weight for the heat test: the thinnest layer of the run, from the stackup row's own thickness (D71da: copper
    weight from the stackup; the rail's oz is snapped to 0.5 oz steps and can round a thin foil up), else the rail's oz."""
    t = {x.get("name"): float(x["thickness"]) for x in stackup or [] if x.get("type") == "copper" and x.get("thickness")}
    return min(t[l] * 1e3 / CU_UM_PER_OZ if t.get(l) else (r["oz_outer"] if l in outer else r["oz_inner"])
               for l in run.get("layers") or [run["layer"]])


def _neck_tests(run, r, outer, stackup=None):
    """D71da tests 1-2 for one run -> list of failed-test strings (empty = passes)."""
    fails = []
    if run["len"] > NECK_MAX_MM + 1e-9:
        fails.append(f"length {run['len']:.2f} mm > {NECK_MAX_MM:g}")
    oz = _run_oz(run, r, outer, stackup)
    rise = r.get("rise_c") or DEFAULT_RISE_C
    dt = neck_heat_c(r["current_a"], run["len"], run["min_w"], oz)
    if dt > rise + 1e-9:
        fails.append(f"dT {dt:.1f} C > {rise:g}")
    return fails


RTI_RULE = ("laminate_rti", "fr4")             # k15: limits.json key of the laminate's UL RTI (continuous use temperature)


def hard_rise_c(cons):
    """k15 (owner: realistic limits): the HARD trace temperature rise = the laminate's sourced UL RTI - [env] temp_max_c, with its
    citation -> (rise C, cite) | None (no sourced RTI or no ambient maximum: nothing is relaxed, the target rise stays the FAIL line)."""
    from . import limits as LM
    e = LM.get(*RTI_RULE)
    tmax = ((cons or {}).get("env") or {}).get("temp_max_c")
    if not LM.sourced(e) or tmax is None:
        return None
    rise = float(e["hard"]) - float(tmax)
    return (rise, f"{e['hard']:g} C RTI - {float(tmax):g} C ambient; {LM.cite(e)}") if rise > 0 else None


def _cont_frac(cons, net):
    """continuous_ma / max_current_ma of the rail (<= 1; 1 when no continuous figure: the rated current is taken as continuous)."""
    p = ((cons or {}).get("power") or {}).get(net) or {}
    try:
        return min(1.0, float(p["continuous_ma"]) / float(p["max_current_ma"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 1.0


def _hard_pass(run, segs, r, outer, stackup, amps_c, rise_h):
    """The run holds the continuous current at the hard rise: every member is wide enough (IPC-2221 at rise_h), or the members that
    are not, taken as ONE neck (their summed length at the narrowest width: conservative, the heat grows with length squared),
    stay within rise_h."""
    def need(l):
        return ipc2221_width_mm(amps_c, rise_h, r["oz_outer"], False) if l in outer else ipc2221_width_mm(amps_c, rise_h, r["oz_inner"], True)
    thin = [segs[i] for i in run["members"] if segs[i]["width"] < need(segs[i]["layer"]) - 1e-6]
    if not thin or run.get("cred_w", 0.0) >= max(need(l) for l in run["layers"]) - 1e-6:
        return True
    ln = sum(math.hypot(g["x2"] - g["x1"], g["y2"] - g["y1"]) for g in thin)
    return neck_heat_c(amps_c, ln, min(g["width"] for g in thin), _run_oz(run, r, outer, stackup)) <= rise_h + 1e-9


def _is_decap(ref):
    return str(ref).upper().startswith("C")


def _board_ctx(board_path, rails):
    """pcbnew view for the per-run physics: {b, pads: {net: [(ref, num, box, value, pin function)]}, copper: {net: (segs, vias)}}."""
    import pcbnew
    if not (isinstance(board_path, str) and os.path.exists(board_path)):
        return None
    b = pcbnew.LoadBoard(board_path)
    pads, copper = {}, {}
    for p in b.GetPads():
        if p.GetNetname() in rails:
            bb = p.GetBoundingBox()
            bb.Inflate(pcbnew.FromMM(0.1))
            fp = p.GetParentFootprint()
            cu = {b.GetLayerName(l) for l in p.GetLayerSet().CuStack()}
            pads.setdefault(p.GetNetname(), []).append((fp.GetReference(), p.GetNumber(), bb, fp.GetValue(),
                                                        (p.GetPinFunction() or p.GetName() or ""),
                                                        (pcbnew.ToMM(p.GetPosition().x), pcbnew.ToMM(p.GetPosition().y)), cu,
                                                        (pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY()),
                                                         pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))))
    for t in b.GetTracks():
        n = t.GetNetname()
        if n not in rails:
            continue
        segs, vias = copper.setdefault(n, ([], []))
        if t.GetClass() == "PCB_VIA":
            vias.append((pcbnew.ToMM(t.GetPosition().x), pcbnew.ToMM(t.GetPosition().y), pcbnew.ToMM(t.GetWidth()) / 2))
        elif t.GetClass() == "PCB_TRACK":
            segs.append((b.GetLayerName(t.GetLayer()), pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetStart().y),
                         pcbnew.ToMM(t.GetEnd().x), pcbnew.ToMM(t.GetEnd().y), pcbnew.ToMM(t.GetWidth())))
    return {"b": b, "pads": pads, "copper": copper}


def parse_ohms(value):
    """'10k' -> 10000.0, '4k7' -> 4700.0, '0R' -> 0.0, '220' -> 220.0, '1M' -> 1e6; None when the Value is not a resistance."""
    import re
    v = str(value or "").strip().replace("Ω", "R").replace("ohm", "R").replace(" ", "")
    m = re.match(r"^(\d+)([kKmMrR])(\d+)$", v)              # 4k7, 2R2
    if m:
        v = f"{m.group(1)}.{m.group(3)}{m.group(2)}"
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmMrR]?)", v)
    if not m:
        return None
    return float(m.group(1)) * {"": 1.0, "r": 1.0, "k": 1e3, "m": 1e6}[m.group(2).lower()]


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def end_kind(ctx, net, run, segs):
    """What a run with no end pad is: "dangling stub" (an end touches nothing: no pad, via, other same-net copper or zone fill:
    nothing downstream, 0 A), or "via/junction link" (every end lands on a via, a wider same-net track or a pour: a link
    inside the rail tree, its current is not resolvable from the pads) -> (kind, free_ends)."""
    import pcbnew
    wide, vias = ctx["copper"].get(net, ([], []))
    mem = [segs[i] for i in run["members"]]
    deg = {}
    for g in mem:
        for k in (("x1", "y1"), ("x2", "y2")):
            key = (round(g[k[0]], 2), round(g[k[1]], 2))
            deg[key] = deg.get(key, 0) + 1
    free = 0
    for (x, y), d in deg.items():
        if d != 1:
            continue
        others = any(_seg_dist(x, y, x1, y1, x2, y2) <= w / 2 + 0.02 and w > max(g["width"] for g in mem) + 1e-6
                     for _l, x1, y1, x2, y2, w in wide)
        onvia = any(math.hypot(x - vx, y - vy) <= vr + 0.02 for vx, vy, vr in vias)
        inzone = False
        for z in ctx["b"].Zones():
            if z.GetNetname() == net:
                for lname in {g["layer"] for g in mem}:
                    if not z.IsOnLayer(ctx["b"].GetLayerID(lname)):
                        continue
                    fill = z.GetFilledPolysList(ctx["b"].GetLayerID(lname))
                    if fill is not None and fill.OutlineCount() and fill.Collide(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)), 0):
                        inzone = True
        if not (others or onvia or inzone):
            free += 1
    return ("dangling stub" if free else "via/junction link"), free


def zone_credit_mm(ctx, net, layer, seg, band_mm):
    """Conducting width (mm) across segment `seg` ({x1,y1,x2,y2,width}) from filled same-net zones on `layer`: the area of the
    zone fill inside the rectangle of the segment's length and `2 * band_mm` across, divided by the length (never below the
    track's own width). Measured on the board's stored fill; no fill, no credit."""
    import pcbnew
    b = ctx["b"]
    L = math.hypot(seg["x2"] - seg["x1"], seg["y2"] - seg["y1"])
    lid = b.GetLayerID(layer)
    if L < 0.01:
        return seg["width"]
    ux, uy = (seg["x2"] - seg["x1"]) / L, (seg["y2"] - seg["y1"]) / L
    nx, ny = -uy * band_mm, ux * band_mm
    rect = pcbnew.SHAPE_POLY_SET()
    rect.NewOutline()
    for x, y in ((seg["x1"] + nx, seg["y1"] + ny), (seg["x2"] + nx, seg["y2"] + ny), (seg["x2"] - nx, seg["y2"] - ny), (seg["x1"] - nx, seg["y1"] - ny)):
        rect.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    got = 0.0
    for z in b.Zones():
        if z.GetNetname() != net or not z.IsOnLayer(lid):
            continue
        fill = z.GetFilledPolysList(lid)
        if fill is None or fill.OutlineCount() == 0:
            continue
        inter = pcbnew.SHAPE_POLY_SET(rect)
        inter.BooleanIntersection(fill)
        got += inter.Area() / 1e12                 # nm^2 -> mm^2
    return max(seg["width"], got / L)


def _pad_amps(pad, npins, rows, src, tot, volts):
    """Current (A) one pad of a rail draws, and the source of the figure: -> (amps | None, how). None = unknown.
    Capacitor 0 A DC; MOSFET gate pin 0 A; resistor at most V_rail / R from its Value (0R = a series link: unknown); the
    rail's source part: its total / its pins on the net; a load named by a single-load [[dcdrop]] row (`to`): that row's
    current_a (per pin: / the part's pins on the net unless the row names the pin). Anything else: unknown."""
    ref, pin = pad[0], str(pad[4]).upper()
    if _is_decap(ref):
        return 0.0, "capacitor 0 A DC"
    if str(ref).upper().startswith("Q") and (pin in ("G", "GATE") or pin.startswith("G") and pin[1:].isdigit()):
        return 0.0, "MOSFET gate 0 A"
    if str(ref).upper().startswith("R") and ref != src:
        ohms = parse_ohms(pad[3])
        if ohms and volts:
            return volts / ohms, f"resistor V/R ({pad[3]})"
        return None, f"unknown ({ref} {pad[3] or 'no value'}: " + ("0R series link" if ohms == 0 else "no resistance value or rail voltage") + ")"
    if ref == src and tot:
        return tot / max(1, npins[ref]), "pin share (source)"
    row = next((d for d in rows if len(d["to"] if isinstance(d.get("to"), list) else [d.get("to")]) == 1
                and str((d["to"] if isinstance(d.get("to"), list) else [d["to"]])[0]).split(".")[0] == ref and d.get("current_a")), None)
    if row is None:
        return None, f"unknown ({ref} load current not in the budget)"
    to = str((row["to"] if isinstance(row["to"], list) else [row["to"]])[0])
    return float(row["current_a"]) / (1 if "." in to else max(1, npins[ref])), "budget (dcdrop)"


def _rail_sources(cons, net, tot):
    """-> (source part refs {ref}, the [power] source part, the net's [[dcdrop]] rows): the [power] source, the `from` part of its rows, and
    an inductor / ferrite bead (L, FB) whose single-load row carries the rail's whole current (the source's series element)."""
    pw = (cons or {}).get("power", {}).get(net) or {}
    drows = [d for d in (cons or {}).get("dcdrop", []) or [] if isinstance(d, dict) and d.get("net") == net]
    src = str(pw.get("source") or (drows[0].get("from") if drows else "") or "").split(".")[0]
    srcs = {src} - {""}
    for d in drows:
        if d.get("from"):
            srcs.add(str(d["from"]).split(".")[0])
        to = d["to"] if isinstance(d.get("to"), list) else [d.get("to")]
        ref0 = str(to[0]).split(".")[0].upper()
        if len(to) == 1 and tot and d.get("current_a") and float(d["current_a"]) >= tot * 0.999 and ref0.startswith(("L", "FB")):
            srcs.add(str(to[0]).split(".")[0])
    return srcs, src, drows


def unknown_loads(board, cons, layers=None):
    """Kickoff check: {rail net: [refs]} of the loads on each [power] rail with a current figure whose draw is not known
    (`_pad_amps` has no figure: no [[dcdrop]] row, not a capacitor / gate / resistor / the source). The route-stage run current
    falls back to the rail total for these, so a rail with any is sized by guesswork. {} when nothing is unknown."""
    rails = {n: r for n, r in plan(cons, layers=layers).items() if r and r.get("track_mm") is not None and r.get("current_a")}
    ctx = _board_ctx(board, set(rails)) if rails else None
    out = {}
    for net, r in rails.items():
        pads = (ctx or {}).get("pads", {}).get(net) or []
        srcs, src, drows = _rail_sources(cons, net, r["current_a"])
        npins = {}
        for pad in pads:
            npins[pad[0]] = npins.get(pad[0], 0) + 1
        unk = sorted({pad[0] for pad in pads if pad[0] not in srcs and _pad_amps(pad, npins, drows, src, r["current_a"], r.get("max_v") or 0.0)[0] is None})
        if unk:
            out[net] = unk
    return out


def _fill_contains(fill):
    import pcbnew
    return lambda x, y: fill.Collide(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)), 0)


def _fill_island(fill):
    """x, y -> index of the fill outline (island) holding the point, -1 when none: contacts in different islands are not joined."""
    import pcbnew

    def isl(x, y):
        pt = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
        for k in range(fill.OutlineCount()):
            if fill.COutline(k).PointInside(pt):
                return k
        return -1
    return isl


def rail_flows(ctx, cons, net, r, rows):
    """W40 tree-flow solve of one rail (powerflow.solve) on the copper `rows` (the net's facts track_geom rows) -> the per-row
    current list [{amps | None, unk, mode, loads}] and its context (source label), cached in ctx["flows"]; None when the rail has no
    pads or copper to solve. Source pads: the [power] source part, the `from` part of its [[dcdrop]] rows, and an inductor / ferrite bead (L, FB) whose
    single-load row carries the rail's whole current (the source's series element). Loads: `_pad_amps` per pad."""
    from . import powerflow as PF
    import pcbnew
    cache = ctx.setdefault("flows", {})
    if net in cache:
        return cache[net]
    cache[net] = None
    pads = (ctx or {}).get("pads", {}).get(net) or []
    if not rows or not pads or any(len(p) < 8 for p in pads) or any("x1" not in g for g in rows):
        return None
    tot = r["current_a"]
    srcs, src, drows = _rail_sources(cons, net, tot)
    volts = r.get("max_v") or 0.0
    npins = {}
    for pad in pads:
        npins[pad[0]] = npins.get(pad[0], 0) + 1
    gp = []
    for pad in pads:
        role = "source" if pad[0] in srcs else "load"
        a_ = None
        if role == "load":
            a_, _h = _pad_amps(pad, npins, drows, src, tot, volts)
        gp.append({"ref": pad[0], "x": pad[5][0], "y": pad[5][1], "layers": pad[6] or None, "box": pad[7], "role": role, "amps": a_})
    outer = {"F.Cu", "B.Cu"}
    segs = [dict(g, t_mm=0.035 * float(r.get("oz_outer" if g["layer"] in outer else "oz_inner") or 1.0)) for g in rows]
    vias = [(x, y, rad) for x, y, rad in ctx["copper"].get(net, ([], []))[1]]
    zones = []
    b = ctx["b"]
    for z in b.Zones():
        if z.GetNetname() != net or z.GetIsRuleArea():
            continue
        for lid in z.GetLayerSet().CuStack():           # every layer the zone is on, not only the layers the rail has tracks on
            fill = z.GetFilledPolysList(lid)
            if fill is None or not fill.OutlineCount():
                continue
            lname = b.GetLayerName(lid)
            oz = float(r.get("oz_outer" if lname in outer else "oz_inner") or 1.0)
            zones.append({"layer": lname, "t_mm": 0.035 * oz, "contains": _fill_contains(fill), "island": _fill_island(fill)})
    res = PF.solve(segs, vias, gp, zones, cap=tot or None)
    res["src"] = sorted(srcs)
    cache[net] = res
    return res


def run_current_flow(res, run, segs):
    """The current of one narrow run from the rail's tree-flow solve (`rail_flows`): the largest figure of its members, or the
    rail total with the unknown parts when any member's branch has an unknown load -> (amps, source, unknown refs) | None."""
    fl = [res["flow"][segs[i]["_row"]] for i in run["members"] if "_row" in segs[i]]
    if len(fl) != len(run["members"]):
        return None
    unk = sorted({u for f in fl for u in f["unk"]})
    src = "+".join(res["src"])
    if unk:
        return None, f"unknown ({', '.join(unk)}: load current not in the budget)", unk
    f = max(fl, key=lambda x: x["amps"])
    mode = {"tree": "tree flow", "mesh": "mesh bound", "mesh-solve": "mesh solve", "dangling": "dangling (no load behind it)",
            "unreached": "no source path (open copper)", "nosource": "no source pad on the copper"}.get(f["mode"], f["mode"])
    return f["amps"], f"{mode} {src}: {f['loads']} load(s) downstream" if f["mode"] in ("tree", "mesh", "mesh-solve") else mode, []


def run_current(ctx, cons, net, r, run, segs):
    """Current (A) one narrow run really carries, and where the figure comes from: -> (amps, source, unknown part refs).
    The run's end pads are the pads of the net whose box holds a segment end.  Per pad: capacitor 0 A DC; MOSFET gate pin (pin
    function G / GATE) 0 A; resistor at most V_rail / R from its Value (0R = a series link: unknown); the rail's source part total /
    its pins on the net; a load named by a single-load [[dcdrop]] row (`to`) that row's current_a (per pin: / the part's pins on the
    net unless the row names the pin).  Diodes / TVS and anything else without a figure: unknown -> the rail total, flagged.
    A run with no end pad is classified (`end_kind`): a dangling stub carries 0 A; a via/junction link stays unknown."""
    tot = r["current_a"]
    pads = (ctx or {}).get("pads", {}).get(net) or []
    ends = [(segs[i][k1], segs[i][k2]) for i in run["members"] for k1, k2 in (("x1", "y1"), ("x2", "y2"))]
    import pcbnew
    hit = {}
    for x, y in ends:
        pt = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
        for pad in pads:
            if pad[2].Contains(pt):
                hit[(pad[0], pad[1])] = pad
    if not hit:
        kind, _free = end_kind(ctx, net, run, segs)
        if kind == "dangling stub":
            return 0.0, "dangling stub (no load behind it)", []
        return tot, "unknown (no end pad: via/junction link inside the rail tree)", []
    pw = (cons or {}).get("power", {}).get(net) or {}
    rows = [d for d in (cons or {}).get("dcdrop", []) or [] if isinstance(d, dict) and d.get("net") == net]
    src = str(pw.get("source") or (rows[0].get("from") if rows else "") or "").split(".")[0]
    volts = r.get("max_v") or 0.0
    npins = {}
    for pad in pads:
        npins[pad[0]] = npins.get(pad[0], 0) + 1
    amps, how, unk = 0.0, set(), []
    for (ref, num), pad in hit.items():
        a_, h_ = _pad_amps(pad, npins, rows, src, tot, volts)
        how.add(h_)
        if a_ is None:
            unk.append(ref)
        else:
            amps += a_
    if unk:
        return tot, "+".join(sorted(how)), sorted(set(unk))
    return amps, "+".join(sorted(how)), []


def _overlaid(s, rows):
    """True when another same-net track on the layer is wider and lies over `s` (both ends on its copper): a wider piece laid
    over a locked narrow one (power.widen, W37) supersedes it, so the narrow one is not counted twice."""
    if "x1" not in s:
        return False
    for o in rows:
        if o is s or "x1" not in o or o["layer"] != s["layer"] or o["width"] <= s["width"] + 1e-6:
            continue
        if all(_seg_dist(x, y, o["x1"], o["y1"], o["x2"], o["y2"]) <= o["width"] / 2 + 0.02 for x, y in ((s["x1"], s["y1"]), (s["x2"], s["y2"]))):
            return True
    return False


def check(facts, cons, dc=None, board=None, detail=None):
    """Review findings (level, code, msg, refs): POWER_WIDTH, POWER_VIAS (FAIL), POWER_NECKDOWN_OK (INFO),
    POWER_CURRENT_UNKNOWN (WARN). `dc` = dcdrop_status(): without it no neck-down is exempt (D71da).
    POWER_WIDTH `value` = the shortfall in equivalent narrow mm: sum over failing runs of length x (1 - width / the run's own need), so
    any widening lowers it (W42). `detail` (a dict) receives {net: [{run, segs, need_run, cleared, amps}]} for every narrow run."""
    out = []
    dc = dc or {}
    ctx = None
    has_dc = {d.get("net") for d in (cons or {}).get("dcdrop", []) or [] if isinstance(d, dict)}
    layers = facts.get("copper_layers")
    inner = set(facts.get("plane_layers") or ())
    zone_nets = set(facts.get("zone_nets") or ())
    plan_ = plan(cons, layers=layers, oz=oz_from_stackup(facts.get("stackup")))
    outer = {"F.Cu", "B.Cu"}
    for net, r in sorted(plan_.items()):
        if r is None or r.get("track_mm") is None:
            continue
        narrow = []
        net_rows = [s for s in facts.get("track_geom") or [] if s["net"] == net and s.get("kind") == "track"]
        for ri, s in enumerate(net_rows):
            need = r["width_outer"] if s["layer"] in outer else r["width_inner"]
            if need is None:
                need = r["track_mm"]
            if s["width"] < need - 1e-6 and not _overlaid(s, net_rows):
                narrow.append(dict(s, need=need, _row=ri))
        if narrow:                                   # D71da has no pour carve-out: a zone on the net proves no track
            bad, ok, target = [], [], []
            hard = hard_rise_c(cons)
            dropf = _net_drop_fail(net, dc, net in has_dc)
            if board and ctx is None:
                ctx = _board_ctx(board, {n for n, rr in plan_.items() if rr and rr.get("track_mm") is not None}) or False
            for run in narrow_runs(narrow, [v for v in facts.get("vias") or [] if v.get("net") == net and "x" in v]):
                rr = r
                if ctx:                              # W34: the run's own current and conducting width (record both)
                    if net not in (ctx.get("flows") or {}):
                        try:
                            rail_flows(ctx, cons, net, r, net_rows)
                        except Exception:            # noqa: BLE001 - the solve is an improvement; the per-run physics stays
                            ctx.setdefault("flows", {})[net] = None
                    got = run_current_flow(ctx["flows"][net], run, narrow) if ctx.get("flows", {}).get(net) else None
                    if got is None:
                        amps, how, unk = run_current(ctx, cons, net, r, run, narrow)
                    else:
                        amps, how, unk = got
                        if amps is None:             # a load on the branch is unknown: the rail total, flagged
                            amps = r["current_a"]
                    cw = min(zone_credit_mm(ctx, net, narrow[i]["layer"], narrow[i], narrow[i]["need"]) for i in run["members"])
                    rise = r.get("rise_c") or DEFAULT_RISE_C
                    need_run = max((ipc2221_width_mm(amps, rise, r["oz_outer"], False) if l in outer else
                                    ipc2221_width_mm(amps, rise, r["oz_inner"], True)) for l in run["layers"]) if amps else 0.0
                    run.update(amps=amps, amps_src=how, unk_refs=unk, cred_w=round(cw, 3), min_w_track=run["min_w"], need_run=round(need_run, 3))
                    if detail is not None:
                        detail.setdefault(net, []).append({"run": run, "segs": [narrow[i] for i in run["members"]], "need_run": need_run,
                                                           "amps": amps, "cleared": cw >= need_run - 1e-6})
                    if cw >= need_run - 1e-6:        # wide enough for the current it really carries: not a constriction
                        run["cleared"] = True
                        continue
                    run["min_w"] = cw
                    rr = dict(r, current_a=amps)
                elif detail is not None:
                    detail.setdefault(net, []).append({"run": run, "segs": [narrow[i] for i in run["members"]], "need_run": run["need"],
                                                       "amps": r["current_a"], "cleared": False})
                nr_ = run.get("need_run") or run["need"]
                run["short_mm"] = run["len"] * max(0.0, 1.0 - run["min_w"] / nr_) if nr_ else 0.0
                f = _neck_tests(run, rr, outer, facts.get("stackup"))
                if (f and not dropf and run.get("amps") and hard is not None
                        and _hard_pass(run, narrow, r, outer, facts.get("stackup"), run["amps"] * _cont_frac(cons, net), hard[0])):
                    target.append((run, f))          # k15 (realistic limits): over the target only, inside the sourced hard limit
                    continue
                (bad if f or dropf else ok).append((run, f))
            if ok:
                out.append({"level": "INFO", "code": "POWER_NECKDOWN_OK", "refs": [net],
                            "value": sum(x["len"] for x, _f in ok),
                            "msg": f"{net}: {len(ok)} neck-down run(s), {sum(x['len'] for x, _f in ok):.2f} mm total, "
                                   f"proven for {r['current_a']:g} A (length <= {NECK_MAX_MM:g} mm, heat, [[dcdrop]]) (D71da)"})
            if target:
                cf = _cont_frac(cons, net)
                out.append({"level": "WARN", "code": "POWER_WIDTH_TARGET", "refs": [net], "value": round(sum(x["short_mm"] for x, _f in target), 3),
                            "msg": f"{net}: {len(target)} run(s), {sum(x['len'] for x, _f in target):.1f} mm, under the dT {r['rise_c']:g} C target "
                                   f"width but inside the hard limit: {cf * 100:.0f} % continuous current at dT {hard[0]:g} C ({hard[1]}): "
                                   + "; ".join(f"{x['layer']} {x['len']:.2f} mm at {x['min_w']:g} ({x['amps']:.2f} A target)" for x, _f in target[:4])})
            if bad:
                tot = sum(x["len"] for x, _f in bad)
                bad.sort(key=lambda t: -t[0]["len"])
                det = "; ".join(f"{x['layer']} {x['len']:.2f} mm at {x['min_w']:g} < {x.get('need_run', x['need']):.2f}"
                                + (f" ({x['amps']:.2f} A {x['amps_src']}, credited {x['cred_w']:g})" if "amps" in x else "")
                                + (": " + ", ".join(f) if f else "") for x, f in bad[:4])
                nunk = sum(1 for x, _f in bad if x.get("unk_refs") or str(x.get("amps_src", "")).startswith("unknown"))
                urefs = sorted({r_ for x, _f in bad for r_ in x.get("unk_refs") or []})
                det += (f"; {nunk} run(s) with current unknown (rail total used)" + (f", parts: {', '.join(urefs[:12])}" if urefs else "") if nunk else "")
                det += (f"; +{len(bad) - 4} more run(s)" if len(bad) > 4 else "") + (f"; net: {dropf}" if dropf else "")
                out.append({"level": "FAIL", "code": "POWER_WIDTH", "refs": [net], "value": round(sum(x["short_mm"] for x, _f in bad), 3),
                            "msg": f"{net}: {len(bad)} narrow run(s), {tot:.1f} mm, not proven for the {r['current_a']:g} A "
                                   f"copper ({r['width_source']}, {r['oz_outer']:g}/{r['oz_inner']:g} oz): {det}"})
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


def pad_spine(b, nc, L, width_mm):
    """R036: the spine of a rail the router never laid (or laid on inner layers only): a minimum spanning tree over the net's pads that
    touch copper layer `L`, straight segments `width_mm` wide. -> [(x1, y1, x2, y2, w) mm]; [] for fewer than two pads."""
    import pcbnew
    pts = [(pcbnew.ToMM(p.GetPosition().x), pcbnew.ToMM(p.GetPosition().y)) for p in b.GetPads()
           if p.GetNetCode() == nc and p.IsOnLayer(L)]
    if len(pts) < 2:
        return []
    done, out = {0}, []
    while len(done) < len(pts):
        i, j = min(((i, j) for i in done for j in range(len(pts)) if j not in done),
                   key=lambda ij: math.hypot(pts[ij[0]][0] - pts[ij[1]][0], pts[ij[0]][1] - pts[ij[1]][1]))
        out.append((pts[i][0], pts[i][1], pts[j][0], pts[j][1], width_mm))
        done.add(j)
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
        b.Delete(z)
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
        spines = {L: _spine_segments(b, ni.GetNetCode(), L) for L in (pcbnew.F_Cu, pcbnew.B_Cu)}
        if not any(spines.values()):                      # R036: no outer spine: one over the net's pads on the outer layer that has most
            w = max(0.15, float(plan(cons, layers=layers)[net]["track_mm"]) - 2.0 * over)      # the pour is spine + 2 * over: at least the IPC width
            tries = {L: pad_spine(b, ni.GetNetCode(), L, w) for L in spines}
            best = max(tries, key=lambda L: (len(tries[L]), L == pcbnew.F_Cu))
            spines[best] = tries[best]
        for L, segs in spines.items():
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
        b.Delete(z)  # Delete, not Remove: a Remove() of a discarded item breaks pcbnew.LoadBoard for the process
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


def _unreached(b, rails, pads, vias_of):
    """{net: [{layer, len_mm, width_mm, need_mm, at, refs}]} narrow runs (check()'s own grouping) still failing D71da's
    length / heat tests: the rail cannot reach its width there, a placement finding (parts around `at`, free a corridor)."""
    import pcbnew
    out = {}
    outer = ("F.Cu", "B.Cu")
    for net, r in rails.items():
        segs = []
        for t in b.GetTracks():
            if t.GetClass() != "PCB_TRACK" or t.GetNetname() != net:
                continue
            lay = b.GetLayerName(t.GetLayer())
            need = _need_on(r, lay)
            w = pcbnew.ToMM(t.GetWidth())
            if w < need - 1e-6:
                s, e = t.GetStart(), t.GetEnd()
                segs.append({"layer": lay, "x1": pcbnew.ToMM(s.x), "y1": pcbnew.ToMM(s.y), "x2": pcbnew.ToMM(e.x),
                             "y2": pcbnew.ToMM(e.y), "len": pcbnew.ToMM(t.GetLength()), "width": w, "need": need})
        for run in narrow_runs(segs, vias_of.get(net, ())):
            if not _neck_tests(run, r, set(outer)):
                continue
            refs = sorted({ref for n_, ref, x, y in pads if n_ == net and
                           any(math.hypot(segs[i][k] - c, segs[i][m] - d) < 1.5 for i in run["members"]
                               for k, m, c, d in (("x1", "y1", x, y), ("x2", "y2", x, y)))})
            out.setdefault(net, []).append({"layer": run["layer"], "len_mm": round(run["len"], 2), "width_mm": round(run["min_w"], 3),
                                            "need_mm": round(run["need"], 3), "at": run.get("at"), "refs": refs[:6]})
    for net in out:
        out[net].sort(key=lambda x: -x["len_mm"])
    return out


def _run_targets(board, cons):
    """W42: each failing run's own width target from its solved current (check()'s per-run physics) ->
    {(layer, x1, y1, x2, y2 rounded 0.01 mm, both orientations): {"need": width (0.05 mm steps, never above the rail's need),
    "amps", "cleared"}}. {} when the check cannot run: widen then falls back to the rail's need."""
    from . import boardfile
    out = {}
    try:
        detail = {}
        check(boardfile.facts_from_file(board), cons, None, board=board, detail=detail)
    except Exception:                                    # noqa: BLE001 - the per-run target is an improvement; the rail need stays
        return out
    for net, runs in detail.items():
        for d in runs:
            if not d["need_run"]:                        # no current behind it (dangling stub): the rail's need stays
                continue
            for g in d["segs"]:
                tgt = min(g["need"], max(0.05, _ceil05(d["need_run"])))
                for a, b in (((g["x1"], g["y1"]), (g["x2"], g["y2"])), ((g["x2"], g["y2"]), (g["x1"], g["y1"]))):
                    out[(g["layer"], round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2))] = {
                        "need": tgt, "amps": d["amps"], "cleared": d["cleared"]}
    return out


def _covered_by_twin(b, t, width_mm):
    """W68: another track of `t`'s net and layer with the same two ends (either order, to 0.1 um) and a width >= width_mm is on `b`."""
    import pcbnew

    def pt(v):
        return round(pcbnew.ToMM(v.x), 4), round(pcbnew.ToMM(v.y), 4)

    def ends(o):
        return {pt(o.GetStart()), pt(o.GetEnd())}
    mine = ends(t)
    return any(o is not t and o.GetClass() == "PCB_TRACK" and o.GetNetCode() == t.GetNetCode() and o.GetLayer() == t.GetLayer()
               and pcbnew.ToMM(o.GetWidth()) >= width_mm - 1e-6 and ends(o) == mine for o in b.GetTracks())


def _widen_ok(HR, M, net, lay, lo, hi, W, cur, uid):
    """K14: the clearance probe accepts a segment at width W when it has no violation the segment does not already have at its current
    width `cur`. Angle and via-centre findings (angle_not_45_90, track_not_centered_on_via) belong to the segment's geometry, not to its
    width: a router's pad stub at 15 deg failed every widening, so rails stayed at 0.15 mm (ledger 2026-11-27 k14)."""
    prim = [("seg", lay, lo, hi)]
    r = HR.probe_prims(M, net, prim, width=W, skip_ids=(uid,))
    if r.get("ok"):
        return True
    key = lambda v: (v.get("kind"), v.get("item"), tuple(round(x, 1) for x in (v.get("at") or ())))
    old = {key(v) for v in HR.probe_prims(M, net, prim, width=cur, skip_ids=(uid,)).get("violations") or ()}
    return all(key(v) in old for v in r.get("violations") or ())


def _widen_once(board, cons, out, nets=None, protect=()):
    """PHASE3G G4.1: every unlocked rail track narrower than the need of its layer (the numbers check() uses) is widened to the
    widest of need, 0.75 need, 0.5 need that the clearance probe accepts (handroute.probe_prims, the segment as one prim,
    the segment's own copper skipped; the probe model is built once, so widenings do not see each other: the close guard
    judges the result). A segment that ends in a pad narrower than its new width is split max(W, 0.5 mm) from that pad and
    only the far part is widened.
    W34 (ledger "route gate (W16 route-4)"): copper inside a same-net pour is widened too (check() has no pour carve-out, D71da:
    skipping it made this rung refuse every rail that had a zone). Last rung of the ladder: a run no longer than D71da's
    NECK_MAX_MM may stay narrow at the neck-proof width (`neck_width_mm`, heat test with the run's own length) when the probe
    refuses anything wider; each one is recorded in `necks` with its length, dT and DC drop (mV, at the rail's full current).
    PHASE3G G1b d: a LOCKED rail track is a candidate too, unless its net is in `protect` (drcguard.protected_nets: pair legs,
    RF). W37: a locked track is never modified (the protection manifest keeps it intact): the wider piece is a new unlocked track
    over it, same ends (a pad-end neck splits only an unlocked track).
    Writes `out` only when a track changed. -> {"widened_mm", "left_mm", "by_net", "necks": [...], "unreached": {net: [run]},
    "skipped": {"locked_mm", "pour_zone_mm" (always 0 now), "by_net"}} (skipped = narrow copper left alone: locked copper of a
    protected net; mm of track length)."""
    import pcbnew
    from . import gaps as GP, handroute as HR
    rec = {"widened_mm": 0.0, "left_mm": 0.0, "by_net": {}, "necks": [], "unreached": {},
           "skipped": {"locked_mm": 0.0, "pour_zone_mm": 0.0, "by_net": {}}}
    protect = set(protect or ())
    rails = _rails_on(board, cons, nets)
    if not rails:
        return rec
    b = pcbnew.LoadBoard(board)
    M = HR._model(board)                                 # the board's own rules (constraints: a file path, not used here)
    pads = [(p.GetNetname(), p.GetBoundingBox(), min(pcbnew.ToMM(p.GetSize().x), pcbnew.ToMM(p.GetSize().y)))
            for p in b.GetPads() if p.GetNetname()]
    pad_pos = [(p.GetNetname(), p.GetParentFootprint().GetReference(), pcbnew.ToMM(p.GetPosition().x), pcbnew.ToMM(p.GetPosition().y))
               for p in b.GetPads() if p.GetNetname()]
    vias_of = {}
    for v in b.GetTracks():
        if v.GetClass() == "PCB_VIA" and v.GetNetname() in rails:
            vias_of.setdefault(v.GetNetname(), []).append({"x": pcbnew.ToMM(v.GetPosition().x), "y": pcbnew.ToMM(v.GetPosition().y),
                                                           "dia_mm": pcbnew.ToMM(v.GetWidth())})
    tot = lambda net: rec["by_net"].setdefault(net, {"widened_mm": 0.0, "left_mm": 0.0})

    def skip(net, kind, mm):
        rec["skipped"][kind] += mm
        rec["skipped"]["by_net"].setdefault(net, {"locked_mm": 0.0, "pour_zone_mm": 0.0})[kind] += mm
    # the D71da runs of every narrow rail track: a short run may be necked, so its length bounds the last rung
    cand, segs = {}, {}
    for t in b.GetTracks():
        r = rails.get(t.GetNetname())
        if t.GetClass() != "PCB_TRACK" or r is None:
            continue
        lay = b.GetLayerName(t.GetLayer())
        need = _need_on(r, lay)
        if pcbnew.ToMM(t.GetWidth()) < need - 1e-6:
            s_, e_ = t.GetStart(), t.GetEnd()
            segs.setdefault(t.GetNetname(), []).append((t, {"layer": lay, "x1": pcbnew.ToMM(s_.x), "y1": pcbnew.ToMM(s_.y), "x2": pcbnew.ToMM(e_.x),
                                                          "y2": pcbnew.ToMM(e_.y), "len": pcbnew.ToMM(t.GetLength()),
                                                          "width": pcbnew.ToMM(t.GetWidth()), "need": need}))
    for net, lst in segs.items():
        for run in narrow_runs([g for _t, g in lst], vias_of.get(net, ())):
            for i in run["members"]:
                cand[lst[i][0].m_Uuid.AsString()] = run
    changed = 0
    tg = _run_targets(board, cons)
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
        own = tg.get((lay, round(a[0], 2), round(a[1], 2), round(z[0], 2), round(z[1], 2)))
        amps = r["current_a"]
        if own is not None:                              # W42: the run's own D71da width from its solved current
            if own["cleared"] or cur >= own["need"] - 1e-6:
                continue
            need, amps = own["need"], own["amps"] or r["current_a"]
        L = math.hypot(z[0] - a[0], z[1] - a[1])
        locked = t.IsLocked()
        if locked and net in protect:
            skip(net, "locked_mm", L)
            continue
        if locked and any(o is not t and o.GetClass() == "PCB_TRACK" and not o.IsLocked() and o.GetNetCode() == t.GetNetCode()
                          and o.GetLayer() == t.GetLayer() and pcbnew.ToMM(o.GetWidth()) >= need - 1e-6
                          and {(o.GetStart().x, o.GetStart().y), (o.GetEnd().x, o.GetEnd().y)} <= {(s.x, s.y), (e.x, e.y)}
                          for o in b.GetTracks()):
            continue                                     # already overlaid by an earlier widen: idempotent
        near = next((k for k, pt in enumerate((a, z)) if (_pad_min_at(pads, net, *pt) or need) < need - 1e-6), None)
        step = max(need, 0.5)
        pad_w = _pad_min_at(pads, net, *(a, z)[near]) if near is not None else None
        if near is not None:                             # the pad-end neck: at most D71da's NECK_MAX_MM long, as wide as the pad
            step = min(step, NECK_MAX_MM)
        near_w = max(cur, math.floor((pad_w or 0.0) * 20 + 1e-9) / 20.0) if pad_w else cur
        split = None
        if near is not None:                             # necked: the near part (step long) is the pad neck
            if L < step + 0.05:
                # the whole track is the pad neck: it takes the pad's width where the probe accepts it (W16 J3: 1.84 mm at 1.0 on a 1.5 mm pad)
                if (not locked and near_w > cur + 1e-6 and not _covered_by_twin(b, t, near_w)
                        and _widen_ok(HR, M, net, lay, a, z, near_w, cur, t.m_Uuid.AsString())):
                    t.SetWidth(pcbnew.FromMM(near_w))
                    tot(net)["widened_mm"] += L
                    rec["widened_mm"] += L
                    changed += 1
                tot(net)["left_mm"] += L
                rec["left_mm"] += L
                continue
            near_pt, far = (a, z) if near == 0 else (z, a)
            u = ((far[0] - near_pt[0]) / L, (far[1] - near_pt[1]) / L)
            split = (near_pt[0] + u[0] * step, near_pt[1] + u[1] * step)
            lo, hi = split, far
        else:
            lo, hi = a, z
            if not locked and L >= SPLIT_MIN_MM:         # what stops a wider track sits at its end(s): only the end(s) stay narrow
                uid = t.m_Uuid.AsString()
                for f in WIDEN_FRACS:
                    Wt = round(need * f, 4)
                    if Wt <= cur + 1e-6:
                        break
                    u = ((z[0] - a[0]) / L, (z[1] - a[1]) / L)
                    pt = lambda d: (round(a[0] + u[0] * d, 4), round(a[1] + u[1] * d, 4))
                    span, found, whole = (0.0, L), False, False
                    for _ in range(4):                   # a finding names one point of its blocker: step past it until the part is clear
                        m0, m1 = pt(span[0]), pt(span[1])
                        if _widen_ok(HR, M, net, lay, m0, m1, Wt, cur, uid):
                            found, whole = span != (0.0, L), span == (0.0, L)
                            break
                        sp = _clear_span(HR.probe_prims(M, net, [("seg", lay, m0, m1)], width=Wt, skip_ids=(uid,)).get("violations") or (),
                                         m0, m1, Wt)
                        if sp is None:
                            break
                        span = (span[0] + sp[0], span[0] + sp[1])
                    if whole:                            # the whole track takes Wt (the ladder below finds it) ...
                        break
                    if not found:                        # ... or no clear part at this width: the next, narrower one
                        continue
                    for p0, p1 in ((a, m0), (m1, z)):    # the end pieces: their own tracks at the old width
                        if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < 1e-3:
                            continue
                        et = pcbnew.PCB_TRACK(b)
                        et.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(p0[0]), pcbnew.FromMM(p0[1])))
                        et.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(p1[0]), pcbnew.FromMM(p1[1])))
                        et.SetWidth(t.GetWidth())
                        et.SetLayer(t.GetLayer())
                        et.SetNetCode(t.GetNetCode())
                        b.Add(et)
                        tot(net)["left_mm"] += math.hypot(p1[0] - p0[0], p1[1] - p0[1])
                        rec["left_mm"] += math.hypot(p1[0] - p0[0], p1[1] - p0[1])
                    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(m0[0]), pcbnew.FromMM(m0[1])))
                    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(m1[0]), pcbnew.FromMM(m1[1])))
                    lo, hi = m0, m1
                    break
        ladder = [round(need * f, 4) for f in WIDEN_FRACS]
        run = cand.get(t.m_Uuid.AsString())
        wn = None
        if run is not None and run["len"] <= NECK_MAX_MM + 1e-9 and near is None:
            oz = _run_oz(run, r, {"F.Cu", "B.Cu"}, None)
            wn = math.ceil(neck_width_mm(amps, run["len"], oz, r.get("rise_c") or DEFAULT_RISE_C) * 20 + 1e-9) / 20.0
            if wn < need - 1e-6:
                ladder = sorted({*ladder, round(wn, 4)}, reverse=True)
        chosen = None
        for W in ladder:
            if W <= cur + 1e-6:
                break
            if _widen_ok(HR, M, net, lay, lo, hi, W, cur, t.m_Uuid.AsString()):
                chosen = W
                break
        if chosen is None:                               # between the rungs: the widest WIDEN_STEP_MM multiple the probe accepts
            chosen = _widest_between(lambda W: _widen_ok(HR, M, net, lay, lo, hi, W, cur, t.m_Uuid.AsString()),
                                     cur, max((W for W in ladder if W > cur + 1e-6), default=cur))
        if chosen is None:
            tot(net)["left_mm"] += L
            rec["left_mm"] += L
            continue
        wide_mm = math.hypot(hi[0] - lo[0], hi[1] - lo[1])
        if split is not None:
            left_mm = step
            if near_w > cur + 1e-6 and not HR.probe_prims(M, net, [("seg", lay, near_pt, split)], width=near_w,
                                                          skip_ids=(t.m_Uuid.AsString(),)).get("ok"):
                near_w = cur                                 # the pad neck takes the pad's width only where the probe accepts it
            nt = pcbnew.PCB_TRACK(b)                       # the near part: its own track at the old width
            nt.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(near_pt[0]), pcbnew.FromMM(near_pt[1])))
            nt.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(split[0]), pcbnew.FromMM(split[1])))
            nt.SetWidth(pcbnew.FromMM(near_w))
            nt.SetLayer(t.GetLayer())
            nt.SetNetCode(t.GetNetCode())
            nt.SetLocked(False)                            # W37: the near part overlays unlocked; a locked original stays whole
            if not locked and not _covered_by_twin(b, nt, near_w):    # W68: no exact duplicate of a neck piece that is already there
                b.Add(nt)
            sp = pcbnew.VECTOR2I(pcbnew.FromMM(split[0]), pcbnew.FromMM(split[1]))
            if not locked:
                t.SetStart(sp) if near == 0 else t.SetEnd(sp)
            tot(net)["left_mm"] += left_mm
            rec["left_mm"] += left_mm
        if locked:                                       # W37: a locked item is never modified: the wide piece is new, unlocked copper over it
            wt = pcbnew.PCB_TRACK(b)
            wt.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(lo[0]), pcbnew.FromMM(lo[1])))
            wt.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(hi[0]), pcbnew.FromMM(hi[1])))
            wt.SetWidth(pcbnew.FromMM(chosen))
            wt.SetLayer(t.GetLayer())
            wt.SetNetCode(t.GetNetCode())
            wt.SetLocked(False)
            if not _covered_by_twin(b, wt, chosen):      # W68: a piece as wide with the same ends is already there (an earlier pass)
                b.Add(wt)
        elif _covered_by_twin(b, t, chosen):             # W68: a track with the same ends is already as wide: widening this one would stack an exact duplicate
            b.Delete(t)
        else:
            t.SetWidth(pcbnew.FromMM(chosen))
        if run is not None and run["len"] <= NECK_MAX_MM + 1e-9 and chosen < need - 1e-6:   # a short run left under the need: a neck-down (D71da)
            oz = _run_oz(run, r, {"F.Cu", "B.Cu"}, None)
            rec["necks"].append({"net": net, "layer": lay, "at": [round(a[0], 2), round(a[1], 2)], "run_len_mm": round(run["len"], 2),
                                 "width_mm": chosen, "dT_c": round(neck_heat_c(amps, run["len"], chosen, oz), 2),
                                 "drop_mv": round(neck_drop_mv(amps, run["len"], chosen, oz), 2), "amps": amps})
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
    try:
        rec["unreached"] = _unreached(b, rails, pad_pos, vias_of)
    except Exception as e:                                # noqa: BLE001 - the findings are a report, never a reason to lose the widening
        rec["unreached_error"] = f"{type(e).__name__}: {str(e)[:100]}"
    if changed:
        pcbnew.SaveBoard(out, b)
        GP._sidecars(board, out)
    return rec


WIDEN_STEP_MM = 0.05
SPLIT_MIN_MM = 3.0                    # a track at least this long is split when its blockers sit at one end
SPLIT_REACH_MM = 0.6                  # room kept past the last blocker, beyond half the full width


def _clear_span(viol, a, z, W):
    """Ledger 2026-11-28 (W16 VBATT_IN: 8.6 mm stayed at 1.43 mm for a pad at its start and a via at its end): `viol` = what the probe
    finds at width W on the track a-z.  -> (d0, d1) mm along the track, the part clear of every finding (each kept SPLIT_REACH_MM
    plus half of W away), when that part is at least 2 mm and a third of the track; None when the track is blocked along its
    length or a finding has no position (a geometry finding)."""
    L = math.hypot(z[0] - a[0], z[1] - a[1])
    if not viol or L <= 0 or any(not v.get("at") for v in viol):
        return None
    u = ((z[0] - a[0]) / L, (z[1] - a[1]) / L)
    ts = [(v["at"][0] - a[0]) * u[0] + (v["at"][1] - a[1]) * u[1] for v in viol]
    reach = W / 2 + SPLIT_REACH_MM
    d0 = max([0.0] + [t + reach for t in ts if t <= L / 2])
    d1 = min([L] + [t - reach for t in ts if t > L / 2])
    return (round(d0, 3), round(d1, 3)) if d1 - d0 >= max(2.0, L / 3) else None


def _widest_between(ok, cur, top):
    """The widest width on the WIDEN_STEP_MM grid strictly between `cur` and `top` that `ok` accepts, None when there is none.
    Ledger 2026-11-28 (W16 VBATT_IN): the ladder had three rungs (need, 0.75, 0.5 of it), so a 1.0 mm run that fitted 1.35 beside
    its neighbours stayed at 1.0 because 1.38 did not fit.  Bisection: a wider track never has more room."""
    n = int(math.floor((top - cur) / WIDEN_STEP_MM - 1e-9))
    if n < 1:
        return None
    w = lambda k: round(cur + k * WIDEN_STEP_MM, 4)
    lo, hi = 0, n                                        # lo: accepted (0 = the current width), hi + 1: refused (top was)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(w(mid)):
            lo = mid
        else:
            hi = mid - 1
    return w(lo) if lo else None


DISPLACE_FRACS = (1.0, 0.75, 0.5)    # widths (fractions of the run's need) the displace step clears room for, widest first


def displace(board, cons, out, nets=None, frac=1.0, keep=()):
    """Ledger 2026-11-28 (W16 VBATT_IN, 5 A): the plain router lays signal tracks against a rail that was laid narrow, so no later
    widen has room (38.7 mm stayed at 1.0-1.38 mm of 2.77 beside CAN2_L / ETH_LED_ACT_N).  For every rail track of `nets` under its
    need (the run's own solved need, as widen): when the ONLY things that stop it at `frac` x need are unlocked tracks / vias of
    other nets (never a net in `keep`, a pad, a locked item, an edge or a keep-out), those items are removed.  The caller widens
    and re-routes the removed nets (gaps._r_powerwiden); nothing here is kept unless its guard accepts the result.
    Writes `out` only when something was removed. -> {"ripped": {net: items removed}, "segs": rail tracks given room}."""
    import pcbnew
    from . import gaps as GP, handroute as HR
    rec = {"ripped": {}, "segs": 0, "frac": frac}
    rails = _rails_on(board, cons, nets)
    if not rails:
        return rec
    keep = set(keep or ()) | set(rails)
    b = pcbnew.LoadBoard(board)
    M = HR._model(board)
    tg = _run_targets(board, cons)
    key = lambda v: (v.get("kind"), v.get("item"), tuple(round(x, 1) for x in (v.get("at") or ())))
    kill = {}
    for t in b.GetTracks():
        r = rails.get(t.GetNetname())
        if t.GetClass() != "PCB_TRACK" or r is None:
            continue
        net, lay = t.GetNetname(), b.GetLayerName(t.GetLayer())
        need, cur = _need_on(r, lay), pcbnew.ToMM(t.GetWidth())
        s, e = t.GetStart(), t.GetEnd()
        a, z = (pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)), (pcbnew.ToMM(e.x), pcbnew.ToMM(e.y))
        own = tg.get((lay, round(a[0], 2), round(a[1], 2), round(z[0], 2), round(z[1], 2)))
        if own is not None:
            if own["cleared"]:
                continue
            need = own["need"]
        W = round(need * frac, 4)
        if W <= cur + 1e-6:
            continue
        uid, prim = t.m_Uuid.AsString(), [("seg", lay, a, z)]
        old = {key(v) for v in HR.probe_prims(M, net, prim, width=cur, skip_ids=(uid,)).get("violations") or ()}
        movable = lambda v: (v.get("kind") in ("clearance", "crossing") and v.get("id") and not v.get("locked") and v.get("net")
                             and v["net"] not in keep and str(v.get("item", "")).split(" ")[0] in ("track", "via"))

        def blockers(w):                                 # what stops width w and is not there at the current width
            return [v for v in HR.probe_prims(M, net, prim, width=w, skip_ids=(uid,)).get("violations") or () if key(v) not in old]
        new = blockers(W)
        if new and not all(movable(v) for v in new):     # a pad / locked item / rail is in the way at W: the widest width only movable copper stops
            W = _widest_between(lambda w: all(movable(v) for v in blockers(w)), cur, W)
            new = blockers(W) if W else []
        if not new:
            continue
        rec["segs"] += 1
        for v in new:
            kill[v["id"]] = v["net"]
    if not kill:
        return rec
    for t in list(b.GetTracks()):
        u = t.m_Uuid.AsString()
        n = next((n for i, n in kill.items() if u.startswith(i)), None)
        if n is not None and not t.IsLocked() and t.GetNetname() == n:
            rec["ripped"][n] = rec["ripped"].get(n, 0) + 1
            b.Delete(t)
    if rec["ripped"]:
        pcbnew.SaveBoard(out, b)
        GP._sidecars(board, out)
    return rec


WIDEN_PASSES = 4


def widen(board, cons, out, nets=None, protect=()):
    """K14: `_widen_once` repeated while it changes copper (max WIDEN_PASSES): a widened run lowers the solved flow of its neighbours and
    the per-run targets move, so one pass left runs at the first target (route-9 VBATT_PROT 6.57 -> 5.90 one pass, 2.82 at the fixpoint).
    -> the last pass's record with widened_mm / necks / by_net summed over the passes ("passes" = how many changed copper)."""
    rec, src, n = None, board, 0
    for _ in range(WIDEN_PASSES):
        r = _widen_once(src, cons, out, nets=nets, protect=protect)
        moved = r["widened_mm"] > 0 and os.path.exists(out)
        if rec is None:
            rec = r
        else:
            keep = {k: v for k, v in rec.items() if k in ("widened_mm", "by_net", "necks")}
            rec = r
            rec["widened_mm"] = round(rec["widened_mm"] + keep["widened_mm"], 3)
            for net, v in keep["by_net"].items():
                d = rec["by_net"].setdefault(net, {"widened_mm": 0.0, "left_mm": 0.0})
                d["widened_mm"] = round(d["widened_mm"] + v["widened_mm"], 3)
            nk = lambda x: (x["net"], x["layer"], tuple(x["at"]))      # a neck a later pass widened again is one neck, at its last width
            rec["necks"] = [x for x in keep["necks"] if nk(x) not in {nk(y) for y in rec["necks"]}] + rec["necks"]
        if not moved:
            break
        n += 1
        src = out
    rec["passes"] = n
    return rec


def widen_guarded(board, cons, out, nets=None, protect=()):
    """W34: `widen` for the route stage: kept only when the DRC guard (shorts, errors, track opens) is no higher afterwards, else
    `out` is removed. -> widen's record plus {"dropped": reason | None, "before", "after"}."""
    from . import drcguard as DG
    rec = widen(board, cons, out, nets=nets, protect=protect)
    rec.update(dropped=None, before=None, after=None)
    if not os.path.exists(out):
        return rec
    rec["before"], rec["after"] = _guard_measure(DG.measure(board), DG), _guard_measure(DG.measure(out), DG)
    worse = [k for k in GUARD_KEYS if rec["after"][k] > rec["before"][k]]
    if worse:
        rec["dropped"] = ", ".join(f"{k} {rec['before'][k]} -> {rec['after'][k]}" for k in worse)
        for f in (out, os.path.splitext(out)[0] + ".kicad_pro", os.path.splitext(out)[0] + ".kicad_prl"):
            if os.path.exists(f):
                os.remove(f)
    return rec


STITCH_PITCH_MM = 2.0        # centre spacing of the stitching vias laid on a still-failing run
STITCH_MAX_PER_RUN = 8


def _seg_key(g):
    return (g["layer"], frozenset(((round(g["x1"], 2), round(g["y1"], 2)), (round(g["x2"], 2), round(g["y2"], 2)))))


def _seg_keys(board):
    """{(layer, endpoints)} of every track of `board` (mm, 0.01): what an update run's input already had."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    mm = pcbnew.ToMM
    return {(b.GetLayerName(t.GetLayer()), frozenset(((round(mm(t.GetStart().x), 2), round(mm(t.GetStart().y), 2)),
                                                     (round(mm(t.GetEnd().x), 2), round(mm(t.GetEnd().y), 2)))))
            for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"}


def _vias_guarded(cur, nxt, board, net, at, dia, drill, ref_m, DG):
    """Copy of `cur` with a via of the given size at each (x, y) of `at` -> `nxt`; kept only when the DRC guard is no higher than
    `ref_m`. -> (ok, measure | None, why)."""
    import pcbnew
    from . import gaps as GP
    bb = pcbnew.LoadBoard(cur)
    nc = bb.FindNet(net).GetNetCode()
    for sx, sy in at:
        v = pcbnew.PCB_VIA(bb)
        v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(sx), pcbnew.FromMM(sy)))
        v.SetWidth(dia)
        v.SetDrill(drill)
        v.SetNetCode(nc)
        bb.Add(v)
    pcbnew.SaveBoard(nxt, bb)
    GP._sidecars(board, nxt)
    m = _guard_measure(DG.measure(nxt), DG)
    worse = [g for g in GUARD_KEYS if m[g] > ref_m[g]]
    if worse:
        return False, None, ", ".join(f"{g} {ref_m[g]} -> {m[g]}" for g in worse)
    return True, m, ""


PAD_VIA_SPACING_MM = 0.9      # centre spacing of vias laid inside one pad


def add_pad_vias(board, cons, out, nets=None, cons_path=None, only_new_vs=None):
    """POWER_WIDTH repair for a plane-adjacent narrow trunk: a narrow run of a rail that has a same-net zone ends in a pad (a source /
    load pad bigger than its via); the pad gets via-in-pad vias into the plane, so the pad reaches the plane directly and the
    narrow track is only a parallel path (the rail solve then gives it its small share). Vias per pad = ceil(run current / via amps),
    at least 2, less the vias already inside the pad; sites come from handroute.window inside the pad, best margin first; the pad's vias are
    added together at the rail's own via size and kept only when the DRC guard (shorts, errors, track opens) is no higher. Writes `out` only when a via was added.
    `only_new_vs`: the board an update run started from; only runs with a segment that is not on it are repaired (inherited copper
    is left as shipped). -> {"pads": {"REF.num": {"net", "need": n, "added": k}}}"""
    import pcbnew
    from . import gaps as GP, handroute as HR, boardfile
    rec = {"pads": {}}
    rails = _rails_on(board, cons, nets)
    ctx = _board_ctx(board, set(rails)) if rails else None
    if not ctx:
        return rec
    zoned = {z.GetNetname() for z in ctx["b"].Zones() if not z.GetIsRuleArea()}
    rails = {n: r for n, r in rails.items() if n in zoned}
    if not rails:
        return rec
    det = {}
    check(boardfile.facts_from_file(board), cons, dcdrop_status(board, cons), board=board, detail=det)
    old = _seg_keys(only_new_vs) if only_new_vs else None
    todo = {}                                                # (net, ref, num) -> (pad, amps the run carries)
    for net, runs in det.items():
        if net not in rails:
            continue
        for rn in runs:
            if rn["cleared"] or (old is not None and all(_seg_key(g) in old for g in rn["segs"])):
                continue
            cnt = {}
            for g in rn["segs"]:
                for k in (("x1", "y1"), ("x2", "y2")):
                    pt = (round(g[k[0]], 2), round(g[k[1]], 2))
                    cnt[pt] = cnt.get(pt, 0) + 1
            for (x, y), n_ in cnt.items():
                if n_ != 1:
                    continue
                pt = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
                for pad in ctx["pads"].get(net, []):
                    if len(pad) >= 8 and pad[2].Contains(pt):
                        key = (net, pad[0], pad[1])
                        todo[key] = (pad, max(todo.get(key, (None, 0.0))[1], float(rn.get("amps") or 0.0)))
    from . import drcguard as DG
    b = ctx["b"]
    cur, tmps, k = board, [], 0
    ref_m = _guard_measure(DG.measure(board), DG)
    try:
        for (net, ref, num), (pad, amps) in sorted(todo.items()):
            r = rails[net]
            x0, y0, x1, y1 = pad[7]
            same = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == net]
            have = sum(1 for vx, vy, _vr in ctx["copper"].get(net, ([], []))[1] if x0 <= vx <= x1 and y0 <= vy <= y1)
            need_n = max(2, math.ceil(amps / max(float(r.get("via_amps") or 1.0), 1e-3) - 1e-9)) - have
            if need_n <= 0 or not same:
                continue
            dia, drill = same[0].GetWidth(), same[0].GetDrillValue()           # the rail's own via size
            rad = pcbnew.ToMM(dia) / 2 + 0.1
            W = HR.window(cur, at=((x0 + x1) / 2, (y0 + y1) / 2), size=(x1 - x0 + 1.0, y1 - y0 + 1.0), net=net, constraints=cons_path)
            sites = [(sx, sy) for sx, sy, *_m in ((W.get("via_sites") or {}).get("sites") or [])
                     if x0 + rad <= sx <= x1 - rad and y0 + rad <= sy <= y1 - rad]
            chosen = []
            for sx, sy in sites:
                if len(chosen) >= need_n:
                    break
                if all(math.hypot(sx - cx, sy - cy) >= PAD_VIA_SPACING_MM for cx, cy in chosen):
                    chosen.append((sx, sy))
            done = 0
            if chosen:
                k += 1
                nxt = f"{out}.pv{k}.kicad_pcb"
                tmps.append(nxt)
                ok, m, why = _vias_guarded(cur, nxt, board, net, chosen, dia, drill, ref_m, DG)
                if ok:
                    cur, ref_m, done = nxt, m, len(chosen)
                else:
                    rec["pads"][f"{ref}.{num}"] = {"net": net, "need": need_n, "added": 0, "refused": why}
                    continue
            rec["pads"][f"{ref}.{num}"] = {"net": net, "need": need_n, "added": done, "at": chosen[:done]}
        # runs still failing: stitching vias on the run's own centre line into the plane (a via on the track joins it to the plane)
        det2 = {}
        check(boardfile.facts_from_file(cur), cons, dcdrop_status(cur, cons), board=cur, detail=det2)
        M = HR._model(cur)
        for net, runs in sorted(det2.items()):
            if net not in rails:
                continue
            same = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == net]
            if not same:
                continue
            dia, drill = same[0].GetWidth(), same[0].GetDrillValue()
            for rn in runs:
                if rn["cleared"] or (old is not None and all(_seg_key(g) in old for g in rn["segs"])):
                    continue
                pts = []
                for g in rn["segs"]:
                    n_ = max(1, int(g["len"] // STITCH_PITCH_MM)) if "len" in g else 1
                    for q in range(n_ + 1):
                        t_ = q / n_
                        pts.append((g["x1"] + (g["x2"] - g["x1"]) * t_, g["y1"] + (g["y2"] - g["y1"]) * t_))
                chosen = []
                for x, y in pts:
                    if len(chosen) >= STITCH_MAX_PER_RUN:
                        break
                    if all(math.hypot(x - cx, y - cy) >= STITCH_PITCH_MM for cx, cy in chosen) and HR.probe_prims(M, net, [("via", (x, y))]).get("ok"):
                        chosen.append((round(x, 3), round(y, 3)))
                if not chosen:
                    continue
                k += 1
                nxt = f"{out}.pv{k}.kicad_pcb"
                tmps.append(nxt)
                ok, m, why = _vias_guarded(cur, nxt, board, net, chosen, dia, drill, ref_m, DG)
                key = f"run@{rn['segs'][0]['x1']:.1f},{rn['segs'][0]['y1']:.1f}"
                rec["pads"][key] = {"net": net, "need": len(chosen), "added": len(chosen) if ok else 0, **({} if ok else {"refused": why})}
                if ok:
                    cur, ref_m = nxt, m
                    M = HR._model(cur)
        if cur != board:
            os.replace(cur, out)
            tmps.remove(cur)
            GP._sidecars(board, out)
    finally:
        for q in tmps:
            GP._rm(q)
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
