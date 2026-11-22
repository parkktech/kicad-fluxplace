"""Differential tester (ROUTER-C1 section 9). Part 1 (K13): calibration board, oracle (kicad-cli DRC), kernel check,
attribution by net token, and the type table. Part 2 (K14) adds edit generation, batching and the metrics.

Every calibration case has its own nets K<n>A / K<n>B so that a violation is attributed by net name on both sides.
Source boards are never written: the calibration board is generated in the work directory.
"""
import json
import random
import re
import shutil
import subprocess
from pathlib import Path

from fluxplace import kernel as K

HEAD = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
        '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        + "".join(f'(gr_line (start {a} {b}) (end {c} {d}) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts") '
                  f'(uuid "1212121{i}-1212-4121-8121-121212121212"))\n'
                  for i, (a, b, c, d) in enumerate([(0, 0, 200, 0), (200, 0, 200, 200), (200, 200, 0, 200), (0, 200, 0, 0)])))

# the project matches the kernel fallback floors and the default class (ROUTER-C1 section 6)
PROJECT = {
    "meta": {"filename": "cal.kicad_pro", "version": 3},
    "board": {"design_settings": {"rules": {
        "min_clearance": 0.0, "min_track_width": 0.0, "min_via_diameter": 0.5, "min_through_hole_diameter": 0.3,
        "min_via_annular_width": 0.1, "min_hole_clearance": 0.25, "min_hole_to_hole": 0.25,
        "min_copper_edge_clearance": 0.5}}},
    "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2,
                                  "via_diameter": 0.6, "via_drill": 0.3}],
                     "netclass_patterns": [], "netclass_assignments": None},
}

DRU = ('(version 1)\n'
       '(rule LO (condition "A.NetName == \'DRU_LO\' || B.NetName == \'DRU_LO\'") (constraint clearance (min 0.1mm)))\n'
       '(rule HI (condition "A.NetName == \'DRU_HI\' || B.NetName == \'DRU_HI\'") (constraint clearance (min 0.4mm)))\n')

# kicad-cli type string -> kernel violation type (ROUTER-C1 section 9)
TYPE_MAP = {
    "clearance": "clearance", "hole_clearance": "hole clearance", "hole_to_hole": "hole to hole",
    "copper_edge_clearance": "edge", "shorting_items": "short", "track_width": "width", "via_diameter": "via size",
    "hole_size": "drill", "annular_width": "annular", "keepout": "keepout",
}


def _tr(x0, y0, x1, y1, net, w=0.2, layer="F.Cu", u=None):
    return (f'(segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") (net "{net}") '
            f'(uuid "{u}"))\n')


def _via(x, y, net, u, size=0.6, drill=0.3, layers=None):
    lay = f' (layers "{layers[0]}" "{layers[1]}")' if layers else ""
    return f'(via (at {x} {y}) (size {size}) (drill {drill}){lay} (net "{net}") (uuid "{u}"))\n'


def _pad_fp(ref, x, y, net, clr=None):
    c = f" (clearance {clr})" if clr is not None else ""
    return (f'(footprint "P" (layer "F.Cu") (uuid "{_u("f" + ref)}") (at {x} {y} 0) (property "Reference" "{ref}") '
            f'(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "{net}") (uuid "{_u("p" + ref)}"){c}))\n')


def _u(tag):
    return f"00000000-0000-4000-8000-{abs(hash(tag)) % 10**12:012d}"


