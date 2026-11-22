"""Differential tester (ROUTER-C1 section 9). Part 1 (K13): calibration board, oracle (kicad-cli DRC), kernel check,
attribution by net token, and the type table. Part 2 (K14) adds edit generation, batching and the metrics.

Every calibration case has its own nets K<n>A / K<n>B so that a violation is attributed by net name on both sides.
Source boards are never written: the calibration board is generated in the work directory.
"""
import json
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
