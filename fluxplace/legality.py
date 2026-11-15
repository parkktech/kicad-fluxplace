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
  hole       a drilled hole (PTH, NPTH, mounting standoff) of one part inside another part's courtyard, closer than the
             board's hole clearance to another net's copper or closer than that to another hole. A hole goes through
             the whole board, so this holds across sides (a bottom-side standoff vs a top-side connector body);
             courtyard overlap stays per side. Severity follows the project's DRC rules (`ignore` = not a violation).
             `collisions` (the gate) and the placers use `Legal.hole_issues`: one code, so they cannot disagree.
"""
import fnmatch
import math
import re
from collections import defaultdict

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
        # never below the routing floor (the gate's rule): a pad pair the router must keep apart is a collision
        self.pclr = max(self.pclr, float((self.cons.get("route") or {}).get("min_clearance_mm", 0.0) or 0.0))
        self.hclr = float(B.get("hole_clearance", 0.25))
        sev = B.get("severities") or {}
        self.hole_sev = {k: str(sev.get(k, "error")).lower() for k in ("pth_inside_courtyard", "npth_inside_courtyard", "hole_clearance")}
        self.hh = GM.Hash(5.0)
        self._hrefs = {}
        for r, p in B["parts"].items():
            self.hash.add(r, p["court"], "x")
            self._add_pads(r, p["pads"])
            self._add_holes(r)

    @staticmethod
    def _rect(q):
        return (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"])

    def _add_pads(self, ref, pads):
        for k in [k for k in self.phash.r if k[0] == ref]:
            self.phash.remove(k)
        for i, q in enumerate(pads):
            if not q.get("np"):
                self.phash.add((ref, i), self._rect(q), "x")

    def _add_holes(self, ref):
        for k in [k for k in self.hh.r if k[0] == ref]:
            self.hh.remove(k)
        hs = GM.holes_of(self.B["parts"][ref])
        self._hrefs[ref] = hs
        for i, q in enumerate(hs):
            r = q["drill"] / 2
            self.hh.add((ref, i), (q["x"] - r, q["y"] - r, q["x"] + r, q["y"] + r), "x")

    def refresh(self, ref):
        self.hash.add(ref, self.B["parts"][ref]["court"], "x")
        self._add_pads(ref, self.B["parts"][ref]["pads"])
        self._add_holes(ref)

    def hole_issues(self, ref, court=None, pads=None):
        """[(other ref, kind, example)] for the holes of `ref` against every other part (courtyard of either side, copper of
        another net, other holes) AND every other part's holes against `ref`'s courtyard / copper, as is or at a
        hypothetical courtyard / pad list. kind = hole_in_courtyard | hole_clearance. Holes occupy both sides."""
        P = self.B["parts"]
        part = P[ref]
        pads_ = pads or part["pads"]
        court_ = court or part["court"]
        mine = GM.holes_of(part, pads_ if pads else None)
        if not pads and court_ != part["court"] and mine:        # a pad-less part (mounting hole): follow the courtyard
            dx, dy = (court_[0] + court_[2] - part["court"][0] - part["court"][2]) / 2, (court_[1] + court_[3] - part["court"][1] - part["court"][3]) / 2
            mine = [dict(h, x=h["x"] + dx, y=h["y"] + dy) for h in mine]
        out, hclr, sv = [], self.hclr, self.hole_sev
        okc = (lambda q: sv["npth_inside_courtyard" if q.get("np") else "pth_inside_courtyard"] != "ignore")
        name = lambda q: f"{'NPTH' if q.get('np') else 'PTH'} {q['ref']}.{q['num'] or '-'}"
        cu = [q for q in pads_ if not q.get("np") and q["layers"]]
        for h in mine:
            r = h["drill"] / 2
            hb = (h["x"] - r, h["y"] - r, h["x"] + r, h["y"] + r)
            hs = ([(h["x"], h["y"])], r)
            if okc(h):
                for o in self.hash.hits(hb, "x", -1e-3, ignore=(ref,)):
                    if frozenset((ref, o)) not in self.allow and P[o].get("court_layers"):
                        out.append((o, "hole_in_courtyard", f"{name(h)} in {o} courtyard"))
            if sv["hole_clearance"] == "ignore":
                continue
            for o, j in self.phash.hits(hb, "x", hclr, ignore=()):
                c = P[o]["pads"][j]
                if o == ref or frozenset((ref, o)) in self.allow or not c["layers"] or (h.get("net") and h["net"] == c["net"]):
                    continue
                g = GM.shape_gap(hs, GM.pad_shape(c))
                if g < hclr - 1e-6:
                    out.append((o, "hole_clearance", f"hole {ref}.{h['num'] or '-'} / {o}.{c['num']}[{c['net'] or '-'}] gap {g:.3f}"))
            for o, k in self.hh.hits(hb, "x", hclr, ignore=()):
                if o == ref or frozenset((ref, o)) in self.allow:
                    continue
                t = self._hrefs[o][k]
                g = math.hypot(h["x"] - t["x"], h["y"] - t["y"]) - r - t["drill"] / 2
                if g < hclr - 1e-6:
                    a, b = sorted(((ref, h["num"] or "-"), (o, t["num"] or "-")))
                    out.append((o, "hole_clearance", f"hole {a[0]}.{a[1]} / hole {b[0]}.{b[1]} gap {g:.3f}"))
        if part.get("court_layers"):
            for o, k in self.hh.hits(court_, "x", -1e-3, ignore=()):
                t = self._hrefs[o][k]
                if o != ref and frozenset((ref, o)) not in self.allow and okc(t):
                    out.append((o, "hole_in_courtyard", f"{name(t)} in {ref} courtyard"))
        if sv["hole_clearance"] != "ignore":
            for q in cu:
                qs = GM.pad_shape(q)
                qb = _bbox(qs[0], qs[1])
                for o, k in self.hh.hits(qb, "x", hclr, ignore=()):
                    t = self._hrefs[o][k]
                    if o == ref or frozenset((ref, o)) in self.allow or (t.get("net") and t["net"] == q["net"]):
                        continue
                    g = GM.shape_gap(([(t["x"], t["y"])], t["drill"] / 2), qs)
                    if g < hclr - 1e-6:
                        out.append((o, "hole_clearance", f"hole {o}.{t['num'] or '-'} / {ref}.{q['num']}[{q['net'] or '-'}] gap {g:.3f}"))
        return out

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
        for o in sorted({o for o, _k, _e in self.hole_issues(ref, court, pads)}):
            out.append(("hole", o))
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
        res = {"overlap": [], "outside": [], "forbidden": [], "no_tht": [], "pad_short": [], "hole": []}
        for ref in sorted(refs or self.B["parts"]):
            for kind, d in self.part_issues(ref):
                if kind in ("overlap", "hole"):
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
        res["hole"] = sorted(set(res["hole"]))
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


# ---- copper-collision gate: true pad shapes, grouped by footprint pair, severity + relevance ------------------------
# kind: (KiCad DRC rule whose project severity sets the level, relevance, hard = always FAIL, text)
KINDS = {
    "copper_overlap":    ("shorting_items", "electrical", True, "copper of different nets overlaps"),
    "copper_clearance":  ("clearance", "manufacturing", False, "copper of different nets inside the clearance"),
    "keepout":           ("items_not_allowed", "placement", True, "inside a rule area that forbids it"),
    "keepout_trapped":   ("items_not_allowed", "routing", True, "connected pad inside a track-forbidding rule area"),
    "keepout_own":       ("items_not_allowed", "manufacturing", False, "the footprint's own hole inside its own pad-forbidding rule area"),
    "courtyard_overlap": ("courtyards_overlap", "placement", False, "courtyards overlap on the same side"),
    "hole_in_courtyard": ("pth_inside_courtyard", "placement", False, "a hole inside another part's courtyard"),
    "hole_clearance":    ("hole_clearance", "manufacturing", False, "a hole closer than the hole clearance to another part's copper"),
    "pour_island":       ("isolated_copper", "electrical", False, "a filled pour piece touches nothing of its net"),
    "unnetted_overlap":  ("shorting_items", "electrical", False, "unnetted copper overlaps"),
}
RELEVANCE = ("placement", "routing", "manufacturing", "electrical")
_LEVEL = {"error": "FAIL", "warning": "WARN", "ignore": "INFO"}
_RANK = {"FAIL": 0, "WARN": 1, "INFO": 2}


def _kind_level(kind, sev):
    rule, _rel, hard, _t = KINDS[kind]
    if hard:
        return "FAIL"
    return _LEVEL.get((sev or {}).get(rule, "error"), "FAIL")


def _hole_shape(q):
    return [(q["x"], q["y"])], q["drill"] / 2


def _poly_hits_shape(pts, shape, margin=1e-6):
    """Does a (possibly concave) polygon touch a pad shape (core, r) closer than `margin`?"""
    core, r = shape
    return GM.core_gap(core, pts) - r < margin


def _bbox(pts, r=0.0):
    return (min(p[0] for p in pts) - r, min(p[1] for p in pts) - r, max(p[0] for p in pts) + r, max(p[1] for p in pts) + r)


def collisions(B, cons=None, relevance=None):
    """Fast geometric pre-route collision check (no DRC run). -> [finding] grouped by footprint pair:
    {level FAIL|WARN|INFO, code COLLISION, pair, kinds {kind: n}, relevance [tags], examples, msg, refs}.

    Pads use their true shapes (circle / oval / roundrect / rect, rotated). Pads of the same footprint are never
    tested against each other (a part's multi-piece pins sharing a number are one part's copper). Levels follow the
    board project's DRC severities (`rule_severities`), except a different-net copper overlap and a keep-out
    violation, which always FAIL. `relevance` = an iterable of tags to keep (placement / routing / manufacturing /
    electrical); None keeps all. [placement] pad_clearance_mm overrides the board clearance (as for PLACEMENT_ILLEGAL)."""
    cons = cons or {}
    sev = B.get("severities") or {}
    clr = float((cons.get("placement") or {}).get("pad_clearance_mm", B.get("clearance", 0.1)))
    # never below the routing floor: a pad pair the router must keep apart (`[route] min_clearance_mm`) is a collision
    clr = max(clr, float((cons.get("route") or {}).get("min_clearance_mm", 0.0) or 0.0))
    hclr = float(B.get("hole_clearance", 0.25))
    P = B["parts"]
    hits = defaultdict(lambda: defaultdict(list))         # key -> kind -> [example]

    def hit(key, kind, ex):
        hits[key][kind].append(ex)

    def pair(a, b):
        return tuple(sorted((a, b)))

    cu = [q for q in B["pads"] if not q.get("np") and q["layers"]]
    shp = {id(q): GM.pad_shape(q) for q in cu}
    H = GM.Hash(2.0)
    for i, q in enumerate(cu):
        H.add(i, _bbox(shp[id(q)][0], shp[id(q)][1]), "x")
    pad_by_ref = defaultdict(list)
    for i, q in enumerate(cu):
        pad_by_ref[q["ref"]].append(i)

    # 1. pad-to-pad copper
    reach = max(clr, hclr)
    for i, q in enumerate(cu):
        s = shp[id(q)]
        for j in H.hits(_bbox(s[0], s[1]), "x", reach):
            if j <= i:
                continue
            o = cu[j]
            if o["ref"] == q["ref"] or (q["net"] and q["net"] == o["net"]) or not (q["layers"] & o["layers"]):
                continue
            g = GM.shape_gap(s, shp[id(o)])
            ex = f"{q['ref']}.{q['num']}[{q['net'] or '-'}]/{o['ref']}.{o['num']}[{o['net'] or '-'}] gap {g:.3f}"
            if g <= 1e-6 and not q["net"] and not o["net"]:
                hit(pair(q["ref"], o["ref"]), "unnetted_overlap", ex)     # neither pad has a net: nothing shorts
            elif g <= 1e-6:
                hit(pair(q["ref"], o["ref"]), "copper_overlap", ex)
            elif g < clr - 1e-6:
                hit(pair(q["ref"], o["ref"]), "copper_clearance", ex)

    # 2. holes (both sides): the placer's own code, Legal.hole_issues (one model for the gate and the placers)
    L = Legal(B, cons)
    seen = set()
    for ref in sorted(P):
        for o, kind, ex in L.hole_issues(ref):
            if (kind, ex) not in seen:
                seen.add((kind, ex))
                hit(pair(ref, o), kind, ex)

    # 3. rule areas (KiCad keep-outs; footprint-owned ones exempt their owner and the part they are named after)
    allpads = [q for q in B["pads"] if q["layers"] or q.get("np")] + [h for p in P.values() for h in p.get("holes", ())]
    for z in B["zones"]:
        if not (z["keepout"] and len(z["pts"]) > 2 and (z.get("no_pads") or z.get("no_footprints") or z["no_tracks"])):
            continue
        zb = _bbox(z["pts"])
        area = f"{z.get('owner') + ' ' if z.get('owner') else ''}rule area{(' ' + z['name']) if z['name'] else ''}".strip()
        for q in allpads:
            if not (q["np"] or q["layers"] & z["layers"]):
                continue
            s = _hole_shape(q) if q.get("np") else shp.get(id(q)) or GM.pad_shape(q)
            if not GM.overlap(_bbox(s[0], s[1]), zb) or not _poly_hits_shape(z["pts"], s):
                continue
            own = z.get("owner") == q["ref"] or (q["ref"].upper() in (z["name"] or "").upper())
            key = (q["ref"], "@" + area)
            if own:
                if q.get("np") and z.get("no_pads"):
                    hit(key, "keepout_own", f"NPTH {q['ref']} in its own {area}")
                continue
            if z.get("no_pads"):
                hit(key, "keepout", f"pad {q['ref']}.{q['num'] or '-'}[{q['net'] or '-'}] in {area} (pads not allowed)")
            elif z["no_tracks"] and q["net"] and not q.get("np"):
                hit(key, "keepout_trapped", f"pad {q['ref']}.{q['num']}[{q['net']}] in {area} (tracks not allowed)")
        if z.get("no_footprints"):
            for ref, p in P.items():
                if ref == z.get("owner") or ref.upper() in (z["name"] or "").upper():
                    continue
                c = p["court"]
                cp = [(c[0], c[1]), (c[2], c[1]), (c[2], c[3]), (c[0], c[3])]
                if GM.overlap(c, zb) and _poly_hits_shape(z["pts"], (cp, 0.0)):
                    hit((ref, "@" + area), "keepout", f"{ref} courtyard in {area} (footprints not allowed)")

    # 4. constraint keep-outs and courtyard overlaps: the PLACEMENT_ILLEGAL model, re-keyed by pair
    res = L.issues()
    for x in res["overlap"]:
        a, b = x.split("/")
        hit(pair(a, b), "courtyard_overlap", f"{a}/{b} courtyards")
    for x in res["forbidden"]:
        ref, name = x.split(" in ", 1)
        if name == "rule area" or any(name == (z["name"] or "rule area") for z in B["zones"] if z["keepout"]):
            continue                                       # rule areas: handled above on true shapes
        hit((ref, "@" + name), "keepout", f"{ref} in keep-out {name}")

    # 5. pour islands: a filled piece touching no pad / via / track end of its net (needs filled zones in the file).
    # Candidates come from spatial hashes built once per board (not a scan of every pad/via/segment per piece).
    pours = [z for z in B["zones"] if not z["keepout"] and z["net"] and z.get("fills")]
    if pours:
        pad_h, via_h, seg_h = GM.Hash(2.0), GM.Hash(2.0), GM.Hash(2.0)
        for i, q in enumerate(B["pads"]):
            r = max(q["ex"], q["ey"])
            pad_h.add(i, (q["x"] - r, q["y"] - r, q["x"] + r, q["y"] + r), "x")
        for i, v in enumerate(B["vias"]):
            r = v["d"] / 2
            via_h.add(i, (v["x"] - r, v["y"] - r, v["x"] + r, v["y"] + r), "x")
        for i, sg in enumerate(B["segs"]):
            seg_h.add(i, _bbox([sg["a"], sg["b"]], sg["w"] / 2), "x")
    for z in pours:
        for layer, pts in z["fills"]:
            if len(pts) < 3:
                continue
            fb = _bbox(pts)
            ok = False
            for i in pad_h.hits(fb, "x"):
                q = B["pads"][i]
                if q["net"] == z["net"] and layer in q["layers"]:
                    s_ = shp.get(id(q)) or GM.pad_shape(q)
                    if _poly_hits_shape(pts, s_, 0.01):
                        ok = True
                        break
            if not ok:                                   # vias / tracks: true shapes (they sit in thermal cut-outs)
                ok = any(B["vias"][i]["net"] == z["net"] and
                         _poly_hits_shape(pts, ([(B["vias"][i]["x"], B["vias"][i]["y"])], B["vias"][i]["d"] / 2), 0.01)
                         for i in via_h.hits(fb, "x"))
            if not ok:
                ok = any(B["segs"][i]["net"] == z["net"] and B["segs"][i]["layer"] == layer and
                         _poly_hits_shape(pts, ([B["segs"][i]["a"], B["segs"][i]["b"]], B["segs"][i]["w"] / 2), 0.01)
                         for i in seg_h.hits(fb, "x"))
            if not ok:
                hit((f"{z['net']}@{layer}",), "pour_island",
                    f"{z['net']} fill piece on {layer} near ({(fb[0] + fb[2]) / 2:.1f}, {(fb[1] + fb[3]) / 2:.1f})")

    out = []
    # a pour island is a WARN unless the constraints say `[review] pour_island = "fail"` (project severity ignored);
    # an unnetted-pad overlap is a WARN too (no net on either side, so nothing shorts)
    pour_fail = str(((cons.get("review") or {}).get("pour_island", ""))).lower() == "fail"
    for key, kinds in hits.items():
        lv = {k: _kind_level(k, sev) for k in kinds}
        if "pour_island" in lv and not pour_fail:
            lv["pour_island"] = "WARN"
        if "unnetted_overlap" in lv:
            lv["unnetted_overlap"] = "WARN"
        if kinds.keys() & {"hole_in_courtyard"}:
            npth = all(e.startswith("NPTH") for e in kinds["hole_in_courtyard"])
            lv["hole_in_courtyard"] = _LEVEL.get(sev.get("npth_inside_courtyard" if npth else "pth_inside_courtyard", "error"), "FAIL")
        rel = sorted({KINDS[k][1] for k in kinds}, key=RELEVANCE.index)
        if relevance is not None and not (set(rel) & set(relevance)):
            continue
        level = min(lv.values(), key=_RANK.get)
        order = sorted(kinds, key=lambda k: (_RANK[lv[k]], k))
        refs = [k for k in key if not k.startswith("@") and "@" not in k] if len(key) else []
        name = " x ".join(k.lstrip("@") for k in key)
        msg = f"{name}: " + "; ".join(f"{KINDS[k][3]} x{len(kinds[k])} ({lv[k]}, {KINDS[k][1]})" for k in order)
        msg += " e.g. " + ", ".join(kinds[order[0]][:3])
        out.append({"level": level, "code": "COLLISION", "msg": msg, "refs": sorted(refs), "pair": list(key),
                    "kinds": {k: len(v) for k, v in kinds.items()}, "levels": lv, "relevance": rel,
                    "examples": {k: v[:6] for k, v in kinds.items()}, "value": sum(len(v) for v in kinds.values()),
                    "limit": 0, "cls": None})
    out.sort(key=lambda f: (_RANK[f["level"]], f["pair"]))
    return out


def collision_gate(B, cons=None):
    """-> (ok, findings): ok is False when any COLLISION finding is FAIL (route-ready, pipeline route stage)."""
    f = collisions(B, cons)
    return not any(x["level"] == "FAIL" for x in f), f


def format_collisions(findings, title="", top=None):
    from collections import Counter
    n = Counter(x["level"] for x in findings)
    L = [f"COLLISIONS {title}".rstrip() + f": {n['FAIL']} FAIL, {n['WARN']} WARN, {n['INFO']} INFO "
         f"({len(findings)} footprint pair(s)/area(s))"]
    for x in findings[:top] if top else findings:
        L.append(f"  {x['level']:4s} [{','.join(x['relevance'])}] {x['msg']}")
    return "\n".join(L)
