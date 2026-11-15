"""Pinned-part conflict resolution: runs BEFORE any placement stage (`floorplan`), fast and deterministic.

A part is PINNED when the placer may not move it: `[lock.at]`, locked footprints, `[placement] fixed` / `anchors`, mounting
hardware and `[placement.group]` centres (modules.fixed_set), minus `[floorplan] movable`. Two pinned parts can collide with
each other (the pins were written at different times: a `fixed` part keeps its input-board position, a `[lock.at]` row
puts another part on top of it; a bottom standoff's hole lands in a top connector's courtyard). The placer cannot fix a
collision between parts it must not move, so this step decides which pin yields. The test is `legality.Legal.part_issues`
(courtyard overlap, pad short, hole through the board): the placer's own legality, nothing else.

Precedence (lower rank wins; the loser's WHOLE rigid unit yields):

  0  explicit coordinate   `[lock.at]`, locked footprint
  1  hard intent           a pinned part named by a hard declared `[[intent.edge]]`
  2  fixed                 `[placement] fixed` / `anchors` (inherits the input board's position)
  3  hardware / group      mounting hardware, group centres
  A rigid unit (`[[intent.group]]` with `rigid` (default true), `[placement.group]`) moves as ONE body: its rank is the best
  rank of its members (its anchor); a stand-off follows the socket it belongs to.

The yielding unit is released from its pin (removed from `fixed` / `anchors` / `[lock.at]`, added to `[floorplan] movable`)
and moved to the NEAREST legal pose (same rotation and side; every hard intent rule kept: edge side, `near`, `away`, regions,
keep-outs) among the parts that stay; the floorplan then starts from that pose and a `stay` term in its cost keeps it there
(`[placement] pin_stay_weight`, default 5 per mm). Same rank on both sides:
  * rank 0 (two explicit coordinates): the step cannot choose. It stops with PIN_CONFLICT (both units, the overlap, the three
    smallest legal moves of each) unless `[placement] resolve_pin_conflicts = "auto"`: then the unit with the smaller required
    displacement moves (tie: the later ref) and the decision says so;
  * rank 1..3: the unit with the smaller required displacement moves (tie: the later ref).
Every decision is `{unit, anchor, from, to, distance, why, rule, winner}`; review reports each as PIN_RELEASED (WARN) from the
`<board>.pins.json` that travels with the board; make it permanent by updating the lock.
"""
import copy
import json
import math
import os
import shutil

from . import geom as GM

EDGE_SLIDE_MM = 25.0
RANK_NAME = {0: "explicit coordinate ([lock.at] / locked)", 1: "hard intent", 2: "fixed", 3: "hardware / group"}
RULE = {0: "lock.at > hard intent > fixed > hardware", 1: "lock.at > hard intent > fixed > hardware"}


def settings(cons):
    pl = (cons or {}).get("placement") or {}
    mode = str(pl.get("resolve_pin_conflicts", "stop")).lower()
    if mode not in ("stop", "auto"):
        raise SystemExit(f"[placement] resolve_pin_conflicts = {mode!r}: use \"stop\" or \"auto\"")
    return mode, float(pl.get("pin_stay_weight", 5.0))


def ranks(B, cons):
    """{ref: (rank, name)} for every pinned part (modules.fixed_set), before units are considered."""
    from . import intent as IT, modules as MD
    cons = cons or {}
    pl = cons.get("placement") or {}
    held = MD.fixed_set(B, cons)
    lock = set(((cons.get("lock") or {}).get("at") or {}))
    named = set(pl.get("fixed", []) or []) | set(pl.get("anchors", []) or [])
    out = {}
    for r in sorted(held):
        if r in lock:
            out[r] = (0, "lock_at")
        elif B["parts"][r].get("locked"):
            out[r] = (0, "locked")
        elif r in named:
            out[r] = (2, "fixed")
        else:
            out[r] = (3, "hardware")
    for e in IT.items(B, cons)["edge"]:
        if e.get("hard") and e.get("src") == "declared":
            for r in e["refs"]:
                if r in out and out[r][0] > 1:
                    out[r] = (1, "hard_intent")
    return out


