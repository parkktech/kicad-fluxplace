"""Incremental placement on a routed board (ticket w74-incplace).

A shipped board gets new footprints (off the outline) while every existing part is locked and every track and via stays.
`detect` decides the case; `place` seats only the new parts in free space near their partners and edits ONLY their
footprint blocks (boardwrite.apply_moves on the board text), so every other byte of the board is unchanged.

Detection: the board has track/via copper, at least one part is not locked ([lock.at], [placement] fixed, footprint lock)
and every such part is off the board (its courtyard lies outside the outline). Anything else is not this case (a normal
floorplan runs). Refusals (IncError): a [lock.at] row that would move a locked part of a board with copper, or a new part
for which no legal seat exists (the message names the locked parts that block the nearest candidates).

Legal seat: courtyard inside the outline (edge margin), clear of every other part's courtyard (both sides for through-hole),
every pad clear of tracks and vias (the board's clearance), clear of keep-out areas that forbid footprints / pads / tracks.
Choice: minimum wire length to the partners ([[intent.near]] rows first, else the other pads of the part's nets), then the
nearest. Board-agnostic: nothing here names a ref or a net."""
import math

from . import boardfile as BF
from . import boardwrite as BW
from . import geom as GM
from . import preroute as PR

ROT = (0, 90, 180, 270)
STEP = 0.25
BIG_NET = 8               # nets with more pads (planes, rails) carry no placement pull
EDGE_MARGIN = 0.3
COURT_GAP = 0.1
SKIP_STAGES = ("modules", "fanout", "pour-tie", "placetour", "lookahead", "pairs", "tune", "critroute")


class IncError(RuntimeError):
    pass


def _off_board(B, part):
    """the courtyard centre is outside the outline (an edge connector that overhangs stays on the board)."""
    cx, cy = (part["court"][0] + part["court"][2]) / 2, (part["court"][1] + part["court"][3]) / 2
    if B.get("edge"):
        return not GM.edge_has(B["edge"], cx, cy)
    bb = B["bbox"]
    return not (bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3])


def lock_drift(B, cons, tol=None):
    """[lock.at] rows whose part is not at the row: [(ref, (x, y), (x_row, y_row))]."""
    lock = (cons or {}).get("lock") or {}
    tol = float(lock.get("tol_mm", 0.01)) if tol is None else tol
    out = []
    for r, v in sorted((lock.get("at") or {}).items()):
        p = B["parts"].get(r)
        if p and (abs(p["pos"][0] - float(v[0])) > tol or abs(p["pos"][1] - float(v[1])) > tol):
            out.append((r, p["pos"], (float(v[0]), float(v[1]))))
    return out


def detect(B, cons):
    """-> {"incremental": bool, "free": [refs to place], "why": str, "drift": [...]}"""
    fixed = PR.explicit_fixed(B, cons)
    free = sorted(r for r in B["parts"] if r not in fixed)
    copper = bool(B["segs"] or B["vias"])
    if not copper:
        return {"incremental": False, "copper": False, "free": free, "why": "the board has no track or via copper", "drift": []}
    if not free:
        return {"incremental": False, "copper": True, "free": [], "why": "every part is locked", "drift": []}
    on = [r for r in free if not _off_board(B, B["parts"][r])]
    if on:
        return {"incremental": False, "copper": True, "free": free, "drift": [],
                "why": f"unlocked part(s) on the board: {', '.join(on[:6])}{' ...' if len(on) > 6 else ''}"}
    return {"incremental": True, "copper": True, "free": free, "drift": lock_drift(B, cons),
            "why": f"{len(free)} new part(s) off the board, {len(fixed & set(B['parts']))} locked, copper kept"}


