"""The project's copper rules, handed to the external router and re-checked after it.

The router (KiCadRoutingTools ``py_router``) only knows what it is told.  Run without them it put
vias inside pads and could descend below the fab floor.  Every place fluxplace builds a router
command calls `flags()` here, and `routeaudit.py` re-checks the result without trusting it.

Floors = the strictest of `[fab]` (min_track_mm, min_clearance_mm, min_via_dia_mm, min_drill_mm),
`[route]` (min_track_mm, min_clearance_mm, min_via_mm, min_drill_mm, hole_to_hole_mm) and the board's
`.kicad_pro` design rules (min_track_width, min_clearance, min_via_diameter, min_through_hole_diameter).

    [route]
    via_in_pad = false            # default false: vias keep `same_net_pad_clearance_mm` off every pad
    same_net_pad_clearance_mm = 0.2
    grid_mm = 0.05                # default 0.05 when any part has pitch <= 0.5 mm, else 0.1
    mode = "serial"               # "serial" | "parallel" (batchroute / routepar)
    workers = 8                   # parallel: CPU count / 2, max 16
"""
import json
import math
import os

from . import adaptive

DEFAULT_SNPC = 0.2


def _pro_rules(board):
    try:
        pro = os.path.splitext(board)[0] + ".kicad_pro"
        return json.load(open(pro))["board"]["design_settings"]["rules"]
    except (OSError, KeyError, ValueError, TypeError):
        return {}


def fine_pitch(B, limit=0.5):
    """True when any part has two pads closer than `limit` centre to centre (B = routediag.load)."""
    for part in B["parts"].values():
        ps = part["pads"]
        for i, p in enumerate(ps):
            for q in ps[i + 1:]:
                d = math.hypot(p["x"] - q["x"], p["y"] - q["y"])
                if 0.1 < d <= limit + 1e-6 and p["num"] != q["num"]:
                    return True
    return False


def rules(cons, board=None, track=None, clearance=None, via=0.45, drill=0.2, B=None):
    """-> the rule dict (floors, requested sizes, via_in_pad, grid, mode, workers, audit_drc)."""
    from . import constraints as C
    fab = C.fab_profile(cons) if (cons or {}).get("fab") is not None else {}
    r = dict((cons or {}).get("route") or {})
    pro = _pro_rules(board) if board else {}

    def floor(*vals, default=0.0):
        v = [float(x) for x in vals if x]
        return max(v) if v else default

    R = {"min_track": floor(fab.get("min_track_mm"), r.get("min_track_mm"), pro.get("min_track_width")),
         "min_clearance": floor(fab.get("min_clearance_mm"), r.get("min_clearance_mm"), pro.get("min_clearance")),
         "min_via": floor(fab.get("min_via_dia_mm"), r.get("min_via_mm"), pro.get("min_via_diameter")),
         "min_drill": floor(fab.get("min_drill_mm"), r.get("min_drill_mm"), pro.get("min_through_hole_diameter")),
         "hole_to_hole": floor(r.get("hole_to_hole_mm"), pro.get("min_hole_to_hole"), default=0.0)}
    R["track"] = max(float(track or 0.0), R["min_track"]) or 0.2
    R["clearance"] = max(float(clearance or 0.0), R["min_clearance"]) or 0.2
    R["via"] = max(float(via), R["min_via"])
    R["drill"] = max(float(drill), R["min_drill"])
    R["via_in_pad"] = bool(r.get("via_in_pad", False))
    R["snpc"] = float(r.get("same_net_pad_clearance_mm", DEFAULT_SNPC))
    if r.get("grid_mm") is not None:
        R["grid"] = float(r["grid_mm"])
    else:
        try:
            from . import routediag as RD
            B = B or RD.load(board)
            R["grid"] = 0.05 if fine_pitch(B) else 0.1
        except Exception:
            R["grid"] = 0.1
    R["mode"] = str(r.get("mode", "serial")).lower()
    cpu = os.cpu_count() or 2
    R["workers"] = max(1, min(16, int(r.get("workers", max(1, cpu // 2)))))
    R["audit_drc"] = bool(r.get("audit_drc", False))
    R["audit"] = bool(r.get("audit", True))
    return R


def write_overrides(path, R):
    """The `--fab-overrides` file (`key = value`): the exact floors the router must not descend below."""
    # the router's floor is what it may DESCEND to: the project floor, not the requested size
    lines = {}
    lines["clearance"] = R["min_clearance"] or None
    lines["track_width"] = R["min_track"] or None
    lines["via_diameter"] = R["min_via"] or None
    lines["via_drill"] = R["min_drill"] or None
    lines["hole_to_hole"] = R.get("hole_to_hole") or None
    lines = {k: v for k, v in lines.items() if v}
    if not lines:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        for k, v in lines.items():
            fh.write(f"{k} = {v:g}\n")
    return path


def flags(R, krt_dir, workdir, script="route.py"):
    """Router arguments carrying the project's rules (only those this checkout accepts)."""
    have = adaptive.krt_flags(krt_dir, script)
    out = []
    if "--fab-overrides" in have:
        fo = write_overrides(os.path.join(workdir, "router-fab.txt"), R)
        if fo:
            out += ["--fab-overrides", fo]
    if "--same-net-pad-clearance" in have:
        out += [f"--same-net-pad-clearance={-1 if R['via_in_pad'] else R['snpc']:g}"]
    if "--grid-step" in have:
        out += ["--grid-step", f"{R['grid']:g}"]
    return out


def size_args(R):
    return ["--track-width", f"{R['track']:g}", "--clearance", f"{R['clearance']:g}",
            "--via-size", f"{R['via']:g}", "--via-drill", f"{R['drill']:g}"]
