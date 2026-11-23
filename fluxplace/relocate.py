"""relocate: the repair rung that moves a small support part next to the pin it serves (close ladder, `relocate`).

Why: a decoupling cap or pull resistor 36-68 mm from its pin leaves an open that no router rung closes at rail width, and the AI
`nudge` is capped at 1 mm.  route-diagnose already names these parts (`moves`: part, nets, dist, toward).  Per suggestion:
  1. stub: the unlocked tracks/vias that run only from the part's pads back to the first junction are removed (locked or protected
     copper never);
  2. spot: a legal free position/rotation in growing rings around the target pad (courtyards, outline, keepouts, foreign copper);
  3. move: footprint text edit (boardwrite.apply_moves);
  4. route: the part's nets with the close ladder's own KRT rung (gaps._r_krt);
  5. keep only if the board's open count fell and no net got more opens; else the attempt is dropped (the board is untouched).
The close loop then runs the usual DRC guard (gaps._accept: opens/shorts/errors, pairs, R5 protect) and boardwrite.commit (manifest) on the
candidate this rung writes.  Board-agnostic: no reference designator, net name or board constant is read here, only the board model."""
import heapq
import math
import os
import shutil
import time
from collections import defaultdict

from . import boardwrite as BW, geom as G, routediag as RD

RELOC_MAX_MM = 8.0          # largest distance (pad to target pad) searched around the target; the part must end inside this ring
RELOC_STEP_MM = 0.25        # grid step of the ring search (origin positions)
RELOC_ROTS = (0, 90, 180, 270)   # footprint angle changes tried, 0 first (a tie keeps the current angle)
RELOC_PAD_MAX = 3           # parts with more pads are never moved (a small support part: 2-pad passive, 3-pad transistor)
RELOC_PARTS_MAX = 8         # suggestions tried per rung call
RELOC_TRY_MAX = 8000        # legality tests per part before "no spot"
RELOC_COURT_GAP = 0.15      # mm kept between the moved courtyard and any other courtyard on a shared side
RELOC_EDGE_MM = 0.3         # mm between the moved courtyard and the board outline
RELOC_COPPER_MARGIN = 0.05  # extra mm over the board clearance between a moved pad and foreign copper
RELOC_LOCKED_STUB_MM = 3.0  # locked copper (a fan-out neck-down) reached by a stub walk may stay when at most this long; it dangles
TOL = 0.02                  # mm: two track ends closer than this are connected


# ---------------------------------------------------------------- stub

def _in_pad(p, pt, tol=TOL):
    return abs(pt[0] - p["x"]) <= p["ex"] + tol and abs(pt[1] - p["y"]) <= p["ey"] + tol


def _spans(B, v):
    """Copper layers a via touches (its two end layers and every layer between them in stack order)."""
    cu = list(B["copper"])
    ix = [cu.index(l) for l in v["layers"] if l in cu]
    return set(cu[min(ix):max(ix) + 1]) if ix else set(v["layers"])


def _len(s):
    return math.hypot(s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])


