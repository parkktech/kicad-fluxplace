import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import anchors as AN, routediag as RD, preroute as PR, geom as GM, boardwrite as BW
import pcbgen as g

ROOT = os.path.join(os.path.dirname(__file__), "..")


def conn(ref, x, y, nets, angle=0.0, pitch=5.0):
    """6-pin single-row connector, pads along y, nets[i] on pad i+1."""
    return g.fp(ref, x, y, [g.pad(str(i + 1), 0, (i - 2.5) * pitch, n, 0.8, 0.6) for i, n in enumerate(nets)],
                angle=angle, court=(-1, -15, 1, 15))


def twoconn(tmp_path, ang_b=0.0):
    # A at left, B at right; row of nets in order A1..A6 -> B1..B6. With B at 0 deg the pad
    # order matches; rotated 180 the pairs cross (longer, tangled).
    nets = [f"S{i}" for i in range(6)]
    b = conn("J1", 20, 40, nets) + conn("J2", 40, 40, nets, angle=ang_b)
    # a third part that pins J2's rotation question: a 2-pin ties S0..S5 mid-way
    return g.board(tmp_path, b)


def test_rotation_is_rigid_and_scored(tmp_path):
    p = g.board(tmp_path, conn("J1", 20, 40, ["A", "B", "C", "D", "E", "F"]) +
                conn("J2", 40, 40, ["F", "E", "D", "C", "B", "A"]))   # J2 wired in reverse order
    B = RD.load(p)
    res = {r["name"]: r for r in AN.evaluate(B)}
    r = res["J2"]
    assert r["best"]["delta"] == 180 and r["worse_pct"] > 15
    # rotating J2 by 180 puts pad 1 where pad 6 was, mirrored about the origin
    mv = AN.rotated(B, next(a for a in AN.detect(B) if a["name"] == "J2"), 180)
    pads = {q["num"]: q for q in mv["J2"][3]}
    old = {q["num"]: q for q in B["parts"]["J2"]["pads"]}
    assert abs(pads["1"]["x"] - (80 - old["1"]["x"])) < 1e-9 and abs(pads["1"]["y"] - (80 - old["1"]["y"])) < 1e-9
    # and the file round-trips: writing the move then reloading gives the same pad positions
    out = tmp_path / "o.kicad_pcb"
    mv = AN.moves_for_best(B, list(res.values()))
    assert len(mv) == 1                       # J1 would fix it too: only ONE of them turns
    BW.move_file(p, str(out), mv)
    assert all(r["worse_pct"] < 15 for r in AN.evaluate(RD.load(str(out))))


def test_flag_in_gate_and_threshold(tmp_path):
    p = g.board(tmp_path, conn("J1", 20, 40, list("ABCDEF")) + conn("J2", 40, 40, list("FEDCBA")))
    f, _ = PR.run(RD.load(p))
    assert any(x["code"] == "ANCHOR_MISORIENTED" for x in f)
    f2, _ = PR.run(RD.load(p), {"preroute": {"anchor_tolerance_pct": 10000}})
    assert not any(x["code"] == "ANCHOR_MISORIENTED" for x in f2)
    ok = g.board(tmp_path, conn("J1", 20, 40, list("ABCDEF")) + conn("J2", 40, 40, list("ABCDEF")), name="ok.kicad_pcb")
    assert not any(x["code"] == "ANCHOR_MISORIENTED" for x in PR.run(RD.load(ok))[0])


def test_group_rotates_about_centre_and_allowed_rotations(tmp_path):
    nets1, nets2 = list("ABCDEF"), list("GHIJKL")
    b = conn("J1", 20, 20, nets1) + conn("J2", 20, 60, nets2)
    # partners placed so the group wants a 180 turn about (20, 40)
    b += conn("J3", 60, 60, nets1) + conn("J4", 60, 20, nets2)
    p = g.board(tmp_path, b)
    B = RD.load(p)
    cons = {"placement": {"group": {"cm": {"refs": ["J1", "J2"], "centre": [20, 40], "rotations": [0, 180]}},
                          "anchor": {"J3": {"rotations": [0]}, "J4": {"rotations": [0]}}}}
    res = {r["name"]: r for r in AN.evaluate(B, cons)}
    assert [o["delta"] for o in res["cm"]["options"]] == [0, 180]
    assert res["cm"]["best"]["delta"] == 180
    mv = AN.rotated(B, AN.detect(B, cons)[0], 180)
    assert abs(mv["J1"][0] - 20) < 1e-9 and abs(mv["J1"][1] - 60) < 1e-9        # J1 went to J2's place
    assert "J1" not in res and "J2" not in res                                   # group replaces singles
    assert [o["delta"] for o in res["J3"]["options"]] == [0]


def test_edge_connector_keeps_orientation_and_outline_feasibility(tmp_path):
    p = g.board(tmp_path, conn("J1", 1.5, 40, list("ABCDEF")) + conn("J2", 40, 40, list("ABCDEF")))
    B = RD.load(p)
    a = {x["name"]: x for x in AN.detect(B)}
    assert a["J1"]["rotations"] == [0.0]
    r = {x["name"]: x for x in AN.evaluate(B, {"placement": {"anchor": {"J1": {"rotations": [0, 90]}}}})}
    assert not r["J1"]["options"][1]["feasible"]


