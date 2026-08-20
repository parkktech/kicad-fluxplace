"""Stackup definition and impedance solving.

Why this exists: a board shipped for review with no stackup at all — no
dielectric thickness, no Er, inner layers typed as generic signal. Its netclasses
confidently specified a 0.19 mm differential pair and 0.075 mm tracks, and none
of it could be checked, because trace geometry means nothing without the
dielectric under it. The netclass looked precise and was unverifiable.

So this module does two things:

  1. Writes a REAL stackup into the .kicad_pcb, from a named fab profile. Once
     it is in the file, KiCad, the fab and any reviewer are looking at the same
     board.
  2. Solves impedance both ways — what does this geometry give, and what
     geometry hits this target — so the netclass numbers are derived from the
     stackup instead of hoped at.

ACCURACY, stated plainly. These are the closed-form IPC-2141 / Wheeler-Hammerstad
approximations, not a 2D field solver. They are good to roughly ±5-10% for
ordinary geometry, which is enough to catch the failure this module was written
for — a trace that is nowhere near its target — and NOT enough to sign off a
controlled-impedance order. Every fab that offers controlled impedance will run
their own field solver against their actual laminate and tell you the widths
they want; those numbers win. `report()` says so in its output rather than
letting a number look more authoritative than it is.
"""

import json
import math
import os
import re


# --------------------------------------------------------------------------
# Fab stackup profiles
#
# Thicknesses in mm. These are the published standard stackups. A fab will
# substitute laminate at will unless you buy controlled impedance, which is
# exactly why the profile is recorded in the board and in the fab brief.
# --------------------------------------------------------------------------