def stub_items(B, ref):
    """Copper that only serves the pads of `ref`: from each pad the chain of same-net tracks/vias is followed until a junction
    (an end where two or more other items meet, a via with two or more other items) or a pad of another part.  The chain's items
    are removed; a junction's items stay.  A locked item ends the walk and stays (its length is counted).
    -> {"segs": set(index), "vias": set(index), "locked_mm": float}  (indices in routediag.load order)."""
    part = B["parts"][ref]
    segs, vias = B["segs"], B["vias"]
    ss, vv = defaultdict(list), defaultdict(list)
    for i, s in enumerate(segs):
        ss[s["net"]].append(i)
    for j, v in enumerate(vias):
        vv[v["net"]].append(j)
    others = defaultdict(list)
    for p in B["pads"]:
        if p["ref"] != ref and p["net"]:
            others[p["net"]].append(p)
    spans = {}

    def vspan(j):
        if j not in spans:
            spans[j] = _spans(B, vias[j])
        return spans[j]

    def at(net, pt, layer, skip):
        out = []
        for i in ss.get(net, ()):
            s = segs[i]
            if ("s", i) in skip or (layer and s["layer"] != layer):
                continue
            if math.dist(s["a"], pt) <= TOL or math.dist(s["b"], pt) <= TOL:
                out.append(("s", i))
        for j in vv.get(net, ()):
            v = vias[j]
            if ("v", j) in skip or (layer and layer not in vspan(j)):
                continue
            if math.dist((v["x"], v["y"]), pt) <= max(TOL, v["d"] / 2):
                out.append(("v", j))
        return out

    seen, rem_s, rem_v, locked_mm = set(), set(), set(), 0.0
    queue = []                                         # (item, near point) near = the end the walk came from
    for p in part["pads"]:
        if not p["net"]:
            continue
        for i in ss.get(p["net"], ()):
            s = segs[i]
            if s["layer"] in p["layers"]:
                for e in (s["a"], s["b"]):
                    if _in_pad(p, e):
                        queue.append((("s", i), e))
                        break
        for j in vv.get(p["net"], ()):
            v = vias[j]
            if _in_pad(p, (v["x"], v["y"])) and vspan(j) & set(p["layers"]):
                queue.append((("v", j), (v["x"], v["y"])))
    while queue:
        item, near = queue.pop()
        if item in seen:
            continue
        seen.add(item)
        kind, k = item
        if kind == "s":
            s = segs[k]
            if s["locked"]:
                locked_mm += _len(s)
                continue
            rem_s.add(k)
            far = s["b"] if math.dist(s["a"], near) <= math.dist(s["b"], near) else s["a"]
            if any(_in_pad(p, far) and s["layer"] in p["layers"] for p in others.get(s["net"], ())):
                continue                               # reached another part's pad: the whole connection goes
            nb = at(s["net"], far, s["layer"], seen)
            nb += [x for x in at(s["net"], far, None, seen) if x[0] == "v" and x not in nb]
            nxt = far
        else:
            v = vias[k]
            if v["locked"]:
                locked_mm += v["d"]
                continue
            pos = (v["x"], v["y"])
            nb = [x for x in at(v["net"], pos, None, seen) if x != item]
            if len(nb) >= 2:                           # a via with two further branches is a junction: it stays
                continue
            rem_v.add(k)
            nxt = pos
        if len(nb) == 1:
            queue.append((nb[0], nxt))
    return {"segs": rem_s, "vias": rem_v, "locked_mm": round(locked_mm, 3)}


# ---------------------------------------------------------------- spot

def _sides(part):
    if part.get("tht"):
        return {"F", "B"}
    return set(part.get("court_layers") or ()) or {part["side"][0]}


def _foreign_copper_hit(B, pads, net_of, local, skip_s, skip_v, margin):
    """True when a moved pad lands on copper of another net (tracks, vias, pads), clearance included."""
    for q in pads:
        n = q["net"]
        lay = set(q["layers"])
        for i, s in local["segs"]:
            if i in skip_s or s["net"] == n or s["layer"] not in lay:
                continue
            m = margin + s["w"] / 2
            r = (q["x"] - q["ex"] - m, q["y"] - q["ey"] - m, q["x"] + q["ex"] + m, q["y"] + q["ey"] + m)
            pts = [s["a"], s["b"]] if not s.get("mid") else [s["a"], s["mid"], s["b"]]
            if any(G.seg_hits_rect(pts[k], pts[k + 1], r) for k in range(len(pts) - 1)):
                return True
        for j, v in local["vias"]:
            if j in skip_v or v["net"] == n or not (_spans(B, v) & lay):
                continue
            m = margin + v["d"] / 2
            if abs(v["x"] - q["x"]) < q["ex"] + m and abs(v["y"] - q["y"]) < q["ey"] + m:
                return True
        for o in local["pads"]:
            if o["net"] == n and n:
                continue
            if not (set(o["layers"]) & lay):
                continue
            gx = max(0.0, abs(o["x"] - q["x"]) - o["ex"] - q["ex"])
            gy = max(0.0, abs(o["y"] - q["y"]) - o["ey"] - q["ey"])
            if math.hypot(gx, gy) < margin:
                return True
    return False


def _local(B, ref, centre, reach):
    """Items of the board within `reach` mm of `centre` (the spot search never looks further)."""
    x0, y0, x1, y1 = centre[0] - reach, centre[1] - reach, centre[0] + reach, centre[1] + reach
    box = (x0, y0, x1, y1)
    segs = [(i, s) for i, s in enumerate(B["segs"])
            if G.overlap((min(s["a"][0], s["b"][0]) - s["w"], min(s["a"][1], s["b"][1]) - s["w"],
                          max(s["a"][0], s["b"][0]) + s["w"], max(s["a"][1], s["b"][1]) + s["w"]), box)]
    vias = [(j, v) for j, v in enumerate(B["vias"]) if x0 <= v["x"] <= x1 and y0 <= v["y"] <= y1]
    pads = [p for p in B["pads"] if p["ref"] != ref and x0 <= p["x"] <= x1 and y0 <= p["y"] <= y1]
    courts = [(r, q) for r, q in B["parts"].items() if r != ref and G.overlap(q["court"], box)]
    return {"segs": segs, "vias": vias, "pads": pads, "courts": courts}


