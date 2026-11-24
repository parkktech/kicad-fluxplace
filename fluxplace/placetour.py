"""PLACETOUR stage: placement tournament judged by a real router (docs/recipes/PLACETOUR.md). No pcbnew in this module.

Round 0 judges the input: a KRT `--quick 2` lean copy (lookahead.lean_krt) and route_review blame on it. Each round the
blame of the current best makes candidate part moves (turn, relieve, nudge, rot, pull); tier-1 gates and the predictor rank
them in seconds; the best `judge_top` are judged by KRT in parallel (one interpreter each). The winner's MOVED board (no
judge copper) is the next base when its judge key beats the best. Stops: no opens and no hot area, no gain for
`no_gain_rounds`, `max_rounds`, or [flow] budget_min.
"""
import copy
import fnmatch
import json
import math
import os
import shutil
import subprocess
from fluxplace import procs as _procs
import time
from types import SimpleNamespace

from . import boardfile as BF
from . import boardwrite as BW
from . import events as EV
from . import intent as IT
from . import legalise as LG
from . import legality as LGL
from . import liveview as LV
from . import lookahead as LA
from . import placechecks as PC
from . import planplace as PP
from . import planroute as PL
from . import relieve as RL
from . import routable as RB
from . import routediag as RD
from . import routestatus as RS
from . import snapshot as SN

DEFAULTS = {"enabled": False, "max_rounds": 10, "no_gain_rounds": 2, "jobs": 4, "judge_top": 4, "judge_min": 4.0,
            "cands": 12, "hot": 0.5, "nudge_mm": [0.5, 1.0, 2.0], "move_routed": True}
_INT = ("max_rounds", "no_gain_rounds", "jobs", "judge_top", "cands")
WORST = 10 ** 6
KINDS = ("turn", "relieve", "nudge", "rot", "pull")


def params(cons):
    """[placetour] table of constraints.toml -> stage parameters (defaults from the recipe). Off unless enabled = true."""
    e = dict((cons or {}).get("placetour") or {})
    P = dict(DEFAULTS)
    P.update(e)
    P["enabled"] = e.get("enabled") is True
    P["move_routed"] = e.get("move_routed", True) is True
    for k in _INT:
        P[k] = int(P[k])
    P["judge_min"] = float(P["judge_min"])
    P["hot"] = float(P["hot"])
    P["nudge_mm"] = [float(x) for x in P["nudge_mm"]]
    return P


def _progress(path, state, message="", **fields):
    st = {"stage": "placetour", "state": state, "pid": os.getpid(), "current_step": str(message)[:240], "updated": time.time()}
    st.update(fields)
    RS._atomic(path, st)


# ------------------------------------------------------------------ P1 judge + blame
def blame_of(review):
    """route_review --json document -> blame {open, costly, hot, turned, source}. Opens are the signal nets (not poured)."""
    opens = [{"net": o["net"], "refs": sorted(set(o.get("disconnected_pad_parts") or o.get("parts") or []))}
             for o in review.get("open_nets", []) if not o.get("poured")]
    costly = {}
    for n in list(review.get("most_vias", [])) + list(review.get("longest_detours", [])):
        cur = costly.setdefault(n["net"], {"net": n["net"], "vias": 0, "detour": 0.0, "refs": []})
        cur["vias"] = max(cur["vias"], n.get("vias") or 0)
        cur["detour"] = max(cur["detour"], n.get("detour") or 0.0)
        cur["refs"] = sorted(set(cur["refs"]) | set(n.get("parts") or []))
    hot = [{"cells": a["cells"], "occ": a["peak"], "refs": sorted(set(a.get("parts") or [])), "nets": list(a.get("nets") or []),
            "bbox": list(a["box"]), "vias": a.get("vias", 0)} for a in review.get("congested_areas", [])]
    turned = [{"ref": t["ref"], "x": t["x"], "y": t["y"], "rot": t["rot"], "saves": t.get("saves_mm", 0.0),
               "swap": tuple(t["swap_pose"]) if t.get("swap_pose") else None}
              for t in review.get("turned_parts", [])]
    return {"open": opens, "costly": sorted(costly.values(), key=lambda c: (-c["detour"], c["net"])),
            "hot": hot, "turned": turned, "source": "route_review"}