def units_of(B, cons):
    """{ref: tuple(sorted refs)}: refs that move as one rigid body (rigid intent groups, placement groups)."""
    from . import intent as IT
    groups = [list(g["refs"]) for g in IT.items(B, cons)["group"] if g["rigid"] and len(g["refs"]) > 1]
    groups += [[r for r in g["refs"] if r in B["parts"]] for g in (((cons or {}).get("placement") or {}).get("group") or {}).values()
               if isinstance(g, dict) and g.get("refs")]
    parent = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for g in groups:
        for r in g[1:]:
            parent[find(r)] = find(g[0])
    byroot = {}
    for r in list(parent):
        byroot.setdefault(find(r), []).append(r)
    out = {}
    for refs in byroot.values():
        t = tuple(sorted(refs))
        for r in t:
            out[r] = t
    return out


def _unit(ref, unit_of):
    return unit_of.get(ref, (ref,))


def pinned_units(B, cons):
    """-> ({unit tuple: rank}, {ref: unit}, {ref: (rank, name)}). A unit with a pinned member is pinned as a whole at its best rank."""
    rk = ranks(B, cons)
    unit_of = units_of(B, cons)
    out = {}
    for r in sorted(rk):
        u = _unit(r, unit_of)
        out[u] = min(out.get(u, 9), rk[r][0])
    return out, unit_of, rk


def conflicts(B, cons, units, unit_of, L=None):
    """{(unit_a, unit_b): [{kind, a, b, detail}]} for pinned units whose parts collide (courtyard, pad short, hole)."""
    from . import legality as LGL
    L = L or LGL.Legal(B, cons)
    pinned = {r for u in units for r in u}
    out = {}
    for r in sorted(pinned):
        ur = _unit(r, unit_of)
        ex = {}
        for o, kind, e in L.hole_issues(r):
            ex.setdefault(o, []).append(e)
        for kind, d in L.part_issues(r):
            if kind in ("overlap", "hole"):
                o = d
            elif kind == "pad_short":
                o = d.split("/")[1].rsplit(".", 1)[0]
            else:
                continue
            uo = _unit(o, unit_of)
            if o not in pinned or uo == ur:
                continue
            key = tuple(sorted((ur, uo)))
            c = B["parts"][r]["court"], B["parts"][o]["court"]
            rect = (max(c[0][0], c[1][0]), max(c[0][1], c[1][1]), min(c[0][2], c[1][2]), min(c[0][3], c[1][3]))
            rec = {"kind": kind, "a": r, "b": o, "detail": (ex.get(o) or [d])[0] if kind == "hole" else d,
                   "overlap_rect": [round(v, 3) for v in rect] if rect[2] > rect[0] and rect[3] > rect[1] else None}
            lst = out.setdefault(key, [])
            if not any((x["a"], x["b"], x["kind"]) in ((rec["a"], rec["b"], rec["kind"]), (rec["b"], rec["a"], rec["kind"])) for x in lst):
                lst.append(rec)
    return out


def _spiral(r_fine, step_fine, r_coarse, step_coarse):
    pts = {}
    n = int(r_fine / step_fine)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            d = math.hypot(i * step_fine, j * step_fine)
            if d <= r_fine + 1e-9:
                pts[(round(i * step_fine, 6), round(j * step_fine, 6))] = d
    n = int(r_coarse / step_coarse)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            d = math.hypot(i * step_coarse, j * step_coarse)
            if r_fine < d <= r_coarse + 1e-9:
                pts[(round(i * step_coarse, 6), round(j * step_coarse, 6))] = d
    return [(d, dx, dy) for (dx, dy), d in sorted(pts.items(), key=lambda kv: (round(kv[1], 6), kv[0]))]