def build(workdir):
    """Writes cal.kicad_pcb/.kicad_pro/.kicad_dru into workdir. Returns (board path, cases)."""
    w = Path(workdir)
    w.mkdir(parents=True, exist_ok=True)
    body, cases = [], []

    def site(k):
        return 20 + (k % 8) * 22, 20 + (k // 8) * 30

    def case(name, expect, sites_nets, group):
        cases.append({"id": len(cases), "name": name, "expect": expect, "group": group, "nets": sites_nets})

    # 0-2 clearance at, around and under 0.2 mm; 3-5 epsilon probe (1 nm, 10 nm, 100 nm under)
    gaps = [(0.2 - 0.001, True), (0.2, False), (0.2 + 0.001, False),
            (0.2 - 1e-6, True), (0.2 - 1e-5, True), (0.2 - 1e-4, True),
            (0.2 - 3e-4, True), (0.2 - 5e-4, True)]
    for name, (g, exp) in zip(["clr_minus_1um", "clr_at", "clr_plus_1um", "eps_1nm", "eps_10nm", "eps_100nm",
                               "eps_300nm", "eps_500nm"], gaps):
        k = len(cases)
        x, y = site(k)
        n = f"K{k}"
        body.append(_tr(x, y, x + 5, y, n + "A", u=_u(n + "a")))
        body.append(_tr(x, y + 0.4 + (g - 0.2) * 0 + (g - 0.2) + 0.0, x + 5, y + 0.4 + (g - 0.2), n + "B", u=_u(n + "b")))
        case(name, exp, [n + "A", n + "B"], "clearance")
    # 6 local pad clearance 0.3 beats class 0.2: track 0.25 below the pad edge (pad half 0.3, track r 0.1)
    k = len(cases); x, y = site(k); n = f"K{k}"
    body.append(_pad_fp(n, x, y, n + "A", clr=0.3))
    body.append(_tr(x - 5, y - 0.3 - 0.1 - 0.25, x + 5, y - 0.3 - 0.1 - 0.25, n + "B", u=_u(n + "b")))
    case("local_pad_0p3_wins", True, [n + "A", n + "B"], "precedence")
    # DRU_LO (0.1) under the class 0.2: gap 0.15 accepted; DRU_HI (0.4): gap 0.3 refused
    for kk, gap, exp, name in [(7, 0.15, False, "dru_lowers_0p1"), (8, 0.3, True, "dru_raises_0p4")]:
        k = len(cases); x, y = site(k); n = f"K{k}"
        net_a = "DRU_LO" if kk == 7 else "DRU_HI"
        body.append(_tr(x, y, x + 5, y, net_a, u=_u(n + "a")))
        body.append(_tr(x, y + 0.2 + gap, x + 5, y + 0.2 + gap, n + "B", u=_u(n + "b")))
        case(name, exp, [net_a, n + "B"], "dru")
    # 9-10 hole clearance 0.25 (centre distance 0.5 = boundary); 11 hole to hole 0.25 (distance 0.55)
    # the through-via copper ring (r 0.3) clears a track only by d - 0.4, so at d = 0.5 copper binds before the hole
    # rule: "hole_copper_at" is a copper-bound boundary case (both tools flag it), not an isolated hole-rule probe
    for d, exp, name in [(0.5 - 0.001, True, "hole_clr_minus_1um"), (0.5, True, "hole_copper_at")]:
        k = len(cases); x, y = site(k); n = f"K{k}"
        body.append(_via(x, y, n + "A", _u(n + "v")))
        body.append(_tr(x - 5, y + d, x + 5, y + d, n + "B", u=_u(n + "b")))
        case(name, exp, [n + "A", n + "B"], "hole")
    k = len(cases); x, y = site(k); n = f"K{k}"
    body.append(_via(x, y, n + "A", _u(n + "v1")))
    body.append(_via(x + 0.55 - 0.001, y, n + "B", _u(n + "v2")))
    case("h2h_minus_1um", True, [n + "A", n + "B"], "hole")
    # 13-14 edge 0.5 (track centre 0.6 from the left outline x = 0)
    for i, (dx, exp, name) in enumerate([(0.6 - 0.001, True, "edge_minus_1um"), (0.6, False, "edge_at")]):
        k = len(cases); x, y = site(k); n = f"K{k}"
        body.append(_tr(dx, 100 + 3 * i, dx + 5, 100 + 3 * i, n + "A", u=_u(n + "a")))
        case(name, exp, [n + "A"], "edge")
    # 15 same-net overlap is never a violation; 16 different nets overlap is a short
    k = len(cases); x, y = site(k); n = f"K{k}"
    body.append(_tr(x, y, x + 5, y, n + "A", u=_u(n + "a")))
    body.append(_tr(x, y + 0.1, x + 5, y + 0.1, n + "A", u=_u(n + "b")))
    case("same_net_overlap", False, [n + "A"], "same")
    k = len(cases); x, y = site(k); n = f"K{k}"
    body.append(_tr(x, y, x + 5, y, n + "A", u=_u(n + "a")))
    body.append(_tr(x + 5, y, x + 10, y, n + "B", u=_u(n + "b")))  # end-to-end touch: KiCad connects copper at endpoints only
    case("different_net_touch", True, [n + "A", n + "B"], "short")
    # 17-20 recorded only (section 13 K13 calibration cases): NPTH with copper annulus, blind via outside its span,
    # wide Edge.Cuts stroke, foreign track nearer a fill than the zone clearance without touching
    k = len(cases); x, y = site(k); n = f"K{k}"
    body.append(f'(footprint "N" (layer "F.Cu") (uuid "{_u("fn")}") (at {x} {y} 0) (property "Reference" "H{k}") '
                f'(pad "" np_thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.8) (layers "*.Cu") (uuid "{_u("pn")}")))\n')
    case("npth_annulus", None, [], "recorded")
    k = len(cases); x, y = site(k); n = f"K{k}"
    body.append(_via(x, y, n + "A", _u(n + "v"), layers=("F.Cu", "In1.Cu")))
    body.append(_tr(x - 2, y + 0.5, x + 2, y + 0.5, n + "B", layer="B.Cu", u=_u(n + "b")))
    case("blind_via_hole_outside_span", None, [n + "A", n + "B"], "recorded")
    # not run: a wide Edge.Cuts stroke needs a closed outline of its own (a stray open line makes KiCad report
    # invalid_outline and corrupts the edge results of every case on the board)
    case("wide_edge_stroke", None, [], "not_run")
    case("fill_near_no_touch", None, [], "recorded")
    text = "".join(body)
    nets = sorted(set(re.findall(r'\(net "([^"]+)"\)', text)))
    decl = "".join(f'(net {i + 1} "{nm}")\n' for i, nm in enumerate(nets))
    body_txt = HEAD + decl + text + ")\n"
    board = w / "cal.kicad_pcb"
    board.write_text(body_txt)
    (w / "cal.kicad_pro").write_text(json.dumps(PROJECT, indent=1))
    (w / "cal.kicad_dru").write_text(DRU)
    return str(board), cases


def oracle(board_path, workdir):
    """kicad-cli DRC (one call) -> list of {type, nets: set, uuids}."""
    out = Path(workdir) / "oracle.json"
    cli = shutil.which("kicad-cli") or "/usr/bin/kicad-cli"
    subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--output", str(out), board_path],
                   capture_output=True, text=True, timeout=110)
    d = json.loads(out.read_text())
    res = []
    for v in d.get("violations", []):
        if v.get("type") not in TYPE_MAP:          # warnings (dangling ends, silk) and invalid_outline are not modelled
            continue
        nets = set()
        for it in v.get("items", []):
            nets |= set(re.findall(r"\[(K\d+[AB]|[^\]]+)\]", it.get("description", "")))
        res.append({"type": TYPE_MAP.get(v.get("type"), v.get("type")), "nets": nets, "raw": v.get("type")})
    return res


