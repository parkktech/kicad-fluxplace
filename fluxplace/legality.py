"""Is this placement LEGAL? One model used by the gate (PLACEMENT_ILLEGAL), by
`clusters --apply` and by the placers (a candidate is accepted only when it adds no
violation). Pure text (routediag model).

Violations, per part (`Legal.part_issues`):

  overlap    courtyard overlaps another part's courtyard on the same side (a through-hole
             part counts on both). Intentional overlaps: `[placement] allow_overlap =
             [["H200", "MH1"], ...]` (unordered pairs).
  outside    courtyard leaves the real outline (rounded corners and cut-outs, Edge.Cuts) or
             comes closer than `[placement] edge_clearance_mm` (0.3) to it. Edge connectors
             (`[placement] edge_mounted = [...]`, every `[placement.connectors]` entry, and a
             J/P/CN/X part whose courtyard touches or crosses the outline) may overhang by
             their mating side only: that side of the courtyard is cut back to the pads, and
             the pads themselves must be inside.
  forbidden  part in a region it may not be in: `[placement.keepout.NAME] = {rect, side,
             allow, reason}` (no part of that side), `[[keepout]]`, and track-forbidding rule
             areas (a footprint's own rule areas and a part named in the area's name are exempt).
  no_tht     a through-hole pad inside `[placement.no_tht.NAME] = {rect, allow, reason}`.
"""
import fnmatch
import re

from . import geom as GM
from . import legalise as LG

_CONN = re.compile(r"^(J|P|CN|X)\d")
_MECH = ("MH", "MK", "FID", "TP")
EDGE_NEAR_MM = 1.5


def _pairs(cons):
    return {frozenset(p) for p in ((cons or {}).get("placement") or {}).get("allow_overlap", []) or []}


def no_tht_regions(cons):
    """[(name, rect, allow)] from [placement.no_tht.NAME]."""
    out = []
    for name, k in sorted((((cons or {}).get("placement") or {}).get("no_tht") or {}).items()):
        if isinstance(k, dict) and k.get("rect"):
            out.append((name, tuple(float(v) for v in k["rect"]), list(k.get("allow", []))))
    return out


def edge_mounted(B, cons=None):
    """{ref: world opening unit vector | None} for parts allowed to overhang the outline by
    their mating side."""
    from . import placechecks as PCK
    pl = (cons or {}).get("placement") or {}
    refs = set(pl.get("edge_mounted", []) or []) | set((pl.get("connectors") or {}))
    bx0, by0, bx1, by1 = B["bbox"]
    for ref, p in B["parts"].items():
        if _CONN.match(ref) and p["n_pads"] >= 2:
            c = p["court"]
            if min(c[0] - bx0, c[1] - by0, bx1 - c[2], by1 - c[3]) < EDGE_NEAR_MM and (
                    not GM.in_outline(B, c, 0.0)):
                refs.add(ref)
    out = {}
    for ref in sorted(refs & set(B["parts"])):
        try:
            d, _how = PCK.opening(B, ref, cons)
        except Exception:
            d = None
        out[ref] = d
    return out


class Legal:
    def __init__(self, B, cons=None, fixed=()):
        self.B, self.cons = B, cons or {}
        self.M = LG._Model(B, self.cons, fixed, B["bbox"])
        self.allow = _pairs(self.cons)
        self.edge = edge_mounted(B, self.cons)
        self.tht_regions = no_tht_regions(self.cons)
        self.hash = GM.Hash(5.0)
        for r, p in B["parts"].items():
            self.hash.add(r, p["court"], "x")

    def refresh(self, ref):
        self.hash.add(ref, self.B["parts"][ref]["court"], "x")

    def eff_court(self, ref, court, pads):
        """Courtyard used for the outline test: an edge part's mating side is cut back to its pads."""
        if ref not in self.edge:
            return court
        d = self.edge[ref]
        pb = (min(q["x"] - q["ex"] for q in pads), min(q["y"] - q["ey"] for q in pads),
              max(q["x"] + q["ex"] for q in pads), max(q["y"] + q["ey"] for q in pads))
        if d is None:
            return pb
        c = list(court)
        if abs(d[0]) > abs(d[1]):
            if d[0] > 0:
                c[2] = min(c[2], pb[2])
            else:
                c[0] = max(c[0], pb[0])
        elif d[1] > 0:
            c[3] = min(c[3], pb[3])
        else:
            c[1] = max(c[1], pb[1])
        return tuple(c)

    def part_issues(self, ref, court=None, pads=None):
        """[(kind, detail)] for `ref`, as is or at a hypothetical courtyard / pad list."""
        B = self.B
        part = B["parts"][ref]
        court = court or part["court"]
        pads = pads or part["pads"]
        out = []
        sd = self.M.sides(ref)
        for k in self.hash.hits(court, "x", -0.001, ignore=(ref,)):
            if frozenset((ref, k)) in self.allow or not (sd & self.M.sides(k)):
                continue
            out.append(("overlap", k))
        if not GM.in_outline(B, self.eff_court(ref, court, pads), self.M.em):
            out.append(("outside", ""))
        elif ref in self.edge and not all(GM.in_outline(B, (q["x"] - q["ex"], q["y"] - q["ey"],
                                                           q["x"] + q["ex"], q["y"] + q["ey"]), 0.0) for q in pads):
            out.append(("outside", "pads"))
        for name, rect in self.M.forbidden_named(ref):
            if GM.overlap(court, rect, -0.001):
                out.append(("forbidden", name))
        for name, rect, allow in self.tht_regions:
            if any(fnmatch.fnmatch(ref, g) for g in allow):
                continue
            if any(q["tht"] and GM.overlap((q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"]),
                                           rect, -0.001) for q in pads):
                out.append(("no_tht", name))
        return out

    def issues(self, refs=None):
        """{kind: [description]} over `refs` (default every part). Overlap pairs listed once."""
        res = {"overlap": [], "outside": [], "forbidden": [], "no_tht": []}
        for ref in sorted(refs or self.B["parts"]):
            for kind, d in self.part_issues(ref):
                if kind == "overlap":
                    if refs is None and d < ref:
                        continue
                    res[kind].append(f"{ref}/{d}" if ref < d else f"{d}/{ref}")
                elif kind == "outside":
                    res[kind].append(ref + (" (pads)" if d else ""))
                else:
                    res[kind].append(f"{ref} in {d}")
        res["overlap"] = sorted(set(res["overlap"]))
        return res


def count(res):
    return sum(len(v) for v in res.values())


def baseline(B, cons=None, fixed=()):
    """Set of (kind, ref-ish) violations present on the board as it is (to tell what a
    move ADDED)."""
    L = Legal(B, cons, fixed)
    return L, L.issues()


def findings(B, cons=None):
    """PLACEMENT_ILLEGAL (FAIL): one finding per violation kind."""
    from .preroute import _f
    L = Legal(B, cons)
    res = L.issues()
    out = []
    why = {"overlap": "courtyard overlap on the same side", "outside": "outside the board outline",
           "forbidden": "in a forbidden region", "no_tht": "through-hole pad in a no-through-hole region"}
    for kind in ("overlap", "outside", "forbidden", "no_tht"):
        v = res[kind]
        if v:
            refs = sorted({x.replace("/", " ").split(" ")[i] for x in v for i in (0,) if x} |
                          {x.split("/")[1] for x in v if "/" in x})
            out.append(_f("FAIL", "PLACEMENT_ILLEGAL",
                          f"{len(v)} {why[kind]}: {', '.join(v[:8])}{'...' if len(v) > 8 else ''}",
                          refs=refs, value=len(v), limit=0))
    return out
