"""Tests for fluxplace.compact — pure-python core, no pcbnew needed."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import compact as C


def mkpart(x, y, w=2.0, h=2.0, locked=False, side="F", drills=0):
    return dict(x=x, y=y, w=w, h=h, locked=locked, side=side,
                drills=drills, tht=drills > 0, angle0=0.0)


def extent(pos, parts):
    xs = [pos[r][0] for r in pos if not r.startswith("__")]
    ys = [pos[r][1] for r in pos if not r.startswith("__")]
    return max(xs) - min(xs), max(ys) - min(ys)


def test_scale_shrinks_extent():
    parts = {f"R{i}": mkpart(10.0 * (i % 5), 10.0 * (i // 5))
             for i in range(25)}
    parts["U1"] = mkpart(20, 20, locked=True)
    pos, st = C.compact(parts, 0.5, 0.5, gap=0.3, pack=0)
    w, h = extent(pos, parts)
    assert w < 41 and h < 41          # started at 40x40 of centers + slack
    assert st["resid"] == 0


def test_no_overlaps_after_legalize():
    # everything piled on one spot must separate
    parts = {f"C{i}": mkpart(5.0 + 0.1 * i, 5.0) for i in range(12)}
    pos, st = C.compact(parts, 1.0, 1.0, gap=0.4, pack=0)
    assert st["resid"] == 0
    refs = sorted(pos)
    # small parts pack at the adaptive (scaled) gap — 0.4 * 0.55
    eff = 0.4 * 0.55 - 0.01
    for i, r1 in enumerate(refs):
        for r2 in refs[i + 1:]:
            dx = abs(pos[r1][0] - pos[r2][0])
            dy = abs(pos[r1][1] - pos[r2][1])
            assert dx > 2.0 + eff or dy > 2.0 + eff, (r1, r2, dx, dy)


def test_locked_never_moves():
    parts = {"U1": mkpart(30, 30, w=10, h=10, locked=True),
             "R1": mkpart(60, 30), "R2": mkpart(0, 30)}
    pos, _ = C.compact(parts, 0.3, 0.3, pack=3)
    assert pos["U1"] == (30, 30)


def test_obstacle_keepout_same_side_and_tht():
    ob = [dict(x=30, y=30, w=20, h=20, side="B")]
    parts = {
        "U1": mkpart(30, 30, locked=True),
        "RB": mkpart(31, 29, side="B"),          # back part: must leave zone
        "JT": mkpart(29, 31, drills=6),          # THT: must leave zone
        "RF": mkpart(30, 30.5, side="F"),        # front SMD: may stay over it
    }
    pos, st = C.compact(parts, 1.0, 1.0, gap=0.3, pack=0, obstacles=ob)
    assert st["resid"] == 0
    for ref in ("RB", "JT"):
        x, y = pos[ref]
        assert abs(x - 30) >= 10 + 1 + 0.29 or abs(y - 30) >= 10 + 1 + 0.29, (
            ref, pos[ref])


def test_gravity_pack_tightens():
    parts = {f"R{i}": mkpart(15.0 * (i % 4), 15.0 * (i // 4))
             for i in range(16)}
    parts["U1"] = mkpart(22.5, 22.5, locked=True)
    _, loose = C.compact(parts, 1.0, 1.0, gap=0.3, pack=0)
    parts2 = {f"R{i}": mkpart(15.0 * (i % 4), 15.0 * (i // 4))
              for i in range(16)}
    parts2["U1"] = mkpart(22.5, 22.5, locked=True)
    pos, packed = C.compact(parts2, 1.0, 1.0, gap=0.3, pack=4)
    lw = loose["extent"][2] - loose["extent"][0]
    pw = packed["extent"][2] - packed["extent"][0]
    assert pw <= lw
    assert packed["resid"] == 0


def test_parse_obstacles():
    msgs = []
    obs = C.parse_obstacles(["10:20:30:40:B", "1:2:3:4", "junk"],
                            log=msgs.append)
    assert len(obs) == 2
    assert obs[0]["side"] == "B" and obs[1]["side"] == "F"
    assert msgs and "junk" in msgs[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(name, "OK")


def test_heavy_squeeze_converges_clean():
    # r6/r8 regression: aggressive anisotropic squeeze with locked islands
    # left 25+ real overlaps that the single-shot legalizer never cleared
    parts = {}
    for i in range(80):
        parts[f"R{i}"] = mkpart(4.0 * (i % 10), 4.0 * (i // 10),
                                w=2.5, h=1.5)
    parts["T1"] = mkpart(20, 10, w=19.4, h=9.9)     # big transformer
    parts["T2"] = mkpart(10, 25, w=9.9, h=19.4)
    parts["U1"] = mkpart(18, 16, w=5, h=24, locked=True)
    parts["U2"] = mkpart(30, 16, w=5, h=24, locked=True)
    ob = [dict(x=24, y=16, w=20, h=28, side="B")]
    pos, st = C.compact(parts, 0.55, 0.40, gap=0.42, pack=6, obstacles=ob)
    assert st["resid"] == 0, st
    refs = sorted(pos)
    for i, a in enumerate(refs):
        for b_ in refs[i + 1:]:
            dx = abs(pos[a][0] - pos[b_][0])
            dy = abs(pos[a][1] - pos[b_][1])
            ox = parts[a]["w"] / 2 + parts[b_]["w"] / 2 - dx
            oy = parts[a]["h"] / 2 + parts[b_]["h"] / 2 - dy
            assert ox <= 0.01 or oy <= 0.01, (a, b_, ox, oy)


def test_tournament_grid_parse():
    from fluxplace import tournament as TN
    g = TN.parse_compact_grid("0.44:0.47,0.5:0.45:0.4,0.55:0.4:0.42:6")
    assert g == [(0.44, 0.47, 0.45, 3), (0.5, 0.45, 0.4, 3),
                 (0.55, 0.4, 0.42, 6)]


def test_intake_scripted():
    from fluxplace import intake as IN
    answers = iter([
        "power", "signal", "3", "2",          # name, kind idx?, conn, edge/remote
    ])
    # scripted session: one power interface, remote, default panel; mounting on
    script = iter(["power", "1", "3", "2", "",     # iface: kind=power conn=microfit remote panel-default
                   "",                              # finish interfaces
                   "1", "1", "2", "5.0"])           # holes yes, corners, M2.5... wait order: holes, pattern, size, inset
    out = []
    intent = IN.run(ask=lambda p: next(script, ""), say=out.append)
    assert intent["interfaces"][0]["placement"] == "remote"
    assert "DT-compatible" in intent["interfaces"][0]["panel_connector"] or \
           intent["interfaces"][0]["panel_connector"]
    assert intent["mounting"]["holes"] is True
    assert intent["mounting"]["pattern"] == "corners"


def test_intake_answers_passthrough():
    from fluxplace import intake as IN
    a = {"interfaces": [{"name": "x", "kind": "rf", "board_connector": "ufl",
                         "placement": "edge", "panel_connector": None}],
         "mounting": {"holes": False}}
    assert IN.run(answers=a) == a
