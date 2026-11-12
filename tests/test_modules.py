"""modules.py + floorplan2.py on synthetic boards (text model; the isolated fan-out needs pcbnew and is not exercised here)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import modules as MD, routediag as RD, floorplan2 as FP


def board(tmp, far_cap=False):
    sig = [f"S{i}" for i in range(8)]
    body = PB.column_ic("U1", 40, 40, ["VX"] + sig[1:])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    body += PB.fp("C2", 80 if far_cap else 36, 55, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    body += PB.fp("J1", 80, 20, [PB.pad(str(i + 1), 0, i * 2.0, n, 1.2, 1.2) for i, n in enumerate(sig)])
    body += PB.fp("U2", 60, 40, [PB.pad(str(i + 1), 0, i * 1.0, n, 0.6, 0.6) for i, n in enumerate(["Q0", "GND", "S0", "S1", "S2", "S3"])])
    return PB.write(tmp, body)


def mods(tmp, **kw):
    B = RD.load(board(tmp, **kw))
    return B, MD.build(B, {}, None, arrange=False)


def test_ic_with_support_is_one_module(tmp_path):
    B, M = mods(tmp_path)
    of = M["of"]
    assert of["C1"] == of["U1"]
    names = {m["name"] for m in M["modules"]}
    assert {"U1", "J1", "U2"} <= names
    m = next(m for m in M["modules"] if m["name"] == "U1")
    assert m["anchor"] == "U1" and "C1" in m["members"]
    assert m["area"] > 0 and len(m["bbox"]) == 4


def test_far_support_stands_alone(tmp_path):
    B, M = mods(tmp_path, far_cap=True)
    assert M["of"]["C2"] != M["of"]["U1"]


def test_pose_rotation_is_rigid(tmp_path):
    B, M = mods(tmp_path)
    m = next(m for m in M["modules"] if m["name"] == "U1")
    p0 = MD.pose(m, 10.0, 20.0, 0)
    p90 = MD.pose(m, 10.0, 20.0, 90)
    for r in m["members"]:
        d0 = math.hypot(p0[r][0] - 10, p0[r][1] - 20)
        d1 = math.hypot(p90[r][0] - 10, p90[r][1] - 20)
        assert abs(d0 - d1) < 1e-9
        assert abs(((p90[r][2] - p0[r][2]) - 90 + 180) % 360 - 180) < 1e-9


def test_ports_per_side_count_external_pins(tmp_path):
    B, M = mods(tmp_path)
    m = next(m for m in M["modules"] if m["name"] == "U1")
    assert sum(m["ports_per_side"].values()) >= 7          # S1..S7 leave the block
    assert "S1" in m["ports"] and m["ports"]["S1"]["pins"] == 1


def test_fixed_connector_is_fixed_module(tmp_path):
    B = RD.load(board(tmp_path))
    M = MD.build(B, {"placement": {"fixed": ["J1"]}}, None, arrange=False)
    assert next(m for m in M["modules"] if m["name"] == "J1")["fixed"]
    assert not next(m for m in M["modules"] if m["name"] == "U2")["fixed"]


def test_grow_outline_and_riders(tmp_path):
    p = board(tmp_path)
    text = open(p).read()
    B = RD.load(p)
    t2 = FP.grow_text(text, B["bbox"], (0, 3, 0, 0))
    open(str(tmp_path / "g.kicad_pcb"), "w").write(t2)
    B2 = RD.load(str(tmp_path / "g.kicad_pcb"))
    assert abs((B2["bbox"][2] - B["bbox"][2]) - 3.0) < 1e-6 and abs(B2["bbox"][0] - B["bbox"][0]) < 1e-6