class Resolver:
    def __init__(self, B, cons, spec, log=None):
        from . import intent as IT
        self.B, self.cons0, self.spec = B, cons, spec
        self.log = log or (lambda s: None)
        self.mode, self.stay = settings(cons)
        self.units, self.unit_of, self.rank = pinned_units(B, cons)
        w = copy.deepcopy(cons)                                  # the Seater works on its own copy (it adds `[intent]._front`)
        w.setdefault("lock", {})["at"] = {}
        w.setdefault("placement", {})["fixed"] = []
        w["placement"]["anchors"] = []
        self.S = IT.Seater(B, w, spec, log=None)
        self.orig = dict(self.S.orig)
        self.decisions, self.unresolved = [], []
        self.released = []

    # -- search
    def _members(self, unit):
        S = self.S
        refs = set(unit)
        for r in list(unit):
            refs |= set(S.unit(r))
        return sorted(refs)

    def _anchor(self, unit):
        P = self.B["parts"]
        return sorted(unit, key=lambda r: (-P[r]["n_pads"], r))[0]

    def _stand(self, unit):
        """the parts that stay while `unit` is searched: every pinned part, and every unit already re-seated."""
        S = self.S
        S.fixed0 = {r for u in self.units for r in u if u != unit} | set(self.released)
        S.held = set()
        S.refresh()

    def nearest(self, unit, k=1, limit=60000):
        """[(distance, dx, dy)] the k nearest legal poses (same rotation and side) for the unit; the board is left unchanged.
        A unit that sits against the outline (its anchor's courtyard within EDGE_NEAR_MM of an edge) first looks for a pose
        that keeps that gap, sliding along the edge (up to EDGE_SLIDE_MM); only when there is none, any legal pose."""
        from . import intent as IT, legality as LGL
        S = self.S
        self._stand(unit)
        mem = self._members(unit)
        anchor = self._anchor(unit)
        x0, y0 = self.B["parts"][anchor]["pos"]
        gaps = IT.edge_gaps(self.B, self.B["parts"][anchor]["court"])
        keep = [sd for sd, v in sorted(gaps.items()) if -2.0 <= v <= LGL.EDGE_NEAR_MM]
        cells = _spiral(40.0, 0.5, self.B["diag"], 2.0)
        passes = []
        if keep:
            along = lambda dx, dy: all((abs(dx) < 1e-9 if sd in ("left", "right") else abs(dy) < 1e-9) for sd in keep)
            passes.append([c for c in cells if along(c[1], c[2]) and c[0] <= EDGE_SLIDE_MM])
        passes.append(cells)
        for cand in passes:
            found = []
            for n, (d, dx, dy) in enumerate(cand):
                if n >= limit or len(found) >= k:
                    break
                if any(math.hypot(dx - fx, dy - fy) < 1.0 for _d, fx, fy in found):
                    continue
                poses = S.poses(mem, anchor, x0 + dx, y0 + dy, 0)
                if not S.legal(mem, poses, ghosts=True):
                    continue
                old = S.apply(poses)
                ok = S.intent_ok(mem)
                S.apply(old)
                if ok:
                    found.append((d, dx, dy))
            if found:
                return found
        return []

    def seat(self, unit, dx, dy):
        S = self.S
        mem = self._members(unit)
        anchor = self._anchor(unit)
        x0, y0 = self.B["parts"][anchor]["pos"]
        S.apply(S.poses(mem, anchor, x0 + dx, y0 + dy, 0))
        return mem

    # -- decisions
    def _pose(self, r):
        p = self.B["parts"][r]
        return [round(p["pos"][0], 4), round(p["pos"][1], 4), round(p["angle"], 4), "F" if p["side"] == "F.Cu" else "B"]

    def run(self):
        for _ in range(4 * max(1, len(self.units)) + 8):
            pairs = conflicts(self.B, self.S.cons, self.units, self.unit_of)
            if not pairs:
                break
            key = sorted(pairs)[0]
            ua, ub = key
            ra, rb = self.units[ua], self.units[ub]
            geom = pairs[key]
            if ra != rb:
                loser, winner, why = (ub, ua, "lower precedence") if ra < rb else (ua, ub, "lower precedence")
            else:
                oa, ob = self.nearest(ua, 3), self.nearest(ub, 3)
                if ra == 0 and self.mode != "auto":
                    self.unresolved.append({"a": list(ua), "b": list(ub), "rank": ra, "geometry": geom,
                                            "options": {self._anchor(ua): [self._opt(ua, o) for o in oa],
                                                        self._anchor(ub): [self._opt(ub, o) for o in ob]}})
                    return self
                da, db = (oa[0][0] if oa else 1e9), (ob[0][0] if ob else 1e9)
                if da != db:
                    loser, winner = (ua, ub) if da < db else (ub, ua)
                else:
                    loser, winner = (ua, ub) if self._anchor(ua) > self._anchor(ub) else (ub, ua)
                why = ("explicit coordinates on both: resolve_pin_conflicts = auto, smaller displacement moves" if ra == 0
                       else "same precedence, smaller displacement moves")
            opt = self.nearest(loser, 1)
            if not opt:
                self.unresolved.append({"a": list(ua), "b": list(ub), "rank": min(ra, rb), "geometry": geom, "options": {},
                                        "note": f"no legal pose for {self._anchor(loser)}"})
                return self
            d, dx, dy = opt[0]
            before = {r: self._pose(r) for r in self._members(loser)}
            mem = self.seat(loser, dx, dy)
            anchor = self._anchor(loser)
            self.decisions.append({
                "unit": sorted(mem), "anchor": anchor, "from": before[anchor], "to": self._pose(anchor),
                "distance": round(d, 3), "level": self.rank[anchor][1] if anchor in self.rank else "",
                "rank": self.units[loser], "winner": self._anchor(winner), "winner_rank": self.units[winner],
                "why": f"{why}: collides with {self._anchor(winner)} ({RANK_NAME[self.units[winner]]}); "
                       f"{anchor} is {RANK_NAME[self.units[loser]]}",
                "rule": RULE[0], "geometry": geom})
            self.released.extend(r for r in mem if r not in self.released)
            self.units.pop(loser)
        return self

    def _opt(self, unit, o):
        d, dx, dy = o
        x, y = self.B["parts"][self._anchor(unit)]["pos"]
        return {"to": [round(x + dx, 3), round(y + dy, 3)], "distance": round(d, 3)}

    @property
    def moves(self):
        return self.S.moves

    def cons_out(self):
        c = copy.deepcopy(self.cons0)
        rel = set(self.released)
        if not rel:
            return c
        pl = c.setdefault("placement", {})
        pl["fixed"] = [r for r in (pl.get("fixed") or []) if r not in rel]
        if pl.get("anchors"):
            pl["anchors"] = [r for r in pl["anchors"] if r not in rel]
        at = (c.get("lock") or {}).get("at")
        if at:
            for r in rel:
                at.pop(r, None)
        fp = c.setdefault("floorplan", {})
        fp["movable"] = sorted(set(fp.get("movable", []) or []) | rel)
        fp["stay"] = {"refs": sorted(rel), "weight": self.stay}
        return c