# ------------------------------------------------------------------ geometry of a candidate seat
class _World:
    def __init__(self, B, cons, free):
        self.B, self.free = B, set(free)
        self.clr = float(B.get("clearance") or 0.15)
        self.edge = max(EDGE_MARGIN, self.clr)
        self.cop = B["copper"]
        self.segs = [dict(s, box=(min(s["a"][0], s["b"][0]), min(s["a"][1], s["b"][1]), max(s["a"][0], s["b"][0]), max(s["a"][1], s["b"][1])))
                     for s in B["segs"]]
        self.keep = [z for z in B["zones"] if z["keepout"] and (z["no_footprints"] or z["no_pads"] or z["no_tracks"]) and len(z["pts"]) >= 3]
        self.placed = {}                               # ref -> (pads, court, side) of the new parts seated so far
        self.blockers = {}                             # ref -> times a locked part's courtyard rejected a candidate

    def others(self, ref):
        for r, p in self.B["parts"].items():
            if r == ref:
                continue
            if r in self.placed:
                pads, court, side, tht = self.placed[r]
                yield r, court, side, tht, pads
            elif r not in self.free:
                yield r, p["court"], p["side"], p["tht"], p["pads"]

    def layers_of(self, q, side):
        return set(self.cop) if q.get("tht") else (q["layers"] or {side})

    def seg_poly(self, s):
        return [s["a"], s["mid"], s["b"]] if s.get("mid") else [s["a"], s["b"]]

    def clear_of_copper(self, pads, side):
        for q in pads:
            lay = self.layers_of(q, side)
            rect = (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"])
            for s in self.segs:
                if s["layer"] not in lay:
                    continue
                g = self.clr + s["w"] / 2
                if not (rect[0] - g < s["box"][2] and s["box"][0] < rect[2] + g and rect[1] - g < s["box"][3] and s["box"][1] < rect[3] + g):
                    continue
                pl = self.seg_poly(s)
                if any(GM.seg_hits_rect(pl[i], pl[i + 1], (rect[0] - g, rect[1] - g, rect[2] + g, rect[3] + g)) for i in range(len(pl) - 1)):
                    return False
            for v in self.B["vias"]:
                g = self.clr + v["d"] / 2
                if rect[0] - g < v["x"] < rect[2] + g and rect[1] - g < v["y"] < rect[3] + g:
                    return False
        return True

    def clear_of_keepout(self, court, pads, side):
        for z in self.keep:
            if side not in z["layers"] and not any(self.layers_of(q, side) & z["layers"] for q in pads):
                continue
            xs, ys = [p[0] for p in z["pts"]], [p[1] for p in z["pts"]]
            if not (court[0] < max(xs) and min(xs) < court[2] and court[1] < max(ys) and min(ys) < court[3]):
                continue
            ctr = [(court[0], court[1]), (court[2], court[1]), (court[2], court[3]), (court[0], court[3]), ((court[0] + court[2]) / 2, (court[1] + court[3]) / 2)]
            if any(GM.poly_has(z["pts"], *c) for c in ctr) or any(court[0] <= x <= court[2] and court[1] <= y <= court[3] for x, y in z["pts"]):
                return False
            pts = z["pts"]
            if any(GM.seg_hits_rect(pts[i], pts[(i + 1) % len(pts)], court) for i in range(len(pts))):
                return False
        return True

    def legal(self, ref, pads, court, side, tht):
        if not GM.in_outline(self.B, court, self.edge):
            return False
        grown = (court[0] - COURT_GAP, court[1] - COURT_GAP, court[2] + COURT_GAP, court[3] + COURT_GAP)
        for r, oc, os_, ot, opads in self.others(ref):
            if (tht or ot or os_ == side) and GM.overlap(grown, oc):
                if r not in self.free:
                    self.blockers[r] = self.blockers.get(r, 0) + 1
                return False
            for q in pads:
                for o in opads:
                    if self.layers_of(q, side) & self.layers_of(o, os_) and GM.rect_gap(
                            (q["x"] - q["ex"], q["y"] - q["ey"], q["x"] + q["ex"], q["y"] + q["ey"]),
                            (o["x"] - o["ex"], o["y"] - o["ey"], o["x"] + o["ex"], o["y"] + o["ey"])) < self.clr:
                        return False
        return self.clear_of_copper(pads, side) and self.clear_of_keepout(court, pads, side)


# ------------------------------------------------------------------ what a part wants to be near
def _pull_points(B, cons, ref, world):
    """-> (net targets [(net, [pad points])], near partners [(weight, [points], max_mm)]) from the seated parts."""
    def seated(r):
        return r in B["parts"] and (r not in world.free or r in world.placed)

    def pads_of(r):
        return world.placed[r][0] if r in world.placed else B["parts"][r]["pads"]

    mine = {q["net"] for q in B["parts"][ref]["pads"] if q["net"]}
    by_net = {}
    for r in B["parts"]:
        if r != ref and seated(r):
            for q in pads_of(r):
                if q["net"] in mine:
                    by_net.setdefault(q["net"], []).append((q["x"], q["y"]))
    size = {}
    for q in B["pads"]:
        size[q["net"]] = size.get(q["net"], 0) + 1
    tg = [(n, pts) for n, pts in sorted(by_net.items()) if size[n] <= BIG_NET]
    near = []
    try:
        from . import intent as IT
        for n in IT.items(B, cons)["near"]:
            chain = n.get("chain")
            if chain and ref in chain:
                i = chain.index(ref)
                near += [(n["weight"], [(q["x"], q["y"]) for q in pads_of(r)], n["max"])
                         for r in chain[max(0, i - 1):i + 2] if r != ref and seated(r)]
            elif not chain and ref in (n.get("refs") or ()) and seated(n["to"]):
                near.append((n["weight"], [(q["x"], q["y"]) for q in pads_of(n["to"])], n["max"]))
            elif not chain and ref == n.get("to"):
                near += [(n["weight"], [(q["x"], q["y"]) for q in pads_of(r)], n["max"]) for r in n.get("refs") or () if seated(r)]
    except Exception:                                    # noqa: BLE001 - no usable intent table: the net pull alone decides
        pass
    return tg, near


def _cost(pads, tg, near):
    c = 0.0
    for q in pads:
        for n, pts in tg:
            if q["net"] == n:
                c += min(math.hypot(q["x"] - x, q["y"] - y) for x, y in pts)
    if near:
        cx, cy = sum(q["x"] for q in pads) / len(pads), sum(q["y"] for q in pads) / len(pads)
        for w, pts, _mx in near:
            c += w * min(math.hypot(cx - x, cy - y) for x, y in pts)
    return c


def _anchor(B, tg, near):
    pts = [p for _n, ps in tg for p in ps] + [p for _w, ps, _m in near for p in ps]
    if pts:
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    bb = B["bbox"]
    return (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2


def _order(B, cons, free):
    """Parts with the most pulls toward seated copper first; parts tied only to other new parts after their partners."""
    seated = {r for r in B["parts"] if r not in free}
    rank = {}
    for r in free:
        nets = {q["net"] for q in B["parts"][r]["pads"] if q["net"]}
        rank[r] = -sum(1 for q in B["pads"] if q["net"] in nets and q["ref"] in seated and q["ref"] != r)
    return sorted(free, key=lambda r: (rank[r], r))


def _seat(world, B, cons, ref, radius=40.0):
    part = B["parts"][ref]
    tg, near = _pull_points(B, cons, ref, world)
    ax, ay = _anchor(B, tg, near)
    n = int(radius / STEP)
    best, first_d = None, None
    cells = sorted(((dx * STEP) ** 2 + (dy * STEP) ** 2, dx, dy) for dx in range(-n, n + 1) for dy in range(-n, n + 1))
    angs = [(part["angle"] + a) % 360 for a in ROT]
    for d2, dx, dy in cells:
        d = math.sqrt(d2)
        if first_d is not None and d > first_d + 2.0:
            break
        x, y = round(ax + dx * STEP, 3), round(ay + dy * STEP, 3)
        for ang in angs:
            pads, court = GM.moved(part, x, y, ang)
            if not world.legal(ref, pads, court, part["side"], part["tht"]):
                continue
            key = (_cost(pads, tg, near) + 0.01 * d, d, ang)
            if best is None or key < best[0]:
                best = (key, (x, y, ang), pads, court)
            if first_d is None:
                first_d = d
    if best is None:
        why = ", ".join(sorted(world.blockers, key=lambda r: -world.blockers[r])[:5])
        raise IncError(f"no legal seat for {ref} within {radius:g} mm of ({ax:.1f},{ay:.1f}) without moving a locked part"
                       + (f"; locked parts in the way: {why}" if why else "; copper, keep-out or the outline blocks every candidate"))
    return best


def place(board_in, board_out, cons):
    """Seat every free part. -> {ref: (x, y, angle)}. Writes board_out (board_in with only those footprints edited)."""
    from . import routediag as RD
    B = RD.load(board_in)
    d = detect(B, cons)
    if not d["incremental"]:
        raise IncError("not an incremental case: " + d["why"])
    if d["drift"]:
        raise IncError("[lock.at] would move locked part(s) of a board with copper: "
                       + ", ".join(f"{r} ({a[0]:.2f},{a[1]:.2f}) -> ({b[0]:.2f},{b[1]:.2f})" for r, a, b in d["drift"][:6])
                       + "; fix the rows or the board, nothing was written")
    world = _World(B, cons, d["free"])
    moves = {}
    for ref in _order(B, cons, d["free"]):
        world.blockers.clear()
        _k, pose, pads, court = _seat(world, B, cons, ref)
        world.placed[ref] = (pads, court, B["parts"][ref]["side"], B["parts"][ref]["tht"])
        moves[ref] = pose
    text = open(board_in, errors="surrogateescape").read()
    text, done = BW.apply_moves(text, moves)
    if set(done) != set(moves):
        raise IncError("footprint(s) not found in the board text: " + ", ".join(sorted(set(moves) - set(done))))
    with open(board_out, "w", errors="surrogateescape") as fh:
        fh.write(text)
    return moves