def legal(B, ref, x, y, ang, local, skip_s=(), skip_v=(), pad_net=None):
    """Can `ref` sit at (x, y, ang)?  Courtyards clear of every other courtyard sharing a side, inside the outline, no
    footprint/pad keepout, no foreign copper under its pads (the part's own stub copper in skip_* is going away)."""
    part = B["parts"][ref]
    pads, court = G.moved(part, x, y, ang)
    mine = _sides(part)
    for r, q in local["courts"]:
        if _sides(q) & mine and G.overlap(court, q["court"], RELOC_COURT_GAP):
            return False
    if not G.in_outline(B, court, RELOC_EDGE_MM):
        return False
    for z in B["zones"]:
        if z.get("keepout") and (z.get("no_footprints") or z.get("no_pads")) and z["pts"]:
            if any(set(q["layers"]) <= z["layers"] and G.poly_has(z["pts"], q["x"], q["y"]) for q in pads):
                return False
    return not _foreign_copper_hit(B, pads, None, local, set(skip_s), set(skip_v), B["clearance"] + RELOC_COPPER_MARGIN)


def find_spot(B, ref, target, max_mm=RELOC_MAX_MM, step=RELOC_STEP_MM, rots=RELOC_ROTS, tries=RELOC_TRY_MAX, skip_s=(), skip_v=()):
    """Legal (x, y, angle) for `ref` as close as possible to the target pad (a pad dict), by growing distance from that pad to the
    part's own pad of the same net, up to `max_mm`.  None when no spot passes `legal` in `tries` tests."""
    part = B["parts"][ref]
    px, py = part["pos"]
    mine = next((p for p in part["pads"] if p["net"] == target["net"]), None) or part["pads"][0]
    w = max(part["court"][2] - part["court"][0], part["court"][3] - part["court"][1])
    local = _local(B, ref, (target["x"], target["y"]), max_mm + w + 2.0)
    heap, n = [], int(max_mm / step) + 1
    for k, d in enumerate(rots):
        ang = (part["angle"] + d) % 360
        ox, oy = G.rot(mine["x"] - px, mine["y"] - py, ang - part["angle"])      # the pad's offset from the origin at this angle
        for ix in range(-n, n + 1):
            for iy in range(-n, n + 1):
                cx, cy = target["x"] + ix * step, target["y"] + iy * step        # where the part's own pad would sit
                dist = math.hypot(cx - target["x"], cy - target["y"])
                if dist <= max_mm:
                    heapq.heappush(heap, (round(dist, 4), k, ix, iy, ang, cx - ox, cy - oy))
    tested = 0
    while heap and tested < tries:
        dist, _k, _ix, _iy, ang, x, y = heapq.heappop(heap)
        tested += 1
        if legal(B, ref, x, y, ang, local, skip_s, skip_v):
            return {"x": round(x, 3), "y": round(y, 3), "angle": round(ang, 3), "dist": dist, "tested": tested}
    return None


# ---------------------------------------------------------------- suggestions

def _open_nets_of(gaps):
    return sorted({n for g in gaps if str(g.get("kind", "")).startswith("open") for n in g.get("nets") or []})


def suggest(B, gaps, cons=None, protect=(), manifest=None):
    """route-diagnose `moves` for the nets of the open gaps, kept when the part is small, free to move and off protected nets.
    -> [{"part", "toward" (pad dict of the target), "nets", "dist", "from"}] in route-diagnose's order."""
    failed = _open_nets_of(gaps)
    if not failed:
        return []
    from . import preroute as PR
    res = RD.diagnose(B, failed, cons=cons)
    try:
        fixed = PR.fixed_parts(B, cons or {})
    except Exception:                                                       # noqa: BLE001 - anchors need a full spec; locked still holds
        fixed = set()
    mnets = {n for n, e in ((manifest or {}).get("nets") or {}).items() if e.get("why") != "locked"}
    protect = set(protect)
    out = []
    for m in res["summary"]["moves"]:
        part = B["parts"].get(m["part"])
        if not part or part.get("locked") or m["part"] in fixed or not (1 <= len(part["pads"]) <= RELOC_PAD_MAX):
            continue
        if any(p["net"] in protect or p["net"] in mnets for p in part["pads"]):
            continue
        tgt = None
        for t in m["toward"]:
            r, _, num = t.rpartition(".")
            tgt = next((p for p in B["pads"] if p["ref"] == r and p["num"] == num), None)
            if tgt:
                break
        if tgt and tgt["ref"] != m["part"]:
            out.append({"part": m["part"], "toward": tgt, "nets": m["nets"], "dist": m["dist"], "from": m["from"]})
    return out


# ---------------------------------------------------------------- rung

def _open_total(board):
    from . import routeconn as RC
    return RC.open_by_net(board)


