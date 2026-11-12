"""Regression: `place --strategy pack` blew a real 337-part board out to > 1 m.
Cause: placement._compact_blocks had no translation anchor and walked the whole block
arrangement ~4 mm per sweep (2 m after 500 sweeps), while locked parts stayed at
their real coordinates. Plus the guard that makes every strategy fail loudly."""
import os, random, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import placement as P

# block sizes (W, H) mm measured on the failing board: one 210-part sheet + 11 small ones
DIMS = [(84, 189), (22, 42), (15, 29), (27, 46), (10, 18), (28, 43), (15, 25), (13, 22), (13, 22),
        (17, 28), (15, 24), (12, 19)]


def blocks_and_edges():
    blocks = {f"g{i}": {"W": w, "H": h} for i, (w, h) in enumerate(DIMS)}
    rnd = random.Random(1)
    names = list(blocks)
    cedge = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if rnd.random() < 0.6:
                cedge[(names[i], names[j])] = rnd.uniform(0.5, 6)
    return blocks, cedge


def test_compact_blocks_does_not_walk_away():
    blocks, cedge = blocks_and_edges()
    bpos = P._arrange_blocks(blocks, cedge, "g0", 0.0, 0.0)
    shelf = {k: list(v) for k, v in bpos.items()}
    P._compact_blocks(blocks, bpos, cedge, gap=2.0, iters=500)
    cen = lambda pp: sum(v[0] * blocks[k]["W"] * blocks[k]["H"] for k, v in pp.items()) / sum(
        b["W"] * b["H"] for b in blocks.values())
    assert abs(cen(bpos) - cen(shelf)) < 5, (cen(bpos), cen(shelf))   # was ~ -2000
    assert P._blocks_area(blocks, bpos) < 1.5 * P._blocks_area(blocks, shelf)


def mk(x, y, w=2.0, h=2.0, locked=False):
    return dict(x=x, y=y, w=w, h=h, locked=locked, side="F", drills=0, tht=False, angle0=0.0)


def test_check_extent_fixed_outline_and_growth():
    parts = {"R1": mk(10, 10), "R2": mk(50, 40)}
    P.check_extent(parts, {"R1": (10, 10), "R2": (50, 40)}, {}, (0, 0, 100, 80), redraw=False)
    with pytest.raises(P.PlacementError, match="outside the board outline"):
        P.check_extent(parts, {"R1": (10, 10), "R2": (150, 40)}, {}, (0, 0, 100, 80), redraw=False)
    with pytest.raises(P.PlacementError, match="more than|x the area|limit"):
        P.check_extent(parts, {"R1": (-2000, 10), "R2": (50, 40)}, {}, (0, 0, 100, 80), redraw=True)
    with pytest.raises(P.PlacementError, match="1 m"):
        P.check_extent(parts, {"R1": (-2000, 10), "R2": (50, 40)}, {}, None, redraw=True)
    # a modest regrowth is fine
    P.check_extent(parts, {"R1": (-20, 10), "R2": (90, 70)}, {}, (0, 0, 100, 80), redraw=True)
