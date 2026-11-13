"""Is this placement LEGAL? One model used by the gate (PLACEMENT_ILLEGAL), by
`clusters --apply` and by the placers (a candidate is accepted only when it adds no
violation). Pure text (routediag model).

Side rule: a part occupies a side when it is through-hole, sits there, has a courtyard there or SMD
copper there (edge-launch connectors occupy both). A keep-out `side` applies to the part's BODY side
only (legalise._Model.body_side); mounting holes are never bodies.

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
  pad_short  a copper pad closer than the board clearance (`[placement] pad_clearance_mm`, default the
             board's own clearance) to a pad of another net on a shared copper layer; pads of the same
             net or the same part are exempt (KiCad `shorting_items` / `clearance`).
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
    from . import intent as IT
    refs |= IT.overhang_refs(B, cons)                        # --- intent: [[intent.edge]] overhang_mm
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
        self.phash = GM.Hash(2.0)
        self.pclr = float((self.cons.get("placement") or {}).get("pad_clearance_mm", B.get("clearance", 0.1)))
        for r, p in B["parts"].items():
            self.hash.add(r, p["court"], "x")
            self._add_pads(r, p["pads"])

    @staticmethod
    def _rect(q):
        return (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"])

    def _add_pads(self, ref, pads):
        for k in [k for k in self.phash.r if k[0] == ref]:
            self.phash.remove(k)
        for i, q in enumerate(pads):
            if not q.get("np"):
                self.phash.add((ref, i), self._rect(q), "x")

    def refresh(self, ref):
        self.hash.add(ref, self.B["parts"][ref]["court"], "x")
        self._add_pads(ref, self.B["parts"][ref]["pads"])

    def pad_shorts(self, ref, pads):
        """['REF.n/OTHER.m'] pads of `ref` (hypothetical list `pads`) too close to another net's pad."""
        P = self.B["parts"]
        out = set()
        for q in pads:
            if q.get("np"):
                continue
            for o, j in self.phash.hits(self._rect(q), "x", self.pclr, ignore=()):
                if o == ref:
                    continue
                r = P[o]["pads"][j]
                if (q["net"] and q["net"] == r["net"]) or not (set(q["layers"]) & set(r["layers"])):
                    continue
                if GM.rect_gap(self._rect(q), self._rect(r)) < self.pclr - 1e-9:
                    out.add(f"{ref}.{q['num']}/{o}.{r['num']}")
        return sorted(out)

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
        for d in self.pad_shorts(ref, pads):
            out.append(("pad_short", d))
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
        res = {"overlap": [], "outside": [], "forbidden": [], "no_tht": [], "pad_short": []}
        for ref in sorted(refs or self.B["parts"]):
            for kind, d in self.part_issues(ref):
                if kind == "overlap":
                    if refs is None and d < ref:
                        continue
                    res[kind].append(f"{ref}/{d}" if ref < d else f"{d}/{ref}")
                elif kind == "pad_short":
                    a, b = d.split("/")
                    res[kind].append(f"{a}/{b}" if a < b else f"{b}/{a}")
                elif kind == "outside":
                    res[kind].append(ref + (" (pads)" if d else ""))
                else:
                    res[kind].append(f"{ref} in {d}")
        res["overlap"] = sorted(set(res["overlap"]))
        res["pad_short"] = sorted(set(res["pad_short"]))
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
           "forbidden": "in a forbidden region", "no_tht": "through-hole pad in a no-through-hole region",
           "pad_short": "pad closer than the clearance to another net's pad"}
    for kind in ("overlap", "outside", "forbidden", "no_tht", "pad_short"):
        v = res[kind]
        if v:
            refs = sorted({x.replace("/", " ").split(" ")[0].split(".")[0] for x in v if x} |
                          {x.split("/")[1].split(".")[0] for x in v if "/" in x})
            out.append(_f("FAIL", "PLACEMENT_ILLEGAL",
                          f"{len(v)} {why[kind]}: {', '.join(v[:8])}{'...' if len(v) > 8 else ''}",
                          refs=refs, value=len(v), limit=0))
    return out


def issue_set(B, cons=None, fixed=()):
    """{"kind:detail"} over the whole board, from a FRESH model (no incremental state)."""
    return {f"{k}:{x}" for k, v in Legal(B, cons, fixed).issues().items() for x in v}


_REF = re.compile(r"[A-Za-z]+\d+[A-Za-z0-9_]*")


def _refs_in(items, parts):
    out = set()
    for s in items:
        for tok in re.split(r"[/ ]", s.split(":", 1)[1]):
            t = tok.split(".")[0]
            if t in parts:
                out.add(t)
    return out


def self_audit(B, cons, fixed, input_issues, origin, log=None):
    """Final proof before a result is written. `origin` = {ref: (x, y, angle, side)} of the INPUT board,
    `input_issues` = issue_set of the input. If the live model B has any violation (courtyard overlap,
    pad short, outside, forbidden, no_tht) the input did not have, the moved parts named in it go back
    to their input pose (repeated up to 3 times); anything still wrong sends EVERY part back.
    -> (reverted refs, whole_result_reverted). B is mutated; the caller recomputes its move list."""
    log = log or (lambda m: None)
    P = B["parts"]

    def back(refs):
        for r in refs:
            x, y, a, side = origin[r]
            if P[r]["side"] != side:
                GM.flip(P[r])
            GM.place(P[r], x, y, a)

    def moved(r):
        x, y, a, side = origin[r]
        p = P[r]
        return (P[r]["side"] != side or abs(p["pos"][0] - x) > 1e-4 or abs(p["pos"][1] - y) > 1e-4
                or abs((p["angle"] - a + 180) % 360 - 180) > 1e-6)

    reverted = set()
    for _ in range(3):
        new = sorted(issue_set(B, cons, fixed) - input_issues)
        if not new:
            return sorted(reverted), False
        bad = {r for r in _refs_in(new, P) if moved(r)}
        log(f"self-audit: {len(new)} violation(s) the input did not have ({', '.join(new[:4])}); "
            f"reverting {', '.join(sorted(bad)) or 'everything'}")
        if not bad:
            break
        back(bad)
        reverted |= bad
    everything = [r for r in P if moved(r)]
    back(everything)
    log(f"self-audit: result still illegal; reverted ALL {len(everything)} moved part(s) to the input")
    return sorted(set(everything) | reverted), True


def origin_of(B):
    return {r: (p["pos"][0], p["pos"][1], p["angle"], p["side"]) for r, p in B["parts"].items()}


def moves_since(B, origin):
    """{ref: (x, y, angle)} of the parts that differ from `origin` (origin_of)."""
    return {r: (round(p["pos"][0], 4), round(p["pos"][1], 4), p["angle"]) for r, p in B["parts"].items()
            if abs(p["pos"][0] - origin[r][0]) > 1e-4 or abs(p["pos"][1] - origin[r][1]) > 1e-4
            or abs((p["angle"] - origin[r][2] + 180) % 360 - 180) > 1e-6}