def kernel(board_path):
    with K.Kernel(board_path) as k:
        items = {i["id"]: i for i in k.call("items")["items"]}
        vs = k.call("check")["violations"]
    res = []
    for v in vs:
        nets = {items[v["a"]]["net_name"]} if v["a"] in items else set()
        if v.get("b") in items:
            nets.add(items[v["b"]]["net_name"])
        res.append({"type": v["type"], "nets": nets})
    return res


def attribute(cases, violations):
    """Violations per case id (a case owns a violation when one of its nets is named in it)."""
    out = {c["id"]: [] for c in cases}
    for v in violations:
        for c in cases:
            if c["nets"] and any(n in v["nets"] for n in c["nets"]):
                out[c["id"]].append(v["type"])
    return out


def run(workdir):
    board, cases = build(workdir)
    ko = attribute(cases, oracle(board, workdir))
    kk = attribute(cases, kernel(board))
    rows = []
    for c in cases:
        o = sorted(set(ko[c["id"]]))
        k_ = sorted(set(kk[c["id"]]))
        rows.append({"case": c["name"], "group": c["group"], "expect_violation": c["expect"],
                     "kicad": o, "kernel": k_, "agree": (bool(o) == bool(k_)) if c["expect"] is not None else None})
    return rows


# ---- K14 part 2: edit generator, batcher, attribution, metrics, runner (ROUTER-C1 section 9) ----
import datetime
import hashlib
import math
import time
import uuid as _uuid
from collections import Counter, defaultdict

