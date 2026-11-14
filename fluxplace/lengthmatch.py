"""Length matching: `[match.NAME]` groups of nets that must have equal (or targeted) length.

  [match.DATA]
  nets          = ["DATA_\\d+"]          # regexes, fullmatch against net names
  target_mm     = 40.0                    # absolute target, OR
  reference     = "CLK"                   # the length of that net, OR neither: the longest member
  tolerance_mm  = 0.5                     # OR tolerance_ps (then target_ps / ps unit everywhere)
  include_vias  = true                    # add via barrel length (stackup thickness between the layers used)
  style         = "accordion"             # rectangular bumps; trombone/sawtooth are not implemented
  amplitude_mm  = 0.6                     # bump height limit used by tune --match
  spacing_mm    = 0.4                     # bump width; the copper between bumps is one more bump width

Length of a net = copper track length on every layer (+ via barrel length when include_vias).
ps = sum over layers of mm x that layer's propagation delay (impedance.route_delay_ps_per_mm).
Track length is the total copper of the net: router loops count (repair.redundant_copper removes them
in the pipeline before review/tune run).
"""
import re

from . import impedance as IM

__all__ = ["groups", "members", "layer_ps", "via_mm", "lengths_from_board", "evaluate", "STYLES"]

STYLES = ("accordion",)
_DEFAULT_AMP = 0.6


def groups(cons):
    """Validated [match.*] groups. Raises ValueError on an incomplete or contradictory entry."""
    out = []
    for name, g in sorted(((cons or {}).get("match") or {}).items()):
        if not isinstance(g, dict):
            raise ValueError(f"[match.{name}] must be a table")
        pats = g.get("nets")
        if not isinstance(pats, list) or not pats or not all(isinstance(p, str) for p in pats):
            raise ValueError(f"[match.{name}] nets must be a non-empty list of regex strings")
        for p in pats:
            re.compile(p)
        targets = [k for k in ("target_mm", "target_ps", "reference") if k in g]
        if len(targets) > 1:
            raise ValueError(f"[match.{name}] give at most one of target_mm / target_ps / reference")
        tols = [k for k in ("tolerance_mm", "tolerance_ps") if k in g]
        if len(tols) != 1:
            raise ValueError(f"[match.{name}] needs exactly one of tolerance_mm / tolerance_ps")
        unit = "ps" if tols[0] == "tolerance_ps" else "mm"
        if targets and targets[0] in ("target_mm", "target_ps") and targets[0] != f"target_{unit}":
            raise ValueError(f"[match.{name}] {targets[0]} does not match the tolerance unit ({unit})")
        style = g.get("style", "accordion")
        if style not in STYLES:
            raise ValueError(f"[match.{name}] style '{style}' not implemented (use one of {STYLES})")
        out.append({
            "name": name, "nets": pats, "unit": unit,
            "target": float(g[targets[0]]) if targets and targets[0] != "reference" else None,
            "reference": g.get("reference") if targets and targets[0] == "reference" else None,
            "tol": float(g[tols[0]]),
            "include_vias": bool(g.get("include_vias", False)),
            "style": style,
            "amplitude_mm": float(g.get("amplitude_mm", _DEFAULT_AMP)),
            "spacing_mm": float(g["spacing_mm"]) if "spacing_mm" in g else None,
        })
    return out


def members(group, net_names):
    """Sorted net names matched by any of the group's regexes (fullmatch)."""
    pats = [re.compile(p) for p in group["nets"]]
    return sorted(n for n in net_names if n and any(p.fullmatch(n) for p in pats))


def layer_ps(stackup, planes):
    """{copper layer name: ps/mm} for every layer with a known delay (others absent)."""
    out = {}
    for r in stackup or []:
        if r.get("type") == "copper" and r.get("name"):
            d = IM.route_delay_ps_per_mm(stackup, set(planes or ()), {r["name"]: 1.0})
            if d:
                out[r["name"]] = d
    return out


def via_mm(stackup, top, bottom):
    """Barrel length between two copper layers: stackup thickness from `top` to `bottom` inclusive.
    None when either layer is not in the stackup."""
    names = [r.get("name") for r in stackup or []]
    if top not in names or bottom not in names:
        return None
    i, j = sorted((names.index(top), names.index(bottom)))
    return sum(float(r.get("thickness") or 0.0) for r in stackup[i:j + 1])


def lengths_from_board(board, nets, stackup, planes, include_vias=False):
    """{net: {"mm", "ps" (None when a layer delay is unknown), "by_layer", "vias", "via_mm"}} from a loaded
    pcbnew board. Nets with no copper are present with zeros."""
    lp = layer_ps(stackup, planes)
    want = set(nets)
    acc = {n: {"by_layer": {}, "vias": 0, "via_list": []} for n in want}
    for t in board.GetTracks():
        n = t.GetNetname()
        if n not in want:
            continue
        if t.GetClass() == "PCB_VIA":
            acc[n]["vias"] += 1
            acc[n]["via_list"].append((board.GetLayerName(t.TopLayer()), board.GetLayerName(t.BottomLayer())))
        elif t.GetClass() == "PCB_TRACK":
            lyr = board.GetLayerName(t.GetLayer())
            acc[n]["by_layer"][lyr] = acc[n]["by_layer"].get(lyr, 0.0) + t.GetLength() / 1e6
    out = {}
    for n, a in acc.items():
        mm = sum(a["by_layer"].values())
        unknown = [l for l in a["by_layer"] if l not in lp]
        ps = None if unknown else sum(v * lp[l] for l, v in a["by_layer"].items())
        vmm = 0.0
        for top, bot in a["via_list"]:
            v = via_mm(stackup, top, bot) if include_vias else None
            if include_vias and v is None:
                unknown.append(f"via {top}-{bot}")
            vmm += v or 0.0
        if include_vias and vmm and ps is not None and mm > 0:
            ps += vmm * ps / mm                       # via barrel at the net's average delay
        if include_vias:
            mm += vmm
        out[n] = {"mm": mm, "ps": None if (unknown or ps is None) else ps,
                  "by_layer": a["by_layer"], "vias": a["vias"], "via_mm": vmm,
                  "unknown": sorted(set(unknown))}
    return out


def evaluate(group, lens, reference_len=None):
    """Judge one group. `lens` = lengths_from_board output restricted to the members.
    Returns dict(unit, target, tol, rows=[(net, value, delta, ok)], ok) in the group's unit.
    reference_len: the reference net's value in the unit (when group['reference'] is set)."""
    unit = group["unit"]
    vals = {n: (v["ps"] if unit == "ps" else v["mm"]) for n, v in lens.items()}
    if group["target"] is not None:
        target = group["target"]
    elif group["reference"]:
        target = reference_len
    else:
        target = max(vals.values()) if vals else 0.0
    rows = []
    for n in sorted(vals):
        v = vals[n]
        if v is None:
            rows.append((n, None, None, False))
            continue
        d = v - target
        rows.append((n, v, d, abs(d) <= group["tol"] + 1e-9))
    return {"unit": unit, "target": target, "tol": group["tol"], "rows": rows,
            "ok": all(r[3] for r in rows)}