def _review(a, routed, rev_json):
    """route_review.py of the KRT checkout on the judged copy -> blame, or None (tool missing, bad exit, bad json)."""
    tool = os.path.join(a.router_dir, "py_tools", "route_review.py")
    if not os.path.exists(tool):
        return None
    try:
        r = _procs.run([a.router_py, "-X", "utf8", tool, routed, "--json", rev_json, "--top", "30"],
                           cwd=a.router_dir, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not os.path.exists(rev_json):
        return None
    try:
        return blame_of(json.load(open(rev_json)))
    except (OSError, ValueError):
        return None


def _predictor_blame(board, cons, spec):
    """fallback when route_review is unavailable: the planroute findings only (sealed pins and their blockers)."""
    R = PL.run(RD.load(board), cons, spec, ignore_routing=True)
    opens = [{"net": f.get("net"), "refs": sorted({r for r in (f.get("part"), f.get("blocker")) if r})}
             for f in R["findings"] if f["code"] == "PLAN_PIN_SEALED"]
    return {"open": opens, "costly": [], "hot": [], "turned": [], "source": "planroute"}


def judge(a, board, outdir, P, cons=None, spec=None, log=print):
    """-> {routed, blame, source, secs}: KRT --quick 2 judge copy of `board` (None when it failed), blame from route_review on
    that copy; without a routed copy or without route_review the blame comes from the predictor (logged)."""
    t0 = time.time()
    os.makedirs(outdir, exist_ok=True)
    routed = LA.lean_krt(a, board, outdir, P["judge_min"])
    blame = _review(a, routed, os.path.join(outdir, "review.json")) if routed else None
    if blame is None:
        log(f"placetour judge: blame from the predictor ({'no routed copy' if not routed else 'route_review unavailable'})")
        blame = _predictor_blame(routed or board, cons or {}, spec)
    return {"routed": routed, "blame": blame, "source": blame["source"], "secs": round(time.time() - t0, 1)}


KEY_NAMES = ("ghost opens", "hot areas", "hot occupancy", "vias", "detour")       # judge_key, in order (live view)


def _lv(key):
    return [[n, (None if v >= WORST else v)] for n, v in zip(KEY_NAMES, key)] if key is not None else None


def judge_key(routed_board, blame, cons=None):
    """(signal opens, hot areas, sum of hot occupancy, total vias, sum of detour over costly nets) of the judge board, smaller is
    better. No routed copy: the worst key."""
    if routed_board is None or blame is None:
        return (WORST, WORST, float(WORST), WORST, float(WORST))
    vias = len(RD.load(routed_board)["vias"])
    return (len(blame["open"]), len(blame["hot"]), round(sum(h["occ"] for h in blame["hot"]), 4), vias,
            round(sum(c["detour"] for c in blame["costly"]), 4))


# ------------------------------------------------------------------ P2 move generator
def _pieces(B):
    """Connected copper: [{"segs", "vias", "refs" (pads touched, sorted), "locked", "net"}]. Items join when an end (track end,
    arc end or via centre) sits on the same net point (0.01 mm). A STUB is a piece that touches the pads of exactly one part."""
    segs, vias = B["segs"], B["vias"]
    n = len(segs) + len(vias)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    at = {}
    for i, sg in enumerate(segs):
        for x, y in (sg["a"], sg["b"]):
            at.setdefault((sg["net"], round(x, 2), round(y, 2)), []).append(i)
    for j, v in enumerate(vias):
        at.setdefault((v["net"], round(v["x"], 2), round(v["y"], 2)), []).append(len(segs) + j)
    for items in at.values():
        for k in items[1:]:
            union(items[0], k)
    A = RL.attached_copper(B)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        sg = [i for i in members if i < len(segs)]
        vi = [i - len(segs) for i in members if i >= len(segs)]
        refs = set()
        for i in sg:
            refs |= {ref for _k, ref, _n in A["segs"].get(i, [])}
        for j in vi:
            refs |= {ref for ref, _n in A["vias"].get(j, [])}
        locked = any(segs[i].get("locked") for i in sg) or any(vias[j].get("locked") for j in vi)
        net = (segs[sg[0]] if sg else vias[vi[0]])["net"]
        out.append({"segs": sorted(sg), "vias": sorted(vi), "refs": sorted(refs), "locked": locked, "net": net})
    return out


def _stub_of(pc):
    return pc["refs"][0] if len(pc["refs"]) == 1 else None


def _movable(B, cons, P):
    """Parts a move may touch: not fixed, not group members (v1: treated as fixed), not locked. A part is blocked by copper
    that joins it to another part (locked, or unlocked when not move_routed); its own stubs are carried, never blocking."""
    pl = (cons or {}).get("placement") or {}
    grp = {r for g in (pl.get("group") or {}).values() for r in g.get("refs", [])}
    blocked = set(RB._fixed_set(B, cons)) | grp | {r for r, p in B["parts"].items() if p.get("locked")}
    for pc in _pieces(B):
        if len(pc["refs"]) >= 2 and (pc["locked"] or not P["move_routed"]):
            blocked |= set(pc["refs"])
    return sorted(r for r in B["parts"] if r not in blocked)


def _legal(B, cons, targets, mov):
    """legaliser with every non-movable part as an obstacle; the moves that really change a pose (None when it cannot)."""
    fixed = sorted(set(B["parts"]) - set(mov))
    try:
        pos, _rep = LG.legalise(B, targets, fixed=fixed, cons=cons, strict=False)
    except Exception:                                    # noqa: BLE001 - one move that cannot be legalised is no candidate
        return None
    mv = set(mov)                                        # Fable: keep the neighbours the legaliser displaced too (else collision)
    out = {r: tuple(pos[r]) for r in pos if r in mv or r in targets}
    out = {r: v for r, v in out.items() if (abs(v[0] - B["parts"][r]["pos"][0]) > 1e-3 or abs(v[1] - B["parts"][r]["pos"][1]) > 1e-3
                                              or abs(v[2] - B["parts"][r]["angle"]) > 1e-3)}
    return out or None


def _box_parts(B, bbox, grow=2.0):
    """Parts with a pad inside the box grown by `grow` mm (route_review gives no parts for some congested areas)."""
    x0, y0, x1, y1 = bbox
    return sorted({q["ref"] for q in B["pads"] if x0 - grow <= q["x"] <= x1 + grow and y0 - grow <= q["y"] <= y1 + grow})


def _key(moves):
    return tuple(sorted((r, round(v[0], 3), round(v[1], 3), round(v[2], 2)) for r, v in moves.items()))


def candidates(B, blame, cons, P, seed=0, spec=None):
    """-> up to P["cands"] candidates {id, kind, moves {ref: (x, y, rot)}}, round-robin over the kinds, deduplicated."""
    mov = set(_movable(B, cons, P))
    hot = [dict(h, refs=h["refs"] or _box_parts(B, h["bbox"])) for h in blame["hot"]]
    pool = {k: [] for k in KINDS}
    seen = set()

    def add(kind, moves):
        if not moves:
            return
        k = _key(moves)
        if k not in seen:
            seen.add(k)
            pool[kind].append(moves)

    def pos(r):
        return B["parts"][r]["pos"][0], B["parts"][r]["pos"][1], B["parts"][r]["angle"]

    # turn: the route_review turned parts (+180 deg), all at once and each alone for the 3 biggest savings
    tr = sorted((t for t in blame["turned"] if t["ref"] in mov and t.get("swap")),
                key=lambda t: (-t["saves"], t["ref"]))
    if tr:
        add("turn", {t["ref"]: tuple(t["swap"]) for t in tr})
        for t in tr[:3]:
            add("turn", {t["ref"]: tuple(t["swap"])})
    # relieve: the predictor's moves with only the hot-area and open-net parts movable, seeds seed..seed+2
    tgt = (set(r for h in hot[:3] for r in h["refs"]) | set(r for o in blame["open"] for r in o["refs"])) & mov
    if tgt:
        for s in range(seed, seed + 3):
            Bc = copy.deepcopy(B)
            for r in Bc["parts"]:
                if r not in tgt:
                    Bc["parts"][r]["locked"] = True
            mv, st = PP.plan_place(Bc, cons, spec, seed=s, log=lambda m: None, max_evals=200, rotation=True)
            flips = set(st.get("flips", []))
            add("relieve", {r: tuple(v) for r, v in mv.items() if r in mov and r not in flips})
    # nudge: every movable part of the top 3 hot areas pushed away from the area centroid by each nudge_mm
    for h in hot[:3]:
        x0, y0, x1, y1 = h["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        refs = [r for r in h["refs"] if r in mov]
        for d in P["nudge_mm"]:
            tg = {}
            for r in refs:
                x, y, a = pos(r)
                L = math.hypot(x - cx, y - cy) or 1e-6
                tg[r] = (x + (x - cx) / L * d, y + (y - cy) / L * d, a)
            add("nudge", _legal(B, cons, tg, mov) if tg else None)
    # rot: the 4 movable small parts with the largest detour, 90 and 180 deg
    pick = []
    for c in blame["costly"]:
        for r in c["refs"]:
            if r in mov and r not in pick and len(B["parts"][r]["pads"]) <= 16:
                pick.append(r)
        if len(pick) >= 4:
            break
    for k in (90, 180):
        if pick:
            tg = {r: (pos(r)[0], pos(r)[1], (pos(r)[2] + k) % 360) for r in pick[:4]}
            add("rot", _legal(B, cons, tg, mov))
    # pull: costly nets with detour > 1.5 move their small movable parts halfway to the net's pad centroid
    for c in blame["costly"]:
        if c["detour"] <= 1.5:
            continue
        pts = [(q["x"], q["y"]) for q in B["pads"] if q["net"] == c["net"]]
        if not pts:
            continue
        gx, gy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        tg = {}
        for r in c["refs"]:
            if r in mov and len(B["parts"][r]["pads"]) <= 16:
                x, y, a = pos(r)
                tg[r] = (x + (gx - x) / 2, y + (gy - y) / 2, a)
        if tg:
            add("pull", _legal(B, cons, tg, mov))

    out = []
    while len(out) < P["cands"] and any(pool.values()):
        for k in KINDS:
            if pool[k] and len(out) < P["cands"]:
                n = len(out)
                out.append({"id": f"{k}{n:02d}", "kind": k, "moves": pool[k].pop(0)})
    return out


# ------------------------------------------------------------------ P3 tier-1 gates
def _ko_hits(B, cons, ko):
    """(ref, keepout name) of parts whose courtyard overlaps a keepout rect on their side (not allowed, not hardware)."""
    hits = []
    for rect, sides, allow, mh, name in ko:
        for r, p in B["parts"].items():
            if any(fnmatch.fnmatch(r, g) for g in allow) or p["side"] not in sides:
                continue
            if not (p["tht"] or mh is None):
                continue
            x0, y0, x1, y1 = p["court"]
            if x0 < rect[2] and rect[0] < x1 and y0 < rect[3] and rect[1] < y1:
                hits.append((r, name))
    return hits


def gate_state(B, cons, spec):
    """Gate values of one board: counts for the FAIL/WARN gates, sets for the named hard rows. Worse = higher count / new member."""
    _ok, cf = LGL.collision_gate(B, cons)
    rows = IT.check(B, cons, spec) if IT.enabled(cons) else []
    pos = {r: (p["pos"][0], p["pos"][1], p["angle"]) for r, p in B["parts"].items()}
    return {"collision": sum(1 for x in cf if x["level"] == "FAIL"),
            "intent": frozenset(f"{r['kind']}:{r['name']}" for r in rows if r["hard"] and not r["met"]),
            "connector": len(PC.connector_blocked(B, cons)),
            "side": len(PC.side_policy(B, cons, spec)),
            "height": len(PC.height_regions(B, cons)),
            "keepout": frozenset(_ko_hits(B, cons, LG.keepout_rects(B, cons))),
            "outline": frozenset(LG.check_outline(B, pos))}


def worse_gates(st, base):
    """Names of the gates where `st` is worse than `base`."""
    out = []
    for k, v in st.items():
        b = base[k]
        if isinstance(v, int):
            if v > b:
                out.append(k)
        elif not v <= b:
            out.append(k)
    return out


CELL = 2.0


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_dist(a, b, c, d):
    """distance between segments ab and cd (0 when they cross)."""
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    if (side(a, b, c) * side(a, b, d) < 0) and (side(c, d, a) * side(c, d, b) < 0):
        return 0.0
    return min(_pt_seg(c[0], c[1], a[0], a[1], b[0], b[1]), _pt_seg(d[0], d[1], a[0], a[1], b[0], b[1]),
               _pt_seg(a[0], a[1], c[0], c[1], d[0], d[1]), _pt_seg(b[0], b[1], c[0], c[1], d[0], d[1]))


def _pt_pad(x, y, q, B):
    """distance from a point to the pad copper (round pads: circle; others: the rectangle at the pad's board rotation)."""
    if q["shape"] == "circle":
        return max(0.0, math.hypot(x - q["x"], y - q["y"]) - q["w"] / 2)
    rot = q["angle"] + B["parts"].get(q["ref"], {}).get("angle", 0.0)
    dx, dy = BF._rot(x - q["x"], y - q["y"], -rot)
    return math.hypot(max(abs(dx) - q["w"] / 2, 0.0), max(abs(dy) - q["h"] / 2, 0.0))


def _grid_add(grid, key_box, item):
    x0, y0, x1, y1 = key_box
    for i in range(math.floor(x0 / CELL), math.floor(x1 / CELL) + 1):
        for j in range(math.floor(y0 / CELL), math.floor(y1 / CELL) + 1):
            grid.setdefault((i, j), []).append(item)


def carried_clearance(B, carried, clearance=None):
    """Carried stub copper (tracks and vias listed in `carried` {'segs': {idx: ref}, 'vias': {idx: ref}}) must keep the
    clearance to every FOREIGN-net pad, track and via: tracks against pads and tracks on the same layer, vias against
    everything (all layers). Net classes are not parsed: the board's Default clearance (routediag) is used for all nets.
    -> the ref of the first moved part whose copper is too close, or None. Uses a 2 mm grid."""
    if not carried or (not carried["segs"] and not carried["vias"]):
        return None
    clr = float(clearance if clearance is not None else B.get("clearance", 0.14))
    tol = 0.005
    grid = {}
    for q in B["pads"]:
        _grid_add(grid, (q["x"] - q["ex"] - clr, q["y"] - q["ey"] - clr, q["x"] + q["ex"] + clr, q["y"] + q["ey"] + clr), ("pad", q))
    for i, sg in enumerate(B["segs"]):
        m = sg["w"] / 2 + clr
        _grid_add(grid, (min(sg["a"][0], sg["b"][0]) - m, min(sg["a"][1], sg["b"][1]) - m,
                         max(sg["a"][0], sg["b"][0]) + m, max(sg["a"][1], sg["b"][1]) + m), ("seg", i, sg))
    for j, v in enumerate(B["vias"]):
        m = v["d"] / 2 + clr
        _grid_add(grid, (v["x"] - m, v["y"] - m, v["x"] + m, v["y"] + m), ("via", j, v))

    def near(box, net):
        seen = set()
        for i in range(math.floor(box[0] / CELL), math.floor(box[2] / CELL) + 1):
            for j in range(math.floor(box[1] / CELL), math.floor(box[3] / CELL) + 1):
                for it in grid.get((i, j), []):
                    obj = it[1] if it[0] == "pad" else it[2]
                    if id(obj) in seen or obj["net"] == net:
                        continue
                    seen.add(id(obj))
                    yield it

    for kind, idx_map in (("seg", carried["segs"]), ("via", carried["vias"])):
        for idx, owner in idx_map.items():
            if kind == "seg":
                sg = B["segs"][idx]
                a, b, w, layer, net = sg["a"], sg["b"], sg["w"], sg["layer"], sg["net"]
                box = (min(a[0], b[0]) - w, min(a[1], b[1]) - w, max(a[0], b[0]) + w, max(a[1], b[1]) + w)
            else:
                v = B["vias"][idx]
                w = v["d"]
                net = v["net"]
                box = (v["x"] - w, v["y"] - w, v["x"] + w, v["y"] + w)
            for tag, *rest in near(box, net):
                clr_need = clr - tol
                if tag == "pad":
                    q = rest[0]
                    if kind == "seg" and layer not in q["layers"]:
                        continue
                    if kind == "seg":                       # sampled every 0.05 mm along the track
                        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.05) + 1)
                        d = min(_pt_pad(a[0] + (b[0] - a[0]) * t / (n - 1), a[1] + (b[1] - a[1]) * t / (n - 1), q, B)
                                for t in range(n)) - w / 2
                    else:
                        d = _pt_pad(v["x"], v["y"], q, B) - w / 2
                elif tag == "seg":
                    j, o = rest
                    if kind == "seg" and o["layer"] != layer:
                        continue
                    if kind == "seg":
                        d = _seg_dist(a, b, o["a"], o["b"]) - (w + o["w"]) / 2
                    else:
                        d = _pt_seg(v["x"], v["y"], o["a"][0], o["a"][1], o["b"][0], o["b"][1]) - (v["d"] + o["w"]) / 2
                else:
                    j, o = rest
                    if kind == "seg":
                        d = _pt_seg(o["x"], o["y"], a[0], a[1], b[0], b[1]) - (o["d"] + w) / 2
                    else:
                        d = math.hypot(o["x"] - v["x"], o["y"] - v["y"]) - (o["d"] + v["d"]) / 2
                if d < clr_need:
                    return owner
    return None