def _write_moved(src, dst, ref, spot, stub):
    text = open(src, errors="surrogateescape").read()
    text = BW.strip_indexed(text, stub["segs"], stub["vias"])
    text, done = BW.apply_moves(text, {ref: (spot["x"], spot["y"], spot["angle"])})
    if not done:
        return False
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(text)
    return True


def _rm(p):
    if os.path.lexists(p):
        os.remove(p)


def relocate(base, out, gaps, ctx, route=None, open_count=None):
    """The rung: see the module doc.  Writes `out` only when at least one move was kept.  `route(board, out, nets, ctx)` lays the
    moved part's nets (default: gaps._r_krt, one KRT call per net); `open_count(board)` -> {net: opens} (default: KiCad connectivity).
    -> summary {"status", "moved", "tried", "log": [{part, from, to, result, ...}]}"""
    from . import gaps as GP
    route = route or (lambda b, o, nets, c: GP._r_krt(b, o, [{"nets": nets, "id": "reloc"}], c))
    open_count = open_count or _open_total
    cons = ctx.get("cons") or {}
    B = RD.load(base)
    man = BW.load_manifest(base)
    cands = suggest(B, gaps, cons, ctx.get("protect") or (), man)[:RELOC_PARTS_MAX]
    if not cands:
        return {"status": "no relocatable part", "moved": 0, "tried": 0}
    log = ctx.get("log") or (lambda *_: None)
    t0 = time.time()
    cur, tmps, rows, moved = base, [], [], 0
    now = open_count(cur)
    for k, c in enumerate(cands):
        if time.time() - t0 > ctx.get("budget_s", 1e9):
            rows.append({"part": c["part"], "result": "budget"})
            break
        ref = c["part"]
        row = {"part": ref, "from": c["from"], "dist": c["dist"], "toward": f"{c['toward']['ref']}.{c['toward']['num']}"}
        rows.append(row)
        Bc = RD.load(cur)
        if ref not in Bc["parts"]:
            row["result"] = "gone"
            continue
        row["from"] = [round(Bc["parts"][ref]["pos"][0], 2), round(Bc["parts"][ref]["pos"][1], 2)]
        tgt = next((p for p in Bc["pads"] if p["ref"] == c["toward"]["ref"] and p["num"] == c["toward"]["num"]), None)
        stub = stub_items(Bc, ref)
        if stub["locked_mm"] > RELOC_LOCKED_STUB_MM:
            row["result"] = f"locked stub {stub['locked_mm']} mm"
            continue
        spot = find_spot(Bc, ref, tgt, skip_s=stub["segs"], skip_v=stub["vias"]) if tgt else None
        if not spot:
            row["result"] = "no spot"
            continue
        row["to"] = [spot["x"], spot["y"]]
        row["angle"] = spot["angle"]
        trial, routed = f"{out}.rl{k}.kicad_pcb", f"{out}.rl{k}r.kicad_pcb"
        tmps += [trial, routed]
        for t in (trial, routed):
            GP._clean_board(t)
        GP._sidecars(cur, trial)
        GP._sidecars(cur, routed)
        if not _write_moved(cur, trial, ref, spot, stub):
            row["result"] = "move failed"
            continue
        nets = sorted({p["net"] for p in Bc["parts"][ref]["pads"] if p["net"]})
        try:
            route(trial, routed, nets, dict(ctx, budget_s=max(1.0, ctx.get("budget_s", 1e9) - (time.time() - t0))))
        except Exception as e:                                              # noqa: BLE001 - a failed route drops this attempt only
            row["result"] = f"route error {type(e).__name__}"
            continue
        end = routed if os.path.exists(routed) else trial
        after = open_count(end)
        worse = sorted(n for n, v in after.items() if v > now.get(n, 0))
        if sum(after.values()) < sum(now.values()) and not worse:
            row["result"] = "kept"
            row["opens"] = [sum(now.values()), sum(after.values())]
            cur, now, moved = end, after, moved + 1
            log(f"relocate: {ref} {row['from']} -> {row['to']} kept, opens {row['opens'][0]} -> {row['opens'][1]}")
        else:
            row["result"] = "opens not lower" if not worse else f"worse: {','.join(worse[:3])}"
            row["opens"] = [sum(now.values()), sum(after.values())]
    try:
        if moved:
            GP._copy(cur, out)
            GP._sidecars(cur, out)
            BW.run_post_write(out, [r["part"] for r in rows if r.get("result") == "kept"])
    finally:
        for t in tmps:
            GP._clean_board(t)
    return {"status": "ran" if moved else "nothing kept", "moved": moved, "tried": sum(1 for r in rows if "to" in r), "log": rows}