from fluxplace import boardtext as BT

COPPER = {"track", "via", "pad"}
NEED = 0.2            # Default class clearance used to place near misses (nominal)
BAND = 0.001          # need - have <= 1 um is the band (recipe section 9); reported, not counted
BOUNDARY_D = [-0.020, -0.005, -0.001, 0.0, 0.001, 0.005, 0.020]
MARGIN = 3.0          # independence: boxes at least 3 mm apart, disjoint net sets
REACH = 1.0           # net-set reach (mm): nets of copper within 1 mm of an edit's box
MODELLED = set(TYPE_MAP)
REV = defaultdict(set)
for _k, _v in TYPE_MAP.items():
    REV[_v].add(_k)
WEIGHTS = [("track", 30), ("via", 10), ("path", 10), ("remove", 10), ("nudge", 5), ("near", 30), ("boundary", 5)]
SAMPLE_LAYERS = None  # set from the board's layers in run_board


def _uid(rng):
    return str(_uuid.UUID(int=rng.getrandbits(128), version=4))


def _pool(items):
    return [it for it in items if it["kind"] in COPPER and it["layers"] and it["net_name"]]


def _grow(bb, m):
    return [bb[0] - m, bb[1] - m, bb[2] + m, bb[3] + m]


def _overlap(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _seg(a, b, net, w, layer, u):
    return {"kind": "seg", "net": net, "layer": layer, "width": w, "a": list(a), "b": list(b), "uuid": u}


def _via_add(at, net, u, size=0.6, drill=0.3):
    return {"kind": "via", "net": net, "at": list(at), "dia": size, "drill": drill, "layers": ["F.Cu", "B.Cu"], "uuid": u}


def gen_edit(rng, eid, items, layers):
    """One edit, deterministic in rng. adds are kernel item dicts; removes are uuids; nets are net names."""
    pool = _pool(items)
    movable = [it for it in items if it["kind"] in ("track", "via") and not it["locked"] and it["net_name"]]
    r = rng.random()
    acc = 0.0
    kind = WEIGHTS[-1][0]
    for name, w in WEIGHTS:
        acc += w / 100.0
        if r < acc:
            kind = name
            break
    sub = rng.random() < 0.03
    adds, removes, nominal = [], [], None
    if kind in ("remove", "nudge") and not movable:
        kind = "track"
    if kind == "path":
        a = rng.choice(pool)
        mates = [b for b in pool if b["net_name"] == a["net_name"] and b["id"] != a["id"]]
        if not mates:
            kind = "track"
        else:
            b = rng.choice(mates)
            ax, ay = (a["bbox"][0] + a["bbox"][2]) / 2, (a["bbox"][1] + a["bbox"][3]) / 2
            bx, by = (b["bbox"][0] + b["bbox"][2]) / 2, (b["bbox"][1] + b["bbox"][3]) / 2
            ly = rng.choice(layers)
            adds.append(_seg((ax, ay), (bx, ay), a["net_name"], 0.2, ly, _uid(rng)))
            adds.append(_seg((bx, ay), (bx, by), a["net_name"], 0.2, ly, _uid(rng)))
    if kind == "track":
        it = rng.choice(pool)
        x, y = rng.uniform(it["bbox"][0], it["bbox"][2]), rng.uniform(it["bbox"][1], it["bbox"][3])
        length = rng.uniform(0.2, 5.0)
        ang = rng.choice([0, 45, 90, 135, 180, 225, 270, 315]) if rng.random() < 0.9 else rng.uniform(0, 360)
        x2, y2 = x + length * math.cos(math.radians(ang)), y + length * math.sin(math.radians(ang))
        w = 0.1 if sub else 0.2
        adds.append(_seg((x, y), (x2, y2), it["net_name"], w, rng.choice(layers), _uid(rng)))
    elif kind == "via":
        it = rng.choice(pool)
        x, y = rng.uniform(it["bbox"][0], it["bbox"][2]), rng.uniform(it["bbox"][1], it["bbox"][3])
        x, y = x + rng.uniform(-3, 3), y + rng.uniform(-3, 3)
        size, drill = (0.25, 0.15) if sub else (0.6, 0.3)
        adds.append(_via_add((x, y), it["net_name"], _uid(rng), size, drill))
    elif kind == "remove":
        it = rng.choice(movable)
        removes.append(it["uuid"])
    elif kind == "nudge":
        it = rng.choice([m for m in movable if m["kind"] == "via"] or movable)
        if it["kind"] == "via":
            d = rng.uniform(0.01, 0.3)
            t = rng.uniform(0, 2 * math.pi)
            cx, cy = (it["bbox"][0] + it["bbox"][2]) / 2, (it["bbox"][1] + it["bbox"][3]) / 2
            removes.append(it["uuid"])
            adds.append(_via_add((cx + d * math.cos(t), cy + d * math.sin(t)), it["net_name"], _uid(rng)))
        else:
            removes.append(it["uuid"])
    elif kind in ("near", "boundary"):
        f = rng.choice(pool)
        d = rng.uniform(-0.05, 0.05) if kind == "near" else rng.choice(BOUNDARY_D)
        y = f["bbox"][3] + 0.1 + NEED + d
        nominal = NEED + d
        adds.append(_seg((f["bbox"][0], y), (f["bbox"][2], y), f"NM{eid}", 0.2, f["layers"][0], _uid(rng)))
    nets = {a.get("net") for a in adds} | {it["net_name"] for it in items if it["uuid"] in removes}
    nets.discard(None)
    bb = [math.inf, math.inf, -math.inf, -math.inf]
    for a in adds:
        pts = [a["a"], a["b"]] if a["kind"] == "seg" else [a["at"]]
        m = (a.get("width", a.get("dia", 0)) / 2)
        for p in pts:
            bb = [min(bb[0], p[0] - m), min(bb[1], p[1] - m), max(bb[2], p[0] + m), max(bb[3], p[1] + m)]
    for it in items:
        if it["uuid"] in removes:
            bb = [min(bb[0], it["bbox"][0]), min(bb[1], it["bbox"][1]), max(bb[2], it["bbox"][2]), max(bb[3], it["bbox"][3])]
    return {"id": eid, "kind": kind, "adds": adds, "removes": removes,
            "uuids": [a["uuid"] for a in adds] + removes, "nets": sorted(n for n in nets if n),
            "bbox": bb, "nominal_gap": nominal, "sub_floor": sub}


def pack(edits, pool, size=25):
    """Greedy first-fit batches of up to `size` independent edits (boxes >= MARGIN apart, disjoint net sets)."""
    for e in edits:
        near = _grow(e["bbox"], REACH)  # nets within reach: every clearance rule in the model is <= 0.5 mm
        e["netset"] = set(e["nets"]) | {it["net_name"] for it in pool if _overlap(it["bbox"], near)}

    def indep(a, b):
        dx = max(b["bbox"][0] - a["bbox"][2], a["bbox"][0] - b["bbox"][2], 0.0)
        dy = max(b["bbox"][1] - a["bbox"][3], a["bbox"][1] - b["bbox"][3], 0.0)
        return math.hypot(dx, dy) >= MARGIN and not (a["netset"] & b["netset"])

    pending, batches = list(edits), []
    while pending:
        cur, rest = [], []
        for e in pending:
            if len(cur) < size and all(indep(e, c) for c in cur):
                cur.append(e)
            else:
                rest.append(e)
        batches.append(cur)
        pending = rest
    return batches


def _drop(text, uuids):
    """text without the segment/via forms whose uuid is in `uuids`."""
    keys = set(uuids)
    if not keys:
        return text
    out, last = [], 0
    for i, j, _k, blk in BT.scan(text):
        m = re.search(r'\(uuid "([^"]+)"\)', blk)
        if m and m.group(1) in keys:
            s = text.rfind("\n", 0, i) + 1
            if text[s:i].strip():
                s = i
            e = j + 1
            if text[e:e + 1] == "\n":
                e += 1
            out.append(text[last:s])
            last = e
    out.append(text[last:])
    return "".join(out)


def _block_text(a):
    if a["kind"] == "seg":
        return _tr(a["a"][0], a["a"][1], a["b"][0], a["b"][1], a["net"], a["width"], a["layer"], a["uuid"])
    return _via(a["at"][0], a["at"][1], a["net"], a["uuid"], a["dia"], a["drill"], tuple(a["layers"]))


def batch_text(base_text, batch):
    adds = [a for e in batch for a in e["adds"]]
    t = _drop(base_text, [u for e in batch for u in e["removes"]])
    declared = set(re.findall(r'\(net \d+ "([^"]*)"\)', base_text))
    new = sorted({a["net"] for a in adds} - declared - {""})
    decl = [f'(net {90000 + i} "{n}")' for i, n in enumerate(new)]
    return BT.append_blocks(t, decl + [_block_text(a) for a in adds])


def drc_raw(board_path, out, refill=True, save=False):
    cli = shutil.which("kicad-cli") or "/usr/bin/kicad-cli"
    args = [cli, "pcb", "drc", "--format", "json", "--severity-all", "--output", str(out)]
    if refill:
        args.append("--refill-zones")
    if save:
        args.append("--save-board")
    subprocess.run(args + [str(board_path)], check=False, capture_output=True, text=True)
    return json.loads(Path(out).read_text())


DANGLING = {"track_dangling", "via_dangling"}


def _open_counts(rep):
    """Unconnected ends per net name (kicad lists them as unconnected_items or as *_dangling violations)."""
    c = Counter()
    rows = list(rep.get("unconnected_items", []))  # islands only; *_dangling are warnings (outside model)
    for u in rows:
        for it in u.get("items", []):
            m = re.search(r"\[([^\]]*)\]", it.get("description", ""))
            c[m.group(1) if m else ""] += 1
    return c


def classify(base_rep, batch_rep, batch):
    """Oracle verdict per edit in one batch: bad when it owns a new modelled violation or a rise in opens on its nets.
    New violations of other types are outside_model. A new violation or open no edit owns is a tester bug (raises)."""
    base_keys = {(v["type"], frozenset(i.get("uuid", "") for i in v.get("items", [])))
                 for v in base_rep.get("violations", [])}
    owner = {u: e["id"] for e in batch for u in e["uuids"]}
    res = {e["id"]: {"oracle": "good", "types": [], "outside": []} for e in batch}
    for v in batch_rep.get("violations", []):
        us = frozenset(i.get("uuid", "") for i in v.get("items", []))
        if (v["type"], us) in base_keys:
            continue
        ids = {owner[u] for u in us if u in owner}
        if not ids and v["type"] in DANGLING:
            continue  # a neighbour's dangling end after a removal: a warning, not an owned violation
        if not ids:
            raise RuntimeError(f"new violation no edit owns: {v['type']} {v.get('description', '')}")
        for i in ids:
            res[i].setdefault("detail", []).append(f"{v['type']}: {v.get('description', '')}")
            if v["type"] in MODELLED:
                res[i]["oracle"] = "bad"
                res[i]["types"].append(v["type"])
            elif v["type"] not in DANGLING:
                res[i]["outside"].append(v["type"])
    base_open, batch_open = _open_counts(base_rep), _open_counts(batch_rep)
    for net, n in batch_open.items():
        if n <= base_open.get(net, 0):
            continue
        # owner: the edit's own nets or nets within REACH of its copper (a new item can split or short a neighbour)
        owners = [e for e in batch if net in (e.get("netset") or set(e["nets"]))]
        if not owners:
            raise RuntimeError(f"opens rise on net {net!r} ({n} vs {base_open.get(net, 0)}) no edit owns; batch: "
                               + "; ".join(f"{e['id']} {e['kind']} {e['nets']} rm={e['removes']}" for e in batch))
        for e in owners:
            res[e["id"]].setdefault("detail", []).append(f"opens on {net}: {n} vs {base_open.get(net, 0)}")
            res[e["id"]]["oracle"] = "bad"
            res[e["id"]]["types"].append("opens")
    return res


def kernel_eval(k, e, base_items, zone_boxes):
    """Kernel verdict for one edit alone against the base: begin, remove, add, check, rollback."""
    k.call("begin")
    try:
        if e["removes"]:
            k.call("remove", uuids=e["removes"])
        ids = k.call("add", items=e["adds"])["ids"] if e["adds"] else []
        near = [it["id"] for it in base_items if it["uuid"] not in e["removes"] and _overlap(it["bbox"], _grow(e["bbox"], 1.0))]
        t0 = time.perf_counter()
        r = k.call("check", ids=ids + near, allow_opens=False)
        ms = (time.perf_counter() - t0) * 1000
    finally:
        k.call("rollback")
    zone = any(_overlap(z, e["bbox"]) for z in zone_boxes)
    verdict = "defer" if zone else r["verdict"]
    return {"verdict": verdict, "violations": r["violations"], "ms": ms, "zone": zone}


def score(edits, kern, oracle):
    rows = []
    for e in edits:
        r, o = kern[e["id"]], oracle[e["id"]]
        gaps = [v["need"] - v["have"] for v in r["violations"] if v.get("have") is not None]
        band = r["verdict"] == "refuse" and bool(gaps) and min(gaps) <= BAND + 1e-9
        rows.append({"id": e["id"], "kind": e["kind"], "kernel": r["verdict"], "oracle": o["oracle"],
                     "k_types": sorted({v["type"] for v in r["violations"]}), "o_types": sorted(set(o["types"])),
                     "outside": o["outside"], "band": band, "ms": r["ms"], "zone": r["zone"],
                     "sub_floor": e["sub_floor"], "nominal_gap": e["nominal_gap"], "edit": e, "o_detail": o.get("detail", []),
                     "k_violations": r["violations"]})
    fa = [x for x in rows if x["kernel"] == "accept" and x["oracle"] == "bad"]
    good = [x for x in rows if x["oracle"] == "good"]
    # kicad-cli does not judge shorts on this build (K13: different_net_touch; overlap rows here): a kernel refusal with
    # a short, which the oracle does not see, is an oracle-blind refusal, reported apart from false refusals
    blind = [x for x in rows if x["kernel"] == "refuse" and x["oracle"] == "good" and "short" in x["k_types"]]
    fr = [x for x in rows if x["kernel"] == "refuse" and x["oracle"] == "good" and not x["band"] and x not in blind]
    bd = [x for x in rows if x["kernel"] == "refuse" and x["oracle"] == "good" and x["band"]]
    refused_bad = [x for x in rows if x["kernel"] == "refuse" and x["oracle"] == "bad"]
    # a refusal by an opens rise has no violation type: it is not a type match
    parity = [x for x in refused_bad if x["k_types"] and REV.get(x["k_types"][0], set()) & set(x["o_types"])]
    ms = sorted(x["ms"] for x in rows if x["kernel"] != "defer")
    pct = lambda p: round(ms[min(len(ms) - 1, int(p * len(ms)))], 3) if ms else None
    near_share = sum(1 for e in edits if e["kind"] in ("near", "boundary")) / max(1, len(edits))
    m = {"edits": len(rows), "accept": sum(x["kernel"] == "accept" for x in rows),
         "refuse": sum(x["kernel"] == "refuse" for x in rows), "defer": sum(x["kernel"] == "defer" for x in rows),
         "defer_zone": sum(x["zone"] for x in rows if x["kernel"] == "defer"),
         "oracle_good": len(good), "oracle_bad": len(rows) - len(good),
         "false_accepts": len(fa), "false_refusals": len(fr), "false_refusal_rate": round(len(fr) / max(1, len(good)), 5),
         "band": len(bd), "oracle_blind": len(blind), "parity": round(len(parity) / len(refused_bad), 4) if refused_bad else None,
         "outside_model": dict(Counter(t for x in rows for t in x["outside"])),
         "p50_ms": pct(0.5), "p99_ms": pct(0.99), "near_share": round(near_share, 4)}
    return m, rows, fa, fr[:20], blind[:20]


def save_cases(cases_dir, sha, board, fa, fr, blind=()):
    cases_dir = Path(cases_dir)
    cases_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for tag, group in (("FA", fa), ("FR", fr), ("FB", blind)):
        for c in group:
            n += 1
            (cases_dir / f"{sha}-{n}.json").write_text(json.dumps({"board": str(board), "board_sha": sha, "tag": tag, **c},
                                                                  indent=1, default=list))


def run_board(board, out, seed=1, n_edits=1000, batch_size=25, cases_dir=None, write_corpus=True):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    status = out / "kdiff.status.json"
    started = datetime.datetime.now().isoformat(timespec="seconds")
    status.write_text(json.dumps({"state": "running", "board": str(board), "started": started}))
    try:
        base = out / "base.kicad_pcb"
        shutil.copy(board, base)
        for ext in (".kicad_pro", ".kicad_dru"):
            side = Path(board).with_suffix(ext)
            if side.exists():
                shutil.copy(side, base.with_suffix(ext))
        base_rep = drc_raw(base, out / "base_drc.json", refill=True, save=True)
        base_text = base.read_text()
        sha = hashlib.sha256(Path(board).read_bytes()).hexdigest()[:8]
        k = K.Kernel(str(base))
        try:
            info = k.call("info")
            base_items = k.call("items")["items"]
            zone_boxes = [z["bbox"] for z in k.call("zones").get("zones", []) if z.get("bbox")]
            rng = random.Random(seed)
            layers = [x for x in info["layers"] if x.endswith("Cu")]
            edits = [gen_edit(rng, i, base_items, layers) for i in range(n_edits)]
            kern = {e["id"]: kernel_eval(k, e, base_items, zone_boxes) for e in edits}
        finally:
            k.close()
        batches = pack(edits, _pool(base_items), batch_size)
        oracle = {}
        for bi, b in enumerate(batches):
            bp = out / f"b{bi:04d}.kicad_pcb"
            bp.write_text(batch_text(base_text, b))
            for ext in (".kicad_pro", ".kicad_dru"):
                if base.with_suffix(ext).exists():
                    shutil.copy(base.with_suffix(ext), bp.with_suffix(ext))
            rep = drc_raw(bp, out / f"b{bi:04d}_drc.json", refill=True)
            oracle.update(classify(base_rep, rep, b))
            for f in (bp, out / f"b{bi:04d}_drc.json"):
                f.unlink(missing_ok=True)
        m, rows, fa, fr, blind = score(edits, kern, oracle)
        m.update({"board": str(board), "board_sha": sha, "seed": seed, "batches": len(batches),
                  "load_ms": info.get("load_ms"), "unsupported": info.get("unsupported"), "dru": info.get("dru")})
        (out / "kdiff.json").write_text(json.dumps({"summary": m, "rows": rows}, indent=1, default=str))
        save_cases(cases_dir or Path(__file__).parent / "kdiff_cases", sha, board, fa, fr, blind)
        corpus = Path(__file__).resolve().parents[1] / "docs" / "corpus" / "kdiff.jsonl"
        line = {"ts": started, "tool": "kdiff", "board": Path(board).name, "board_sha": sha, "seed": seed,
                "edits": m["edits"], "mix": dict(WEIGHTS), "near_share": m["near_share"],
                "false_accepts": m["false_accepts"], "false_refusals": m["false_refusals"], "band": m["band"],
                "defers": m["defer"], "outside_model": m["outside_model"], "p50_ms": m["p50_ms"],
                "p99_ms": m["p99_ms"], "load_ms": m["load_ms"], "coverage": {"unsupported": m["unsupported"], "dru": m["dru"]}}
        if write_corpus:
            with corpus.open("a") as fh:
                fh.write(json.dumps(line) + "\n")
        status.write_text(json.dumps({"state": "done", "board": str(board), "started": started,
                                      "finished": datetime.datetime.now().isoformat(timespec="seconds")}))
        return m
    except Exception as exc:
        status.write_text(json.dumps({"state": "failed", "board": str(board), "error": repr(exc)}))
        raise


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--edits", type=int, default=1000)
    a = ap.parse_args()
    m = run_board(a.board, a.out, a.seed, a.edits)
    print("| metric | value |\n|---|---|")
    for key, val in m.items():
        print(f"| {key} | {val} |")
