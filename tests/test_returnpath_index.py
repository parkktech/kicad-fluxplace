"""Planes.nets_at (returnpath) with the y-band edge index must equal the reference
per-ring _inside scan on every point: random polygons with holes, and a zone file."""
import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import returnpath as RP


def _reference_grid(rings):
    """The pre-index construction: raw (bbox, pts) items in every cell their bbox touches."""
    grid = {}
    for key, rs in rings.items():
        g = defaultdict(list)
        for item in rs:
            (x0, y0, x1, y1), _ = item
            for cx in range(math.floor(x0 / RP.CELL_MM), math.floor(x1 / RP.CELL_MM) + 1):
                for cy in range(math.floor(y0 / RP.CELL_MM), math.floor(y1 / RP.CELL_MM) + 1):
                    g[(cx, cy)].append(item)
        grid[key] = g
    return grid


def _reference_nets_at(P, ref_grid, layer, x, y):
    cell = (math.floor(x / RP.CELL_MM), math.floor(y / RP.CELL_MM))
    return {net for net in P.nets_on.get(layer, ())
            if RP._inside(ref_grid[(layer, net)].get(cell, ()), x, y)}


def _random_ring(rng, cx, cy, r, n):
    pts = []
    for k in range(n):
        a = 2 * math.pi * k / n
        rr = r * rng.uniform(0.3, 1.0)           # star-shaped, concave on purpose
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def test_edge_index_matches_inside_on_random_polygons():
    rng = random.Random(7)
    for trial in range(40):
        rings = []
        for _ in range(rng.randint(1, 4)):
            pts = _random_ring(rng, rng.uniform(0, 30), rng.uniform(0, 30), rng.uniform(1, 12),
                               rng.randint(3, 60))
            rings.append((_bbox(pts), pts))
        indexed = [RP._EdgeRing(b, p) for b, p in rings]
        for _ in range(200):
            x, y = rng.uniform(-5, 40), rng.uniform(-5, 40)
            assert RP._inside_indexed(indexed, x, y) == RP._inside(rings, x, y), (trial, x, y)


def test_edge_index_on_edge_ties_and_horizontal_edges():
    sq = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]     # horizontal edges at y=0 and y=4
    rings = [(_bbox(sq), sq)]
    indexed = [RP._EdgeRing(b, p) for b, p in rings]
    for x in (-1.0, 0.0, 1.0, 4.0, 5.0):
        for y in (0.0, 1.0, 2.0, 3.999, 4.0, 4.001):
            assert RP._inside_indexed(indexed, x, y) == RP._inside(rings, x, y), (x, y)


def test_planes_nets_at_matches_reference_on_a_zone_file_with_a_hole(tmp_path):
    path = str(tmp_path / "z.kicad_pcb")
    open(path, "w").write(
        '(kicad_pcb (zone (net 1) (net_name "GND") (layer "In1.Cu")'
        ' (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))'
        ' (filled_polygon (layer "In1.Cu") (pts (xy 10 5) (xy 20 5) (xy 20 15) (xy 10 15))))'
        ' (zone (net 2) (net_name "VCC") (layer "In2.Cu")'
        ' (filled_polygon (layer "In2.Cu") (pts (xy 5 2) (xy 25 2) (xy 25 18) (xy 5 18)))))\n')
    P = RP.Planes(path, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    ref = _reference_grid(P.rings)
    rng = random.Random(3)
    hits = 0
    for _ in range(2000):
        x, y = rng.uniform(-2, 33), rng.uniform(-2, 23)
        for layer in ("In1.Cu", "In2.Cu", "F.Cu"):
            got = P.nets_at(layer, x, y)
            assert got == _reference_nets_at(P, ref, layer, x, y), (layer, x, y)
            hits += len(got)
    assert hits > 0
    assert P.nets_at("In1.Cu", 15, 10) == set()             # inside the hole: even-odd, no net
    assert P.nets_at("In1.Cu", 2, 2) == {"1"}                # net keyed by number here