PROFILES = {
    "jlcpcb-4l-1.6": {
        "description": "JLCPCB standard 4-layer 1.6 mm (JLC7628), 1 oz outer / 0.5 oz inner",
        "vendor": "JLCPCB",
        "reference_layers": ["In1.Cu"],
        "layers": [
            {"type": "copper",     "name": "F.Cu",   "thickness": 0.035},
            {"type": "prepreg",    "material": "7628", "thickness": 0.2104,
             "epsilon_r": 4.4, "loss_tangent": 0.02},
            {"type": "copper",     "name": "In1.Cu", "thickness": 0.0152},
            {"type": "core",       "material": "FR4", "thickness": 1.065,
             "epsilon_r": 4.6, "loss_tangent": 0.02},
            {"type": "copper",     "name": "In2.Cu", "thickness": 0.0152},
            {"type": "prepreg",    "material": "7628", "thickness": 0.2104,
             "epsilon_r": 4.4, "loss_tangent": 0.02},
            {"type": "copper",     "name": "B.Cu",   "thickness": 0.035},
        ],
    },
    "pcbway-4l-1.6": {
        "description": "PCBWay standard 4-layer 1.6 mm, 1 oz outer / 0.5 oz inner",
        "vendor": "PCBWay",
        "reference_layers": ["In1.Cu"],
        "layers": [
            {"type": "copper",  "name": "F.Cu",   "thickness": 0.035},
            {"type": "prepreg", "material": "7628", "thickness": 0.2,
             "epsilon_r": 4.4, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In1.Cu", "thickness": 0.0152},
            {"type": "core",    "material": "FR4", "thickness": 1.08,
             "epsilon_r": 4.6, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In2.Cu", "thickness": 0.0152},
            {"type": "prepreg", "material": "7628", "thickness": 0.2,
             "epsilon_r": 4.4, "loss_tangent": 0.02},
            {"type": "copper",  "name": "B.Cu",   "thickness": 0.035},
        ],
    },
    "jlcpcb-6l-1.6": {
        "description": "JLCPCB standard 6-layer 1.6 mm — Sig/GND/Sig/Sig/PWR/Sig",
        "vendor": "JLCPCB",
        "reference_layers": ["In1.Cu", "In4.Cu"],
        "layers": [
            {"type": "copper",  "name": "F.Cu",   "thickness": 0.035},
            {"type": "prepreg", "material": "3313", "thickness": 0.0994,
             "epsilon_r": 4.05, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In1.Cu", "thickness": 0.0152},
            {"type": "core",    "material": "FR4", "thickness": 0.15,
             "epsilon_r": 4.5, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In2.Cu", "thickness": 0.0152},
            {"type": "prepreg", "material": "2116", "thickness": 0.9704,
             "epsilon_r": 4.3, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In3.Cu", "thickness": 0.0152},
            {"type": "core",    "material": "FR4", "thickness": 0.15,
             "epsilon_r": 4.5, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In4.Cu", "thickness": 0.0152},
            {"type": "prepreg", "material": "3313", "thickness": 0.0994,
             "epsilon_r": 4.05, "loss_tangent": 0.02},
            {"type": "copper",  "name": "B.Cu",   "thickness": 0.035},
        ],
    },
    "pcbway-6l-1.6": {
        "description": "PCBWay standard 6-layer 1.6 mm — Sig/GND/Sig/Sig/PWR/Sig",
        "vendor": "PCBWay",
        "reference_layers": ["In1.Cu", "In4.Cu"],
        "layers": [
            {"type": "copper",  "name": "F.Cu",   "thickness": 0.035},
            {"type": "prepreg", "material": "1080", "thickness": 0.1,
             "epsilon_r": 4.1, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In1.Cu", "thickness": 0.0152},
            {"type": "core",    "material": "FR4", "thickness": 0.2,
             "epsilon_r": 4.5, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In2.Cu", "thickness": 0.0152},
            {"type": "prepreg", "material": "7628", "thickness": 0.8692,
             "epsilon_r": 4.4, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In3.Cu", "thickness": 0.0152},
            {"type": "core",    "material": "FR4", "thickness": 0.2,
             "epsilon_r": 4.5, "loss_tangent": 0.02},
            {"type": "copper",  "name": "In4.Cu", "thickness": 0.0152},
            {"type": "prepreg", "material": "1080", "thickness": 0.1,
             "epsilon_r": 4.1, "loss_tangent": 0.02},
            {"type": "copper",  "name": "B.Cu",   "thickness": 0.035},
        ],
    },
    "generic-2l-1.6": {
        "description": "Generic 2-layer 1.6 mm FR4",
        "vendor": "generic",
        "layers": [
            {"type": "copper", "name": "F.Cu", "thickness": 0.035},
            {"type": "core",   "material": "FR4", "thickness": 1.51,
             "epsilon_r": 4.5, "loss_tangent": 0.02},
            {"type": "copper", "name": "B.Cu", "thickness": 0.035},
        ],
    },
}


def profile_names():
    return sorted(PROFILES)


def total_thickness(profile):
    return round(sum(l["thickness"] for l in PROFILES[profile]["layers"]), 4)


def outer_dielectric(profile):
    """(height, Er) of the dielectric between an outer copper layer and the
    first inner plane — the geometry a microstrip actually sees."""
    layers = PROFILES[profile]["layers"]
    for i, l in enumerate(layers):
        if l["type"] in ("prepreg", "core") and i > 0:
            return l["thickness"], l["epsilon_r"]
    raise ValueError("no dielectric in profile")


# --------------------------------------------------------------------------
# Impedance — IPC-2141 closed forms
# --------------------------------------------------------------------------

def microstrip_z0(w, h, t, er):
    """Single-ended surface microstrip characteristic impedance (ohms).

    IPC-2141:  Z0 = 87/sqrt(er+1.41) * ln(5.98h / (0.8w + t))

    Valid roughly for 0.1 < w/h < 3.0 and er 1..15. Outside that the answer
    degrades, so callers should sanity-check w/h rather than trust it blindly.
    """
    if w <= 0 or h <= 0:
        raise ValueError("width and height must be positive")
    denom = 0.8 * w + t
    if denom <= 0:
        raise ValueError("degenerate geometry")
    return (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / denom)


def stripline_z0(w, h, t, er):
    """Symmetric stripline (inner layer between two planes), IPC-2141:
        Z0 = 60/sqrt(er) * ln(4h / (0.67*pi*(0.8w + t)))
    h is the FULL plane-to-plane separation."""
    if w <= 0 or h <= 0:
        raise ValueError("width and height must be positive")
    return (60.0 / math.sqrt(er)) * math.log(4.0 * h / (0.67 * math.pi * (0.8 * w + t)))


def diff_microstrip_z(w, s, h, t, er):
    """Edge-coupled differential microstrip, IPC-2141:
        Zdiff = 2*Z0 * (1 - 0.48 * exp(-0.96 * s/h))
    s is the edge-to-edge GAP between the two traces."""
    z0 = microstrip_z0(w, h, t, er)
    return 2.0 * z0 * (1.0 - 0.48 * math.exp(-0.96 * s / h))


def diff_stripline_z(w, s, h, t, er):
    """Edge-coupled differential stripline:
        Zdiff = 2*Z0 * (1 - 0.347 * exp(-2.9 * s/h))"""
    z0 = stripline_z0(w, h, t, er)
    return 2.0 * z0 * (1.0 - 0.347 * math.exp(-2.9 * s / h))


def _bisect(fn, target, lo, hi, tol=1e-4, iters=200):
    """Solve fn(x) == target on [lo,hi]. fn must be monotonic here (impedance
    falls as width rises, which holds across every sane geometry)."""
    flo, fhi = fn(lo), fn(hi)
    if (flo - target) * (fhi - target) > 0:
        return None                      # target not bracketed
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        fm = fn(mid)
        if abs(fm - target) < tol:
            return mid
        if (flo - target) * (fm - target) <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi)


def solve_microstrip_width(target_z, h, t, er, lo=0.05, hi=5.0):
    """Trace width (mm) that hits target_z on an outer layer."""
    return _bisect(lambda w: microstrip_z0(w, h, t, er), target_z, lo, hi)


def solve_diff_microstrip(target_z, h, t, er, gap=None, lo=0.05, hi=5.0):
    """(width, gap) for a differential pair hitting target_z.

    With gap fixed, solve width. Without, pick gap = h (a common, manufacturable
    default that keeps the pair loosely enough coupled that small etch errors do
    not swing the impedance) and solve width for it.
    """
    g = gap if gap is not None else h
    w = _bisect(lambda w: diff_microstrip_z(w, g, h, t, er), target_z, lo, hi)
    return (w, g)


# --------------------------------------------------------------------------
# Reading / writing the board
# --------------------------------------------------------------------------

def board_has_stackup(board_path):
    with open(board_path) as fh:
        return "(stackup" in fh.read()


def _fmt(v):
    return ("%.4f" % v).rstrip("0").rstrip(".")


def render_stackup_sexp(profile, indent="\t\t"):
    """The KiCad `(stackup ...)` s-expression for a profile."""
    p = PROFILES[profile]
    L = []
    L.append("%s(stackup" % indent)
    i2 = indent + "\t"
    i3 = i2 + "\t"
    diel = 0
    for l in p["layers"]:
        if l["type"] == "copper":
            L.append('%s(layer "%s" (type "copper") (thickness %s))'
                     % (i2, l["name"], _fmt(l["thickness"])))
        else:
            diel += 1
            L.append('%s(layer "dielectric %d" (type "%s")' % (i2, diel, l["type"]))
            L.append('%s(thickness %s) (material "%s")'
                     % (i3, _fmt(l["thickness"]), l.get("material", "FR4")))
            L.append('%s(epsilon_r %s) (loss_tangent %s)'
                     % (i3, _fmt(l["epsilon_r"]), _fmt(l.get("loss_tangent", 0.02))))
            L.append("%s)" % i2)
    L.append('%s(copper_finish "ENIG")' % i2)
    L.append('%s(dielectric_constraints no)' % i2)
    L.append("%s)" % indent)
    return "\n".join(L)


def strip_stackup(src):
    """Remove an existing (stackup ...) block, brace-balanced."""
    i = src.find("(stackup")
    if i < 0:
        return src, False
    depth, j = 0, i
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    # swallow the trailing newline+indent the block sat on
    k = i
    while k > 0 and src[k-1] in " \t":
        k -= 1
    if k > 0 and src[k-1] == "\n":
        k -= 1
    return src[:k] + src[j:], True


def apply_to_board(board_path, profile, backup=True, replace=False, log=print):
    """Write the stackup into the .kicad_pcb `(setup ...)` block.

    Text-level surgery rather than pcbnew, because the board stackup is not
    exposed through the python API in a way that survives a save on every KiCad
    build, and the s-expression is unambiguous.
    """
    if profile not in PROFILES:
        raise ValueError("unknown profile %r (have: %s)"
                         % (profile, ", ".join(profile_names())))
    with open(board_path) as fh:
        src = fh.read()

    replaced = False
    if "(stackup" in src:
        if not replace:
            return {"changed": False,
                    "reason": "board already has a stackup; refusing to overwrite it "
                              "(pass replace=True / --replace to change it)"}
        src, replaced = strip_stackup(src)
        log("replacing the existing stackup")

    m = re.search(r"^(\s*)\(setup\b", src, flags=re.M)
    if not m:
        return {"changed": False, "reason": "no (setup ...) block found in the board"}

    setup_indent = m.group(1)
    insert_at = m.end()
    block = "\n" + render_stackup_sexp(profile, indent=setup_indent + "\t")

    if backup:
        bak = board_path + ".bak-stackup"
        with open(bak, "w") as fh:
            fh.write(src)
        log("backup: %s" % bak)

    out = src[:insert_at] + block + src[insert_at:]
    with open(board_path, "w") as fh:
        fh.write(out)

    return {"changed": True, "profile": profile, "replaced_existing": replaced,
            "total_thickness_mm": total_thickness(profile),
            "backup": (board_path + ".bak-stackup") if backup else None}


# --------------------------------------------------------------------------
# Verifying netclasses against the stackup
# --------------------------------------------------------------------------

def project_for(board_path):
    return os.path.splitext(board_path)[0] + ".kicad_pro"


def read_netclasses(board_path):
    try:
        with open(project_for(board_path)) as fh:
            d = json.load(fh)
    except Exception:
        return [], []
    ns = d.get("net_settings", {})
    return ns.get("classes", []) or [], ns.get("netclass_patterns", []) or []


def check_netclasses(board_path, profile, target_se=50.0, target_diff=100.0,
                     tolerance=10.0):
    """Grade every netclass's geometry against the stackup.

    Assumes outer-layer (microstrip) routing, which is where controlled-impedance
    nets normally live and is the worse case for a thin trace. A net actually
    routed on an inner layer sees stripline and will read differently; the
    report says so rather than pretending otherwise.
    """
    h, er = outer_dielectric(profile)
    t = PROFILES[profile]["layers"][0]["thickness"]
    classes, patterns = read_netclasses(board_path)

    rows = []
    for c in classes:
        name = c.get("name")
        w = c.get("track_width")
        dw = c.get("diff_pair_width")
        dg = c.get("diff_pair_gap")
        row = {"netclass": name, "track_width": w,
               "diff_pair_width": dw, "diff_pair_gap": dg}
        if w:
            try:
                z = microstrip_z0(w, h, t, er)
                row["single_ended_z"] = round(z, 1)
                row["single_ended_error_pct"] = round(100 * (z - target_se) / target_se, 1)
                row["single_ended_ok"] = abs(z - target_se) / target_se * 100 <= tolerance
            except Exception as e:
                row["single_ended_z"] = None
                row["error"] = str(e)
        if dw and dg:
            try:
                zd = diff_microstrip_z(dw, dg, h, t, er)
                row["differential_z"] = round(zd, 1)
                row["differential_error_pct"] = round(
                    100 * (zd - target_diff) / target_diff, 1)
                row["differential_ok"] = abs(zd - target_diff) / target_diff * 100 <= tolerance
            except Exception as e:
                row["differential_z"] = None
                row["error"] = str(e)
        rows.append(row)

    w50 = solve_microstrip_width(target_se, h, t, er)
    wd, gd = solve_diff_microstrip(target_diff, h, t, er)
    return {
        "profile": profile,
        "dielectric_height_mm": h,
        "epsilon_r": er,
        "copper_thickness_mm": t,
        "target_single_ended": target_se,
        "target_differential": target_diff,
        "tolerance_pct": tolerance,
        "netclasses": rows,
        "patterns": patterns,
        "recommended": {
            "single_ended_width_mm": round(w50, 4) if w50 else None,
            "diff_pair_width_mm": round(wd, 4) if wd else None,
            "diff_pair_gap_mm": round(gd, 4) if gd else None,
        },
        "caveat": ("Closed-form IPC-2141 approximations, good to roughly "
                   "5-10%. Adequate to catch geometry that is nowhere near "
                   "target; NOT adequate to sign off a controlled-impedance "
                   "order. Ask the fab for their field-solver widths against "
                   "their actual laminate — those numbers win."),
    }


# --------------------------------------------------------------------------
# Verifying the ROUTED COPPER, not just the netclass
# --------------------------------------------------------------------------

RF_HINTS = ("RF", "ANT", "GNSS", "GPS", "UFL", "U_FL", "COAX", "LNA")


def looks_rf(netname):
    """Nets whose NAME claims they carry RF. Naming is the only intent signal a
    board carries once the schematic is gone."""
    u = netname.upper()
    return any(h in u for h in RF_HINTS)


def check_traces(board_path, profile, nets=None, target_z=50.0, tolerance=10.0):
    """Grade the ACTUAL routed geometry, net by net.

    A netclass check is not enough. A netclass says what the router was told; a
    trace says what it did — and a net can be routed at the Default width while
    a careful ETH_100R class sits nearby looking correct. This walks the copper.

    Reports per net: the widths actually used, the layers crossed, total length,
    via count, and the impedance each distinct width implies. Vias are counted
    because every layer transition on an RF net is an impedance discontinuity
    no width calculation captures.
    """
    import pcbnew
    board = pcbnew.LoadBoard(board_path)
    h, er = outer_dielectric(profile)
    layers = PROFILES[profile]["layers"]
    t_outer = layers[0]["thickness"]
    core = next((l for l in layers if l["type"] == "core"), None)

    want = set(nets) if nets else None
    seg = {}
    vias = {}
    for tr in board.GetTracks():
        n = tr.GetNetname()
        if not n:
            continue
        if want is not None and n not in want:
            continue
        if want is None and not looks_rf(n):
            continue
        if tr.GetClass() == "PCB_VIA":
            vias[n] = vias.get(n, 0) + 1
            continue
        if tr.GetClass() != "PCB_TRACK":
            continue
        d = seg.setdefault(n, {"widths": {}, "layers": set(), "length": 0.0})
        w = round(pcbnew.ToMM(tr.GetWidth()), 4)
        ln = pcbnew.ToMM(tr.GetLength())
        lname = board.GetLayerName(tr.GetLayer())
        d["widths"][w] = d["widths"].get(w, 0.0) + ln
        d["layers"].add(lname)
        d["length"] += ln

    outer_names = {layers[0]["name"], layers[-1]["name"]}
    rows = []
    for n in sorted(seg):
        d = seg[n]
        on_inner = bool(d["layers"] - outer_names)
        widths = []
        for w, ln in sorted(d["widths"].items()):
            try:
                z_ms = microstrip_z0(w, h, t_outer, er)
            except Exception:
                z_ms = None
            z_sl = None
            if core:
                try:
                    z_sl = stripline_z0(w, core["thickness"],
                                        layers[2]["thickness"], core["epsilon_r"])
                except Exception:
                    pass
            widths.append({
                "width_mm": w,
                "length_mm": round(ln, 2),
                "microstrip_z": round(z_ms, 1) if z_ms else None,
                "stripline_z": round(z_sl, 1) if z_sl else None,
                "error_pct": round(100 * (z_ms - target_z) / target_z, 1) if z_ms else None,
                "ok": (abs(z_ms - target_z) / target_z * 100 <= tolerance) if z_ms else None,
            })
        rows.append({
            "net": n,
            "total_length_mm": round(d["length"], 2),
            "layers": sorted(d["layers"]),
            "crosses_inner_layers": on_inner,
            "vias": vias.get(n, 0),
            "widths": widths,
            "worst_error_pct": max((abs(w["error_pct"]) for w in widths
                                    if w["error_pct"] is not None), default=None),
        })

    ideal = solve_microstrip_width(target_z, h, t_outer, er)
    bad = [r for r in rows if r["worst_error_pct"] is not None
           and r["worst_error_pct"] > tolerance]
    return {
        "profile": profile,
        "target_z": target_z,
        "tolerance_pct": tolerance,
        "required_width_mm": round(ideal, 4) if ideal else None,
        "nets_checked": len(rows),
        "nets_off_target": len(bad),
        "nets": rows,
        "verdict": ("%d of %d net(s) are outside %.0f%% of %.0f ohm"
                    % (len(bad), len(rows), tolerance, target_z)) if bad
                   else "all checked nets are within tolerance",
    }


def check_reference_planes(board_path, plane_layers=None):
    """Are the 'plane' layers actually planes?

    This check exists because of a miss that made every impedance number on a
    board meaningless. The board declared 4-layer Sig/GND/PWR/Sig, and it DID
    have a GND pour on In1 and a +5V pour on In2 — so a zone-level check said
    'planes present, as intended'. But both inner layers also carried ~1000-1500
    mm of routing across 57 nets each. They were shared signal layers with pour
    filling the gaps, not reference planes.

    That distinction is the whole ballgame. Microstrip impedance is defined
    against a CONTINUOUS reference plane one dielectric away. A reference carved
    up by 57 nets' worth of traces is not continuous, so the impedance is not
    merely unverified — it is not controlled, and no trace width can fix it.
    Checking that a pour exists is not checking that a plane exists.
    """
    import pcbnew
    board = pcbnew.LoadBoard(board_path)

    if plane_layers is None:
        plane_layers = []
        for z in board.Zones():
            seq = z.GetLayerSet().Seq()
            for i in range(len(seq)):
                nm = board.GetLayerName(seq[i])
                if nm not in plane_layers:
                    plane_layers.append(nm)

    bbox = board.GetBoardEdgesBoundingBox()
    board_area = (pcbnew.ToMM(bbox.GetWidth()) * pcbnew.ToMM(bbox.GetHeight())) or None

    rows = []
    for name in plane_layers:
        lid = board.GetLayerID(name)
        if lid < 0:
            continue
        pour_net, area = None, 0.0
        for z in board.Zones():
            seq = z.GetLayerSet().Seq()
            if any(seq[i] == lid for i in range(len(seq))):
                pour_net = z.GetNetname()
                try:
                    area += z.GetFilledArea() / 1e12
                except Exception:
                    pass
        length, nets = 0.0, set()
        for t in board.GetTracks():
            if t.GetClass() == "PCB_TRACK" and t.GetLayer() == lid:
                length += pcbnew.ToMM(t.GetLength())
                nets.add(t.GetNetname())
        nets.discard(pour_net)
        coverage = (100.0 * area / board_area) if board_area else None
        # A layer is a usable reference if essentially nothing else routes on it.
        solid = len(nets) == 0
        rows.append({
            "layer": name,
            "pour_net": pour_net,
            "filled_area_mm2": round(area, 1),
            "coverage_pct": round(coverage, 1) if coverage else None,
            "foreign_routing_mm": round(length, 1),
            "foreign_nets": len(nets),
            "is_reference_plane": solid,
        })

    broken = [r for r in rows if r["pour_net"] and not r["is_reference_plane"]]
    return {
        "board_area_mm2": round(board_area, 0) if board_area else None,
        "layers": rows,
        "broken_planes": [r["layer"] for r in broken],
        "impedance_controllable": not broken,
        "verdict": (
            "NOT CONTROLLABLE — %s carr%s routing from other nets, so no "
            "continuous reference plane exists. Impedance cannot be set by trace "
            "width on this arrangement; the layer assignment has to change first."
            % (", ".join(r["layer"] for r in broken),
               "ies" if len(broken) == 1 else "y")
        ) if broken else
        "Reference planes are continuous; trace geometry determines impedance.",
    }


def solve_diff_options(target_z, h, t, er, gaps=None, min_feature=0.0889):
    """Manufacturable (width, gap) pairs that hit a differential target.

    A single answer is not useful when it lands under the fab's minimum feature.
    On a thin 6-layer outer dielectric, 100 ohm at gap=h wants ~0.10/0.10 mm,
    which is 3.9 mil — right at the edge of standard 3/3 mil process and a poor
    place to sit. Opening the gap lets the trace widen, which is both easier to
    fabricate and less sensitive to etch tolerance. This returns the options so
    the choice is visible instead of implicit.

    min_feature defaults to 3.5 mil (0.0889 mm), a common standard-process
    floor; anything below it is returned but flagged.
    """
    if gaps is None:
        gaps = [round(h * m, 4) for m in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)]
    out = []
    for g in gaps:
        w = _bisect(lambda w: diff_microstrip_z(w, g, h, t, er), target_z, 0.03, 5.0)
        if w is None:
            continue
        out.append({
            "gap_mm": round(g, 4),
            "width_mm": round(w, 4),
            "achieved_z": round(diff_microstrip_z(w, g, h, t, er), 1),
            "manufacturable": w >= min_feature and g >= min_feature,
            "min_feature_mm": min_feature,
        })
    return out


def enforce_reference_planes(board_path, profile, log=print):
    """Report what would have to move for a profile's reference layers to
    actually be reference planes.

    fluxplace can now DECLARE which layers a stackup intends as planes
    (`reference_layers` on each profile). This compares the intent against the
    copper and reports the gap: which nets are squatting on a plane layer and
    how much routing has to be relocated. It does not move anything — a
    reference plane is restored by re-routing, and re-routing is a placement
    decision, not a cleanup.
    """
    import pcbnew
    refs = PROFILES[profile].get("reference_layers") or []
    if not refs:
        return {"profile": profile, "reference_layers": [],
                "note": "profile declares no reference layers"}

    board = pcbnew.LoadBoard(board_path)
    rows = []
    for name in refs:
        lid = board.GetLayerID(name)
        if lid < 0:
            rows.append({"layer": name, "present_on_board": False})
            continue
        by_net = {}
        for t in board.GetTracks():
            if t.GetClass() == "PCB_TRACK" and t.GetLayer() == lid:
                n = t.GetNetname()
                by_net[n] = by_net.get(n, 0.0) + pcbnew.ToMM(t.GetLength())
        pour = None
        for z in board.Zones():
            seq = z.GetLayerSet().Seq()
            if any(seq[i] == lid for i in range(len(seq))):
                pour = z.GetNetname()
        by_net.pop(pour, None)
        rows.append({
            "layer": name,
            "present_on_board": True,
            "pour_net": pour,
            "squatting_nets": len(by_net),
            "routing_to_relocate_mm": round(sum(by_net.values()), 1),
            "worst_offenders": sorted(by_net.items(), key=lambda kv: -kv[1])[:8],
            "is_clean": not by_net,
        })
    dirty = [r for r in rows if r.get("present_on_board") and not r.get("is_clean")]
    return {
        "profile": profile,
        "reference_layers": refs,
        "layers": rows,
        "clean": not dirty,
        "verdict": ("%d reference layer(s) still carry routing; %.0f mm across "
                    "%d net(s) must be relocated before impedance is controlled"
                    % (len(dirty),
                       sum(r["routing_to_relocate_mm"] for r in dirty),
                       sum(r["squatting_nets"] for r in dirty))) if dirty
                   else "all declared reference layers are clean planes",
    }