def _apply(board_path, moves, out_path, move_routed):
    """Moves on a copy (text, no post-write hook). Stub copper of a moved part is carried with it (boardwrite.carry_copper);
    with move_routed the unlocked non-stub copper that touches a moved part is dropped (it would be detached).
    -> (ok, problems, removed {'segs', 'vias'}), ok from _guard."""
    B = RD.load(board_path)
    carry, rs, rv = [], set(), set()
    for pc in _pieces(B):
        st = _stub_of(pc)
        if st is not None and st in moves:
            carry.append({"ref": st, "segs": pc["segs"], "vias": pc["vias"]})
        elif move_routed and not pc["locked"] and any(r in moves for r in pc["refs"]):
            rs |= set(pc["segs"])
            rv |= set(pc["vias"])
    moves_by_ref = {r: ((B["parts"][r]["pos"][0], B["parts"][r]["pos"][1], B["parts"][r]["angle"]), tuple(v))
                    for r, v in moves.items()}
    text = open(board_path, errors="surrogateescape").read()
    text = BW.carry_copper(text, moves_by_ref, carry)
    if rs or rv:
        text = BW.strip_indexed(text, rs, rv)[0]
    text = BW.apply_moves(text, moves)[0]
    with open(out_path, "w", errors="surrogateescape") as fh:
        fh.write(text)
    ok, probs = _guard(B, RD.load(out_path), carry, rs, rv)
    carried = {"segs": {i: c["ref"] for c in carry for i in c["segs"]},
               "vias": {j: c["ref"] for c in carry for j in c["vias"]}}
    return ok, probs, {"segs": sorted(rs), "vias": sorted(rv), "carried": carried}


