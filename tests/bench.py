#!/usr/bin/env python3
"""Regression benchmark — run the full route-aware pipeline against a real board
(never modifying it) and print one comparable metric line. Used after EVERY change:
a feature only survives if this does not regress.

  PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3 tests/bench.py \
      --board path/to/board.kicad_pcb [--json out.json] [--seeds N]

Metrics: overflow (the gate — must stay 0), overlaps (must stay 0), HPWL, part
extent + fill, hub centrality, hub<->biggest-module adjacency gap, wall time.
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--json", default=None)
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--baseline", default=None, help="prior bench json to diff against")
    a = ap.parse_args()

    from fluxplace import kicad_io as IO, graph as G, topology as T, placement as P

    board = IO.load(a.board)
    parts, nets = IO.read_board(board)
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    center = IO.board_center(board)

    t0 = time.time()
    kw = {"layers": len(IO.signal_layers(board))}
    if a.seeds != 1:
        kw["seeds"] = a.seeds
    pos, angles, rep = P.place_routed(parts, cg, topo, center=center, **kw)
    dt = time.time() - t0

    ov = P.count_overlaps(parts, pos, 0.0, angles=angles)
    hpwl = P.hpwl(parts, cg, pos)
    xs0 = [pos[r][0] - P.eff_size(parts, r, angles.get(r, 0.0), 0.0)[0] / 2 for r in pos]
    ys0 = [pos[r][1] - P.eff_size(parts, r, angles.get(r, 0.0), 0.0)[1] / 2 for r in pos]
    xs1 = [pos[r][0] + P.eff_size(parts, r, angles.get(r, 0.0), 0.0)[0] / 2 for r in pos]
    ys1 = [pos[r][1] + P.eff_size(parts, r, angles.get(r, 0.0), 0.0)[1] / 2 for r in pos]
    ew, eh = max(xs1) - min(xs0), max(ys1) - min(ys0)
    tot = sum(parts[r]["w"] * parts[r]["h"] for r in parts)
    cx, cy = (max(xs1) + min(xs0)) / 2, (max(ys1) + min(ys0)) / 2

    hub = topo.hub
    hubc = math.hypot(pos[hub][0] - cx, pos[hub][1] - cy) if hub in pos else -1
    # biggest non-hub module = the M.2-class part; adjacency gap to the hub
    mods = sorted((r for r in parts if r != hub),
                  key=lambda r: -parts[r]["w"] * parts[r]["h"])
    mod = mods[0]
    gap = -1.0
    if hub in pos:
        dx = abs(pos[mod][0] - pos[hub][0]) - (parts[mod]["w"] + parts[hub]["w"]) / 2
        dy = abs(pos[mod][1] - pos[hub][1]) - (parts[mod]["h"] + parts[hub]["h"]) / 2
        gap = min(dx, dy)  # <=0 on one axis means touching-adjacent along the other

    m = dict(overflow=rep["overflow"], overlaps=ov, hpwl=round(hpwl),
             extent=[round(ew, 1), round(eh, 1)],
             fill=round(100 * tot / (ew * eh), 1),
             hub_center_off=round(hubc, 1), module_gap=round(gap, 1),
             wirelength=round(rep["wirelength"]),
             worst_detour=round(max(rep["detour"].values()), 2) if rep["detour"] else 0,
             pair_sep=rep.get("pair_sep"), power_routed=rep.get("power_nets"),
             secs=round(dt, 1))
    print("BENCH " + json.dumps(m))

    ok = True
    if a.baseline and os.path.exists(a.baseline):
        b = json.load(open(a.baseline))
        def worse(key, tol, larger_is_worse=True):
            if key not in b or b[key] is None or m.get(key) is None:
                return False
            d = m[key] - b[key] if larger_is_worse else b[key] - m[key]
            return d > tol
        checks = [
            ("overflow", m["overflow"] > 0 and m["overflow"] > b.get("overflow", 0),
             "GATE: overflow regressed"),
            ("overlaps", m["overlaps"] > 0, "GATE: overlaps present"),
            ("hpwl", worse("hpwl", b.get("hpwl", 0) * 0.08), "HPWL regressed >8%"),
            ("area", (m["extent"][0] * m["extent"][1]) >
             (b["extent"][0] * b["extent"][1]) * 1.08, "area regressed >8%"),
        ]
        for _, bad, msg in checks:
            if bad:
                ok = False
                print("REGRESSION — " + msg)
        print("vs baseline: " + ("OK (no regressions)" if ok else "FAILED"))
    if a.json:
        json.dump(m, open(a.json, "w"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