def report(R):
    """text lines for the stage log."""
    L = []
    for d in R.decisions:
        L.append(f"pins: PIN_RELEASED {d['anchor']}{' +' + str(len(d['unit']) - 1) if len(d['unit']) > 1 else ''} "
                 f"({d['from'][0]:g},{d['from'][1]:g}) -> ({d['to'][0]:g},{d['to'][1]:g}) {d['distance']:g} mm; {d['why']}")
    for u in R.unresolved:
        g = u["geometry"][0]
        L.append(f"pins: PIN_CONFLICT {','.join(u['a'])} x {','.join(u['b'])}: {g['kind']} {g['a']}/{g['b']}"
                 f"{' overlap ' + str(g['overlap_rect']) if g.get('overlap_rect') else ''} ({g['detail']}); {u.get('note', '')}")
        for ref, opts in sorted(u["options"].items()):
            L.append(f"  smallest legal moves of {ref}: " + "; ".join(f"-> {o['to']} ({o['distance']:g} mm)" for o in opts))
        L.append("  both parts are explicit coordinates: change one lock, or set [placement] resolve_pin_conflicts = \"auto\"")
    return L


def sidecar_path(board):
    return os.path.splitext(board)[0] + ".pins.json"


def write(path, R):
    json.dump({"decisions": R.decisions, "unresolved": R.unresolved}, open(path, "w"), indent=1, default=str)


def prepare(board, cons, spec, workdir, log=print, name="pins-resolved"):
    """-> (board path, constraints to run with, Resolver | None). Nothing happens when no two pinned parts collide."""
    from . import boardwrite as BW, routediag as RD
    B = RD.load(board)
    if not any(True for _ in B["parts"]):
        return board, cons, None
    R = Resolver(B, cons, spec)
    if not conflicts(B, R.S.cons, R.units, R.unit_of):
        return board, cons, None
    R.run()
    os.makedirs(workdir, exist_ok=True)
    for line in report(R):
        log(line)
    write(os.path.join(workdir, "pins.json"), R)
    if R.unresolved:
        return board, cons, R
    out = os.path.join(workdir, name + ".kicad_pcb")
    BW.move_file(board, out, R.moves, post=False)
    for ext in (".kicad_pro", ".fluxmap.json", ".assumed.json"):
        src = os.path.splitext(board)[0] + ext
        if os.path.exists(src):
            shutil.copy(src, os.path.splitext(out)[0] + ext)
    log(f"pins: {len(R.decisions)} pinned unit(s) released, {len(R.moves)} part(s) moved; {out}")
    return out, R.cons_out(), R