def _guard(A, Bn, carry, rs, rv):
    """Connectivity guard for a moved board: same refs and pad->net map; the kept tracks and vias keep their net; the
    non-carried ones keep their end points; every kept end still lands on the same pad (carried stubs included)."""
    probs = []
    if set(A["parts"]) != set(Bn["parts"]):
        probs.append("ref set differs")
    for r in sorted(set(A["parts"]) & set(Bn["parts"])):
        pa = sorted((str(q.get("num")), str(q.get("net"))) for q in A["parts"][r]["pads"])
        pb = sorted((str(q.get("num")), str(q.get("net"))) for q in Bn["parts"][r]["pads"])
        if pa != pb:
            probs.append(f"{r}: pad/net map changed")
    if len(A["segs"]) - len(rs) != len(Bn["segs"]) or len(A["vias"]) - len(rv) != len(Bn["vias"]):
        return False, probs + ["segment or via count changed"]
    cs = {i for c in carry for i in c["segs"]}
    cv = {j for c in carry for j in c["vias"]}
    Aa, Ba = RL.attached_copper(A), RL.attached_copper(Bn)
    ks = [i for i in range(len(A["segs"])) if i not in rs]
    kv = [j for j in range(len(A["vias"])) if j not in rv]
    for n, i in enumerate(ks):
        sa, sb = A["segs"][i], Bn["segs"][n]
        if sa["net"] != sb["net"]:
            probs.append(f"track net changed at {sa['a']}")
        if i not in cs and (tuple(sa["a"]), tuple(sa["b"])) != (tuple(sb["a"]), tuple(sb["b"])):
            probs.append(f"track {sa['net']} at {sa['a']} moved without its part")
        before = {(r, num) for _k, r, num in Aa["segs"].get(i, [])}
        after = {(r, num) for _k, r, num in Ba["segs"].get(n, [])}
        if before != after:
            probs.append(f"track {sa['net']} at {sa['a']} no longer on {sorted(before - after)}")
    for n, j in enumerate(kv):
        va, vb = A["vias"][j], Bn["vias"][n]
        if va["net"] != vb["net"]:
            probs.append(f"via net changed at ({va['x']},{va['y']})")
        if j not in cv and (va["x"], va["y"]) != (vb["x"], vb["y"]):
            probs.append(f"via {va['net']} moved without its part")
        if set(Aa["vias"].get(j, [])) != set(Ba["vias"].get(n, [])):
            probs.append(f"via {va['net']} at ({va['x']},{va['y']}) no longer on its pads")
    return not probs, probs[:50]


