"""fluxplace fanout on synthetic boards. Needs /usr/bin/python3 with pcbnew (the helper runs there)."""
import argparse
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import fanout as FO

HAVE = os.path.exists("/usr/bin/python3") and subprocess.run(
    ["/usr/bin/python3", "-c", "import pcbnew"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not HAVE, reason="pcbnew not available")


def args(**kw):
    d = dict(track=None, clearance=None, via=None, drill=None, via_pad_gap=0.2, reach=2.2, lane=1.0, max_dom=60, nodes=200000,
             repairs=12, check_only=False)
    d.update(kw)
    return argparse.Namespace(**d)


def go(tmp, body, refs=None, **kw):
    path = PB.write(tmp, body)
    out = str(tmp / "out.kicad_pcb")
    B, res = FO.run(path, {}, None, out, args(**kw), refs)
    return path, out, B, res, FO.summarise(B, res)


def sink(ref, x, y, nets):
    """a far-away connector that gives every net its second pad."""
    return PB.fp(ref, x, y, [PB.pad(str(i + 1), 0, i * 2.0, n, 1.2, 1.2) for i, n in enumerate(nets)])


def in_pad(res, pads):
    bad = []
    for p in res["pins"]:
        if p.get("via"):
            vx, vy = p["via"]
            for (x0, y0, x1, y1) in pads:
                if x0 - 0.225 < vx < x1 + 0.225 and y0 - 0.225 < vy < y1 + 0.225:
                    bad.append((p["ref"], p["pin"]))
    return bad


def test_qfn_all_escape_locked_tagged(tmp_path):
    sig = [f"S{i}" for i in range(8)]
    body = PB.column_ic("U1", 40, 40, sig) + sink("J1", 80, 20, sig) + sink("J2", 20, 20, [f"EU1{i}" for i in range(8)])
    _, out, B, res, per = go(tmp_path, body)
    assert per["U1"]["ok"] == per["U1"]["req"] >= 16
    txt = open(out).read()
    assert '(name "fanout:U1")' in txt or '"fanout:U1"' in txt
    assert txt.count("(locked yes)") + txt.count("locked") >= per["U1"]["req"]
    assert all(p["ok"] for p in res["pins"])
    assert all(p.get("lane") for p in res["pins"] if p["need"] == "signal")


def test_two_row_04mm_connector(tmp_path):
    n = 10
    top = [f"T{i}" for i in range(n)]
    bot = [f"B{i}" for i in range(n)]
    pads = [PB.pad(str(i + 1), (i - n / 2) * 0.4, -1.5, top[i], 0.2, 0.7) for i in range(n)]
    pads += [PB.pad(str(n + i + 1), (i - n / 2) * 0.4, 1.5, bot[i], 0.2, 0.7) for i in range(n)]
    body = PB.fp("J5", 40, 40, pads, court=(-3, -2.5, 3, 2.5)) + sink("J1", 80, 10, top) + sink("J2", 80, 50, bot)
    _, out, B, res, per = go(tmp_path, body)
    assert per["J5"]["ok"] == per["J5"]["req"] == 2 * n
    assert not in_pad(res, [(0, 0, 0, 0)])


def test_bga_grid(tmp_path):
    nets = [f"G{i}" for i in range(16)]
    pads = [PB.pad(str(i + 1), (i % 4 - 1.5) * 0.8, (i // 4 - 1.5) * 0.8, nets[i], 0.4, 0.4) for i in range(16)]
    body = PB.fp("U2", 40, 40, pads, court=(-2.4, -2.4, 2.4, 2.4)) + sink("J1", 80, 10, nets)
    _, out, B, res, per = go(tmp_path, body, reach=3.0)
    assert per["U2"]["req"] == 16
    assert per["U2"]["ok"] >= 12            # the 4 inner balls need a via; the ring must leave a lane
    vias = [p["via"] for p in res["pins"] if p.get("via")]
    assert len(vias) == len(set(map(tuple, vias)))


def test_power_pins_to_plane_get_stub_and_via(tmp_path):
    caps = "".join(PB.fp(f"C{i}", 20 + 4 * i, 30, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)]) for i in range(5))
    caps += PB.fp("J1", 60, 60, [PB.pad("1", 0, 0, "VX", 1, 1), PB.pad("2", 3, 0, "GND", 1, 1)])
    _, out, B, res, per = go(tmp_path, caps)
    gnd = [p for p in res["pins"] if p["net"] == "GND" and p["ref"].startswith("C")]
    assert len(gnd) == 5 and all(p["ok"] and p["via"] for p in gnd)
    for p in gnd:                                    # via beside the pad, never in it
        cx = {"C0": 20.5, "C1": 24.5, "C2": 28.5, "C3": 32.5, "C4": 36.5}[p["ref"]]
        assert not (abs(p["via"][0] - cx) < 0.25 + 0.225 and abs(p["via"][1] - 30) < 0.25 + 0.225)
    assert not [p for p in res["pins"] if p["net"] == "VX"]       # a local rail needs no escape


def test_pair_legs_symmetric(tmp_path):
    nets = ["D_P", "D_N", "S0", "S1", "S2", "S3", "S4", "S5"]
    body = PB.column_ic("U1", 40, 40, nets) + sink("J1", 80, 20, nets) + sink("J2", 20, 20, [f"EU1{i}" for i in range(8)])
    _, out, B, res, per = go(tmp_path, body)
    p = {q["net"]: q for q in res["pins"] if q["ref"] == "U1"}
    assert p["D_P"]["ok"] and p["D_N"]["ok"]
    assert p["D_P"]["kind"] == p["D_N"]["kind"] and p["D_P"]["layer"] == p["D_N"]["layer"]
    assert bool(p["D_P"]["via"]) == bool(p["D_N"]["via"])


def test_blocked_pin_reported_with_blocker(tmp_path):
    sig = [f"S{i}" for i in range(6)]
    wall = PB.wall("R1", 36.0, 40.0, 40, "W", 0.4, 0.3, 4.0)
    body = PB.column_ic("U1", 40, 40, sig) + sink("J1", 80, 20, sig) + wall + sink("J3", 20, 60, [f"W{i}" for i in range(40)])
    _, out, B, res, per = go(tmp_path, body)
    bl = [p for p in res["pins"] if not p["ok"]]
    assert bl and all(p.get("blocker") for p in bl)


def test_deterministic_and_check_only_writes_nothing(tmp_path):
    sig = [f"S{i}" for i in range(8)]
    body = PB.column_ic("U1", 40, 40, sig) + sink("J1", 80, 20, sig)
    path = PB.write(tmp_path, body)
    r1 = FO.run(path, {}, None, str(tmp_path / "o1.kicad_pcb"), args(), None)[1]
    r2 = FO.run(path, {}, None, str(tmp_path / "o2.kicad_pcb"), args(), None)[1]
    assert json.dumps(r1["pins"], sort_keys=True) == json.dumps(r2["pins"], sort_keys=True)
    c = str(tmp_path / "o3.kicad_pcb")
    FO.run(path, {}, None, c, args(check_only=True), None)
    assert not os.path.exists(c)


def _copper_in(txt, rect, margin=0.0):
    """vias / segment endpoints of the written fan-out inside `rect` (x0, y0, x1, y1) grown by margin."""
    import re
    x0, y0, x1, y1 = rect
    bad = []
    for m in re.finditer(r"\(via\s+\(at ([-\d.]+) ([-\d.]+)\)", txt):
        x, y = float(m.group(1)), float(m.group(2))
        if x0 - margin < x < x1 + margin and y0 - margin < y < y1 + margin:
            bad.append(("via", x, y))
    for m in re.finditer(r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)", txt):
        for x, y in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
            x, y = float(x), float(y)
            if x0 - margin < x < x1 + margin and y0 - margin < y < y1 + margin:
                bad.append(("seg", x, y))
    return bad


def _caps(n=5, y=30, x0=20):
    return "".join(PB.fp(f"C{i}", x0 + 4 * i, y, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
                   for i in range(n)) + PB.fp("J1", 60, 60, [PB.pad("1", 0, 0, "VX", 1, 1), PB.pad("2", 3, 0, "GND", 1, 1)])


def test_fanout_honours_board_and_footprint_keepouts(tmp_path):
    # a keep-out on the natural via site of every cap: even caps board-level, odd caps owned by a footprint (a connector's)
    kos, body = [], _caps()
    for i in range(5):
        r = (21.0 + 4 * i, 29.0, 22.6 + 4 * i, 31.0)
        kos.append(r)
        body += PB.keepout(*r) if i % 2 == 0 else PB.fp(f"K{i}", 90, 10 + 5 * i, [PB.pad("1", 0, 0, f"Z{i}", 1, 1), PB.keepout(*r)])
    _, out, B, res, per = go(tmp_path, body)
    txt = open(out).read()
    for r in kos:
        assert not _copper_in(txt, r, 0.0), r
    gnd = [p for p in res["pins"] if p["net"] == "GND" and p["ref"].startswith("C")]
    assert gnd and all(p["ok"] or p.get("blocker") for p in gnd)
    assert sum(1 for p in gnd if p["ok"]) >= 3                  # they escape elsewhere, not just fail


def test_fanout_keeps_board_edge_clearance(tmp_path):
    body = _caps(y=1.2)                                        # caps almost on the 0..100 x 0..80 outline
    _, out, B, res, per = go(tmp_path, body)
    import re
    for m in re.finditer(r"\(via\s+\(at ([-\d.]+) ([-\d.]+)\)", open(out).read()):
        x, y = float(m.group(1)), float(m.group(2))
        assert y - 0.225 >= 0.25 - 1e-6 and x - 0.225 >= 0.25 - 1e-6        # copper to edge >= the helper's edge floor


def test_reserved_escapes_in_keepout_or_edge_band_are_dropped(tmp_path):
    """escres plans on a raster that knows no keep-outs or edge: fanout.py must drop what it cannot legally claim."""
    body = _caps(n=1) + PB.keepout(40, 40, 44, 44) + PB.fp("K1", 90, 10, [PB.pad("1", 0, 0, "Z", 1, 1), PB.keepout(50, 20, 54, 24)])

    def claim(pin, x, y, lane_end=None):
        return {"ref": "C0", "pin": pin, "net": "GND", "layer": "F.Cu", "pad_xy": [x - 1.0, y], "site": [x, y],
                "lane": [[lane_end[0], lane_end[1]] if lane_end else [x, y]], "width": 0.15, "via": 0.45}
    claims = [claim("1", 42, 42), claim("2", 52, 22), claim("3", 0.3, 40), claim("4", 70, 60, (70.1, 60.0)), claim("5", 20, 50)]
    path = PB.write(tmp_path, body)
    logs = []
    job = {"params": FO.params(args(), __import__("fluxplace.escapeplan", fromlist=["x"]).plan_params({}), path), "signal_layers": ["F.Cu", "B.Cu"]}
    kept = FO._drop_illegal_claims(path, job, claims, str(tmp_path), logs.append)
    assert [c["pin"] for c in kept] == ["4", "5"]
    assert len(logs) == 3 and any("keepout" in l for l in logs) and any("edge" in l for l in logs)
    assert kept[0]["lane"][-1] == [70, 60]                      # the stub ends at the via centre
