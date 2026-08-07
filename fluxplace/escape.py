"""Adaptive fine-pitch escape — the design-rule annealer.

fluxplace places a board and hands it to a router at the CONSERVATIVE rule (0.2 mm
track/space by default). Some boards then plateau: a handful of fine-pitch parts
(a 0.5 mm-pitch LQFP, a 2-row mezzanine) physically cannot escape at 0.2 mm no
matter how good the placement — it is geometry, not routing effort.

Instead of a human spotting that and hand-drawing local rules, this module does it:

  1. route at the current rule, read the UNROUTED nets (DRC),
  2. cluster the unrouted pads by part -> the parts that are stuck are the escape
     bottleneck (measured: on RAZOR-01 dig, 60 of ~100 unrouted pads sat on just
     U10 (LQFP-144) and J20 (LSHM-140 mezzanine)),
  3. drop a LOCAL rule area over each stuck part and step ONLY those zones down the
     ladder 0.20 -> 0.15 -> 0.125 -> 0.10 mm (the JLCPCB standard floor), bulk stays
     conservative,
  4. re-route the stalled nets and repeat until the board closes or the floor is hit.

The bulk board keeps its safe geometry; only the spots where physics demands it get
fine copper. `detect_escape_zones` and the rule/area emitters are pure and pcbnew-free
so they unit-test without KiCad; `apply_rule_areas` is the one pcbnew touch.
"""
import re
from collections import Counter


# the step-down ladder, mm — conservative first, JLCPCB 4/6-layer standard floor last
LADDER = [0.20, 0.15, 0.125, 0.10]


def _unrouted_by_ref(drc):
    """Count unrouted-endpoint incidences per refdes from a kicad-cli DRC json."""
    cnt = Counter()
    for u in drc.get("unconnected_items", []):
        for it in u.get("items", []):
            m = re.search(r"\bof (\S+) on\b", it.get("description", ""))
            if m:
                cnt[m.group(1)] += 1
    return cnt


def detect_escape_zones(parts, drc, min_unrouted=5, margin=1.5):
    """Which parts are the routing bottleneck. Cluster the DRC's unrouted pads by part;
    a part carrying >= `min_unrouted` unrouted endpoints is an escape zone. Returns
    [{ref, n, bbox=(x0,y0,x1,y1)}] sorted worst-first — the bbox (part footprint +
    `margin` mm) is where the local fine-pitch rule area goes. Pure: parts is the
    read_board dict, drc is parsed kicad-cli json."""
    cnt = _unrouted_by_ref(drc)
    zones = []
    for ref, n in cnt.items():
        if n < min_unrouted or ref not in parts:
            continue
        p = parts[ref]
        w, h = p.get("w", 1.0), p.get("h", 1.0)
        zones.append(dict(
            ref=ref, n=n,
            bbox=(p["x"] - w / 2 - margin, p["y"] - h / 2 - margin,
                  p["x"] + w / 2 + margin, p["y"] + h / 2 + margin)))
    return sorted(zones, key=lambda z: -z["n"])


def dru_text(zones, width_mm, clearance_mm):
    """The KiCad custom-rules (.kicad_dru) text: inside each escape area, relax the min
    track width and the min clearance to the fine-pitch value; the bulk board keeps its
    board-setup rule. Clearance is relaxed only where BOTH items sit in the same area,
    so a fine trace leaving the zone still meets full clearance to the bulk."""
    if not zones:
        return "(version 1)\n"
    names = [f"escape_{z['ref']}" for z in zones]
    inA = " || ".join(f"A.insideArea('{n}')" for n in names)
    both = " || ".join(f"(A.insideArea('{n}') && B.insideArea('{n}'))" for n in names)
    return (
        "(version 1)\n\n"
        f'(rule "finepitch_escape_width"\n'
        f'  (condition "{inA}")\n'
        f"  (constraint track_width (min {width_mm}mm)))\n\n"
        f'(rule "finepitch_escape_clearance"\n'
        f'  (condition "{both}")\n'
        f"  (constraint clearance (min {clearance_mm}mm)))\n\n"
        f'(rule "finepitch_escape_hole"\n'
        f'  (condition "{both}")\n'
        f"  (constraint hole_clearance (min {clearance_mm}mm)))\n")


def ladder_step(current_mm):
    """Next value down the fine-pitch ladder, or None at the floor."""
    for v in LADDER:
        if v < current_mm - 1e-9:
            return v
    return None


def apply_rule_areas(board, zones, layers=None):
    """Add a named KiCad Rule Area (keepout-free, name = escape_<ref>) over each zone's
    bbox so the .kicad_dru conditions can reference it. Idempotent: removes any prior
    fluxplace escape areas first. The one pcbnew-touching function."""
    import pcbnew
    def mm(v):
        return pcbnew.FromMM(float(v))
    # drop previous escape areas
    for z in list(board.Zones()):
        if z.GetIsRuleArea() and z.GetZoneName().startswith("escape_"):
            board.Remove(z)
    lset = pcbnew.LSET()
    for lname in (layers or ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]):
        lid = board.GetLayerID(lname)
        if lid >= 0:
            lset.AddLayer(lid)
    n = 0
    for z in zones:
        x0, y0, x1, y1 = z["bbox"]
        area = pcbnew.ZONE(board)
        area.SetIsRuleArea(True)
        # a rule area that constrains geometry, not a keepout: allow everything
        for setter in ("SetDoNotAllowTracks", "SetDoNotAllowVias", "SetDoNotAllowPads",
                       "SetDoNotAllowCopperPour", "SetDoNotAllowFootprints"):
            if hasattr(area, setter):
                getattr(area, setter)(False)
        area.SetLayerSet(lset)
        area.SetZoneName(f"escape_{z['ref']}")
        pts = pcbnew.SHAPE_LINE_CHAIN()
        for (px, py) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            pts.Append(mm(px), mm(py))
        pts.SetClosed(True)
        area.AddPolygon(pts)
        board.Add(area)
        n += 1
    return n