def tier1(base_path, cands, cons, spec, P, outdir, base_state):
    """-> (survivors [(rank key, cand, moved board path, removed)] best first, dropped [(cand, reason)])."""
    os.makedirs(outdir, exist_ok=True)
    survivors, dropped = [], []
    for c in cands:
        p = os.path.join(outdir, f"{c['id']}.kicad_pcb")
        ok, probs, removed = _apply(base_path, c["moves"], p, P["move_routed"])
        if not ok:
            dropped.append((c, "guard: " + "; ".join(probs[:2])))
            continue
        B1 = RD.load(p)
        bad = carried_clearance(B1, removed.get("carried") or {"segs": {}, "vias": {}})
        if bad:
            dropped.append((c, f"carried copper clearance {bad}"))
            continue
        worse = worse_gates(gate_state(B1, cons, spec), base_state)
        if worse:
            dropped.append((c, "worse: " + ",".join(worse)))
            continue
        survivors.append((tuple(PL.rank_key(PL.run(B1, cons, spec, ignore_routing=False))), c, p, removed))
    survivors.sort(key=lambda t: t[0])
    return survivors, dropped


# ------------------------------------------------------------------ P4 tournament stage
def _worker(jobfile):
    """one judge job in a fresh interpreter (LA._run_jobs entry "placetour:_worker"): KRT judge + blame + key + PNG -> result.json."""
    from . import constraints as C
    job = json.load(open(jobfile))
    out = {"tag": job["tag"], "routed": None, "blame": None, "key": None, "png": None, "secs": None, "error": None}
    try:
        acfg = job["a"]
        a = SimpleNamespace(**acfg)
        cons = C.load(acfg["constraints"]) if acfg.get("constraints") else {}
        spec = json.load(open(acfg["spec"])) if acfg.get("spec") else None
        P = params(cons)
        P["judge_min"] = float(job["judge_min"])
        j = judge(a, job["board"], job["dir"], P, cons, spec)
        key = judge_key(j["routed"], j["blame"], cons)
        out.update(routed=j["routed"], blame=j["blame"], secs=j["secs"], key=list(key))
        if j["routed"]:
            fit = {"id": job["tag"], "key": list(key), "critical": job.get("critical", [])}
            out["png"] = LA.render(j["routed"], [], fit, os.path.join(job["dir"], "judge.png"))
    except Exception as e:                                # noqa: BLE001 - one judge failing is a row, not a stage crash
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    with open(job["result"], "w") as fh:
        json.dump(out, fh, default=sorted)