def test_cli_anchor_check(tmp_path):
    p = g.board(tmp_path, conn("J1", 20, 40, list("ABCDEF")) + conn("J2", 40, 40, list("FEDCBA")))
    out = tmp_path / "fixed.kicad_pcb"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "anchor-check", p, "--apply",
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and "ANCHOR_MISORIENTED" in r.stdout and "rotated J" in r.stdout
    B = RD.load(str(out))
    assert sorted(B["parts"][x]["angle"] % 360 for x in ("J1", "J2")) == [0, 180]


def _apply(tmp_path, p, cons_toml, name="o.kicad_pcb"):
    c = tmp_path / "c.toml"
    c.write_text(cons_toml)
    out = tmp_path / name
    r = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "anchor-check", p, "--apply",
                        "--constraints", str(c), "--out", str(out)], capture_output=True, text=True,
                       env={**os.environ, "FLUXPLACE_NO_CALL_LOG": "1"})
    return r, out


import pytest


@pytest.mark.parametrize("side", ["F", "B"])
@pytest.mark.parametrize("d", [0, 90, 180, 270])
@pytest.mark.parametrize("start", [0.0, 90.0])
def test_apply_single_rotation_matches_analysis(tmp_path, side, d, start):
    # a lone anchor with ONE configured rotation: applied once (a delta from the current angle), at the
    # analysed pose, whatever the tolerance; the written board re-reads to the evaluated box
    p = g.board(tmp_path, conn("J1", 50, 40, list("ABCDEF"), angle=start) if side == "F" else
                g.fp("J1", 50, 40, [g.pad(str(i + 1), 0, (i - 2.5) * 5, n, 0.8, 0.6) for i, n in enumerate("ABCDEF")],
                     angle=start, side="B", court=(-1, -15, 1, 15)))
    B = RD.load(p)
    want = GM.moved(B["parts"]["J1"], 50, 40, start + d)
    r, out = _apply(tmp_path, p, f"[placement.anchor.J1]\nrotations = [{d}]\n[preroute]\nanchor_tolerance_pct = 10000\n")
    assert r.returncode == 0, r.stdout + r.stderr
    W = RD.load(str(out))["parts"]["J1"]
    assert (W["angle"] - (start + d)) % 360 == 0 and W["pos"] == (50, 40)
    assert all(abs(a - b) < 0.05 for a, b in zip(W["court"], want[1]))
    assert GM.inside(W["court"], RD.load(str(out))["bbox"])


def test_apply_skips_threshold_with_note_but_not_explicit(tmp_path):
    body = conn("J1", 20, 40, list("ABCDEF")) + conn("J2", 40, 40, list("ABCDEF"))
    p = g.board(tmp_path, body)
    # several allowed rotations: threshold decides, and the skip is reported
    r, out = _apply(tmp_path, p, "[placement.anchor.J2]\nrotations = [0, 180]\n")
    assert r.returncode == 0 and "rotated nothing" in r.stdout
    assert RD.load(str(out))["parts"]["J2"]["angle"] % 360 == 0
    # one explicit rotation: applied despite the default 15 % threshold
    r, out = _apply(tmp_path, p, "[placement.anchor.J2]\nrotations = [180]\n", "o2.kicad_pcb")
    assert "J2" in r.stdout.split("rotated ")[1].split(";")[0] and RD.load(str(out))["parts"]["J2"]["angle"] % 360 == 180
    # threshold skip names part, rotation, gain and threshold
    p2 = g.board(tmp_path, conn("J1", 20, 40, list("ABCDEF")) + conn("J2", 40, 40, list("FEDCBA")),
                 name="p2.kicad_pcb")
    notes = []
    B = RD.load(p2)
    AN.moves_for_best(B, None, {"placement": {"anchor": {"J2": {"rotations": [0, 180]}}}}, None, 1e9, notes=notes)
    n = next(x for x in notes if "J2" in x)
    assert "180" in n and "29." in n and "1e+09" in n


def test_verify_written_flags_a_different_pose(tmp_path):
    p = g.board(tmp_path, conn("J1", 50, 40, list("ABCDEF")))
    B = RD.load(p)
    a = AN.detect(B)[0]
    mv = {"J1": (50.0, 40.0, 90.0)}
    W = RD.load(p)                                    # "written" board still at 0 deg
    assert any("J1" in m for m in AN.verify_written(W, B, mv)) is False   # B unmoved too: equal
    for ref, (x, y, ang, _p, _c) in AN.rotated(B, a, 90).items():
        GM.place(B["parts"][ref], x, y, ang)
    assert any("wrote" in m for m in AN.verify_written(W, B, mv))
    assert not AN.verify_written(B, B, mv)


def test_moves_for_best_turns_an_anchor_once(tmp_path):
    p = g.board(tmp_path, conn("J1", 50, 40, list("ABCDEF")))
    B = RD.load(p)
    mv = AN.moves_for_best(B, None, {"placement": {"anchor": {"J1": {"rotations": [90]}}}}, None, 0.0)
    assert mv["J1"][2] == 90.0