def _row(r, cid, kind, moved, t1key, jkey, png, verdict):
    return {"round": r, "id": cid, "variant": f"{kind} moved={moved} t1={tuple(t1key) if t1key else '-'}", "fit": jkey,
            "png": png, "verdict": verdict}


def _report(path, P, start, end, accepted, removed_refs, groups, rounds_log):
    L = ["# PLACETOUR report", "", "| | input | output |", "|---|---|---|"]
    names = ("judge opens (signal)", "hot areas", "hot occupancy", "vias", "detour sum")
    for i, nm in enumerate(names):
        L.append(f"| {nm} | {start[i]} | {end[i]} |")
    L += ["", "## rounds", "| round | candidates | tier-1 survivors | judged | winner | judge key | verdict |", "|---|---|---|---|---|---|---|"]
    L += [f"| {x['round']} | {x['cands']} | {x['surv']} | {x['judged']} | {x['winner']} | {x['key']} | {x['verdict']} |" for x in rounds_log]
    L += ["", "## accepted moves", "| round | kind | ref | from | to | rot |", "|---|---|---|---|---|---|"]
    L += [f"| {m['round']} | {m['why']} | {m['ref']} | {m['from']} | {m['to']} | {m['rot']} |" for m in accepted]
    L += ["", "## refs whose attached copper was removed (fan-out again)", ", ".join(sorted(removed_refs)) or "none", ""]
    L += ["## group members (treated as fixed in v1)", ", ".join(sorted(groups)) or "none", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


def _png(routed, cid, key, crit, path):
    return LA.render(routed, [], {"id": cid, "key": list(key), "critical": crit}, path)


def stage(c, n, board):
    """placetour stage: tournament of part moves judged by KRT --quick 2 (see module doc). Output: the winner's moved board."""
    P = params(c.cons)
    if not P["enabled"]:
        return {"verdict": "skipped", "line": "[placetour] enabled = false", "board": board}
    t_stage = time.time()
    d = c.dir(n, "placetour")
    spath, table = os.path.join(d, "progress.json"), os.path.join(d, "TOURNAMENT.md")
    a, cons, spec = c.a, c.cons, c.spec
    acfg = {k: getattr(a, k, None) for k in ("constraints", "spec", "router_dir", "router_py", "freerouting_jar", "java")}
    crit = LA.critical_nets(board, cons, spec)
    budget = float(((cons or {}).get("flow") or {}).get("budget_min") or 0)
    timing, rows, accepted, rounds_log, removed_refs = {}, [], [], [], set()
    _progress(spath, "running", "round 0: judge the input")
    j0 = judge(a, board, os.path.join(d, "r0-input"), P, cons, spec)
    key0 = judge_key(j0["routed"], j0["blame"], cons)
    best = {"board": board, "blame": j0["blame"], "key": key0, "routed": j0["routed"]}
    png0 = _png(j0["routed"] or board, "input", key0, crit, os.path.join(d, "input.png"))
    rows.append(_row(0, "input", "input", "-", None, key0, png0, "reference"))
    if j0["routed"]:
        SN.active_take(j0["routed"], "placetour r0 judge input", force=True, opens=key0[0])
    EV.artifact("png", png0, "input")
    EV.iteration("place", 0, n=P["max_rounds"], metrics={"opens": key0[0], "hot": key0[1], "vias": key0[3]}, note="input")
    LA._table(table, rows)
    from . import ready as RDY, parts as PTS                  # C7b: the rows of the input, measured now; the output is set against them
    ds_dir = PTS.resolve_dir(((cons or {}).get("docs") or {}).get("datasheets"), [getattr(a, "constraints", None), board])
    rows_in = RDY.stage_rows(spec, cons, RD.load(board), board, ds_dir)
    ex = LV.Exporter(cons, getattr(a, "constraints", None), stage="placetour") if LV.work() else None          # F16: the live view
    LV.candidate("placetour", "input", board, None, _lv(key0), None, cons, ex, board=board, generation=0, generations=P["max_rounds"],
                 arm="move", is_leader=True, leader="input", full=True)
    stale, rounds, stop = 0, 0, "no round ran"
    for r in range(1, P["max_rounds"] + 1):
        if budget and time.time() - t_stage > budget * 60:
            stop = "flow budget_min"
            break
        rounds = r
        Bb = RD.load(best["board"])
        _progress(spath, "running", f"round {r}: candidates", timing=timing, rounds=r - 1)
        cands = candidates(Bb, best["blame"], cons, P, seed=100 * r, spec=spec)
        rdir = os.path.join(d, f"r{r}")
        surv, _dropped = tier1(best["board"], cands, cons, spec, P, rdir, gate_state(Bb, cons, spec))
        top = surv[:P["judge_top"]]
        LV.progress("placetour", "tier1", always=True, generation=r, generations=P["max_rounds"], trial=len(surv), trials=len(cands), arm="move",
                    leader_key=_lv(best["key"]), why=f"{len(cands)} candidates, {len(surv)} pass tier 1, {len(top)} to judge")
        _progress(spath, "running", f"round {r}: {len(cands)} candidates, {len(surv)} tier-1, judging {len(top)}",
                  timing=timing, rounds=r - 1)
        jobs = []
        for _key1, cnd, p, _rem in top:
            jd = LA._mk(os.path.join(rdir, "judge", cnd["id"]))
            job = {"tag": f"r{r}-{cnd['id']}", "board": p, "dir": jd, "result": os.path.join(jd, "result.json"), "a": acfg,
                   "judge_min": P["judge_min"], "critical": sorted(crit)}
            job["jobfile"] = os.path.join(LA._mk(os.path.join(rdir, "jobs")), f"{cnd['id']}.json")
            with open(job["jobfile"], "w") as fh:
                json.dump(job, fh, default=sorted)
            jobs.append(job)
        t_judge = time.time()
        results = LA._run_jobs(jobs, P, P["judge_min"] * 60 + 600, entry="placetour:_worker") if jobs else []
        timing[f"round{r}_judge_s"] = round(time.time() - t_judge, 1)
        winner = None
        for (key1, cnd, p, rem), job, res in zip(top, jobs, results):
            jk = tuple(res["key"]) if res.get("key") else None
            if res.get("routed"):
                SN.active_take(res["routed"], f"placetour r{r} judge {cnd['id']}", force=True, opens=jk[0])
            if res.get("png"):
                EV.artifact("png", res["png"], job["tag"])
            verdict = "fail: " + str(res.get("error") or "no routed copy") if jk is None else "kept"
            if jk is not None and (winner is None or jk < winner[0]):
                winner = (jk, cnd, p, rem, res)
            rows.append(_row(r, job["tag"], cnd["kind"], _moved(cnd["moves"]), key1, jk, res.get("png"), verdict))
            LV.progress("placetour", "judge", always=True, generation=r, generations=P["max_rounds"], arm="move", cand=job["tag"], key=_lv(jk),
                        leader_key=_lv(best["key"]), verdict="kept" if jk is not None and jk < tuple(best["key"]) else "dropped",
                        why=verdict if jk is None else cnd["kind"])
        improved = winner is not None and tuple(winner[0]) < tuple(best["key"])
        if improved:
            jk, cnd, p, rem, res = winner
            rows[-len(jobs) + [j["tag"] for j in jobs].index(f"r{r}-{cnd['id']}")]["verdict"] = "winner"
            for ref, v in cnd["moves"].items():
                accepted.append({"round": r, "why": cnd["kind"], "ref": ref, "rot": v[2],
                                 "from": (round(Bb["parts"][ref]["pos"][0], 3), round(Bb["parts"][ref]["pos"][1], 3), Bb["parts"][ref]["angle"]),
                                 "to": (round(v[0], 3), round(v[1], 3), v[2])})
            A = RL.attached_copper(Bb)
            for i in rem["segs"]:
                removed_refs |= {ref for _k, ref, _n in A["segs"][i] if ref in cnd["moves"]}
            for i in rem["vias"]:
                removed_refs |= {ref for ref, _n in A["vias"][i] if ref in cnd["moves"]}
            best = {"board": p, "blame": res["blame"], "key": jk, "routed": res["routed"]}
            stale = 0
            SN.active_take(p, f"placetour r{r} winner", force=True, opens=jk[0])
            EV.emit("parts", stage="placetour", i=r, refs=sorted(cnd["moves"]), why=cnd["kind"])
        else:
            stale += 1
        best_key = best["key"]
        winner_id = winner[1]["id"] if (winner and improved) else "-"
        rounds_log.append({"round": r, "cands": len(cands), "surv": len(surv), "judged": len(jobs), "winner": winner_id,
                           "key": tuple(best_key), "verdict": "improved" if improved else "no gain"})
        EV.iteration("place", r, n=P["max_rounds"], metrics={"candidates": len(cands), "tier1": len(surv), "judged": len(jobs),
                                                             "opens": best_key[0], "hot": best_key[1], "vias": best_key[3],
                                                             "stale": stale}, note=winner_id)
        timing[f"round{r}_s"] = round(time.time() - t_stage, 1)
        LV.candidate("placetour", f"r{r}-{winner_id}" if improved else f"r{r}-no-gain", best["board"] if improved else None, None, _lv(best_key), None, cons,
                     ex if improved else None, board=best["board"], generation=r, generations=P["max_rounds"], arm="move", is_leader=improved,
                     verdict="kept" if improved else "dropped", why="improved" if improved else "no gain")
        if ex is not None:
            ex.poll()
        LA._table(table, rows)
        _progress(spath, "running", f"round {r} done: best key {tuple(best_key)}", rounds=r, timing=timing)
        if best_key[0] == 0 and best_key[1] == 0:
            stop = "clean (no opens, no hot area)"
            break
        if stale >= P["no_gain_rounds"]:
            stop = f"no gain for {stale} round(s)"
            break
        stop = "max_rounds"
    out = os.path.join(d, "board.kicad_pcb")
    shutil.copy(best["board"], out)
    LA._copy_sidecar(best["board"], out)
    rep = os.path.join(d, "placetour.md")
    groups = [r for g in ((cons.get("placement") or {}).get("group") or {}).values() for r in g.get("refs", [])]
    _report(rep, P, key0, best["key"], accepted, removed_refs, groups, rounds_log)
    timing["stage_s"] = round(time.time() - t_stage, 1)
    moved = sorted({m["ref"] for m in accepted})
    line = (f"placetour: {rounds} rounds, judge key {tuple(key0)} -> {tuple(best['key'])}, {len(moved)} parts moved, "
            f"{stop}, {timing['stage_s']} s")
    _progress(spath, "done", line, board=out, rounds=rounds, timing=timing, reason=stop)
    nxt = [f"TOURNAMENT.md: {table}", f"report: {rep}"]
    hon = RDY.stage_honoured("placetour", spec, cons, rows_in, out, RD.load(out), ds_dir=ds_dir)
    nxt.append(hon["line"])
    LV.candidate("placetour", "result", out, None, _lv(best["key"]), hon["criteria"], cons, ex, board=out, lane="accepted", phase="gate", arm="move", full=True)
    if LV.work():                                # F16: the owner's view of the result; until the hard gate exists (F7) it is recorded, not enforced
        _b, _c, fb = LV.stage_start(out, cons, spec, "placetour", d, apply=False)
        g = LV.gate("PLACE_OK", None, [{"cand": "result", "key": _lv(best["key"]), "criteria": hon["criteria"], "board": out}], fb, cons, ex)
        if g["by"]:
            nxt.append(f"gate PLACE_OK closed by {g['by']}: {g['action']}" + (f" - {g['text']}" if g["text"] else ""))
        for row in fb.finish(RD.load(out)) if fb is not None else []:
            nxt.append(row)
    if ex is not None:
        ex.close()
    if removed_refs:
        nxt.append("re-escape moved parts: fanout --parts " + ",".join(sorted(removed_refs)))
    return {"verdict": "pass" if best["routed"] else "warn", "line": line, "board": out, "next": nxt}


def _moved(moves):
    return ",".join(sorted(moves))[:80] or "-"
