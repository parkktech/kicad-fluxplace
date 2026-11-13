"""`pairs`: differential pairs laid by construction on synthetic boards (pure text; kicad-cli DRC when it is installed)."""
import json
import math
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import pairgeo as G
from fluxplace import pairs as PR
from fluxplace import routediag as RD

W, GAP, CLR = 0.15, 0.2, 0.127


def duo(ref, x, y, p, n, pitch=0.5, layer="F.Cu", w=0.3, h=0.8):
    pads = [PB.pad("1", 0, 0, p, w, h), PB.pad("2", pitch, 0, n, w, h)]
    return PB.fp(ref, x, y, [q.replace('(layers "F.Cu")', f'(layers "{layer}")') for q in pads])


def lay(tmp, body, cons=None):
    b = PB.write(tmp, body)
    out = str(tmp / "o.kicad_pcb")
    rep = PR.run(b, cons or {}, None, out)
    return rep, RD.load(out), open(out).read()


def clean(B, nets):
    """smallest exact gap of the laid copper of `nets` to every foreign item (an independent check of the result)."""
    I = G.Items.of_board(B)
    worst = 9.0
    for s in B["segs"]:
        if s["net"] in nets:
            worst = min(worst, I.gap_seg(s["a"], s["b"], s["w"], s["layer"], s["net"], 0.5)[0])
    for v in B["vias"]:
        if v["net"] in nets:
            worst = min(worst, I.gap_via(v["x"], v["y"], v["d"], v["net"], 0.5)[0])
    return worst


def coupled_share(B, p, n, pitch):
    """fraction of the P leg's length that runs parallel to an N segment at the pair pitch."""
    tot = cov = 0.0
    N = [s for s in B["segs"] if s["net"] == n]
    for s in B["segs"]:
        if s["net"] != p:
            continue
        L = math.hypot(s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
        tot += L
        mid = ((s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2)
        if any(q["layer"] == s["layer"] and abs(G.pt_seg(mid, q["a"], q["b"]) - pitch) < 0.01 for q in N):
            cov += L
    return cov / max(tot, 1e-9)


def test_straight_pair_is_coupled_locked_and_matched(tmp_path):
    rep, B, txt = lay(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 20, 40, "D_P", "D_N"))
    assert rep["verdict"] == "PAIRS_OK" and rep["complete"] == rep["total"] == 1
    r = rep["pairs"][0]
    assert r["vias"] == 0 and r["layers"] == ["F.Cu"] and r["skew"] <= 0.05
    assert 19.0 < r["length"][0] < 21.0
    segs = [s for s in B["segs"] if s["net"] in ("D_P", "D_N")]
    assert segs and all(s["locked"] and abs(s["w"] - W) < 1e-6 for s in segs)
    assert coupled_share(B, "D_P", "D_N", W + GAP) > 0.9
    assert clean(B, {"D_P", "D_N"}) >= CLR


def test_bend_round_an_obstacle(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 40, 30, "D_P", "D_N") + PB.wall("W1", 30, 25, 12)
    rep, B, _ = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["vias"] == 0
    assert r["skew"] <= r["limit"] and r["skew"] <= 0.3            # corners are compensated with a bump
    assert clean(B, {"D_P", "D_N"}) >= CLR
    assert coupled_share(B, "D_P", "D_N", W + GAP) > 0.8
    # every corner is 45 degrees
    for net in ("D_P", "D_N"):
        ss = [s for s in B["segs"] if s["net"] == net]
        for s in ss:
            dx, dy = abs(s["b"][0] - s["a"][0]), abs(s["b"][1] - s["a"][1])
            assert min(dx, dy) < 2e-3 or abs(dx - dy) < 2e-3


def test_one_layer_change_is_a_matched_via_pair(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 30, 30, "D_P", "D_N", layer="B.Cu")
    rep, B, _ = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["vias"] == 1 and r["layers"] == ["F.Cu", "B.Cu"]
    vp = [v for v in B["vias"] if v["net"] == "D_P"]
    vn = [v for v in B["vias"] if v["net"] == "D_N"]
    assert len(vp) == len(vn) == 1 and vp[0]["locked"]
    d = math.hypot(vp[0]["x"] - vn[0]["x"], vp[0]["y"] - vn[0]["y"])
    assert vp[0]["d"] + CLR <= d < 1.2                             # side by side, a clearance apart
    assert r["skew"] <= 0.3 and clean(B, {"D_P", "D_N"}) >= CLR


def test_series_caps_make_two_sections(tmp_path):
    # driver -> AC caps (C1 on P, C2 on N, side by side) -> receiver: TX_P/TX_N and RX_P/RX_N are separate pair sections
    caps = PB.fp("C1", 30, 20, [PB.pad("1", 0, -0.5, "TX_P", 0.5, 0.5), PB.pad("2", 0, 0.5, "RX_P", 0.5, 0.5)]) + \
        PB.fp("C2", 31, 20, [PB.pad("1", 0, -0.5, "TX_N", 0.5, 0.5), PB.pad("2", 0, 0.5, "RX_N", 0.5, 0.5)])
    body = duo("U1", 30, 10, "TX_P", "TX_N") + caps + duo("J1", 30, 32, "RX_P", "RX_N")
    rep, B, _ = lay(tmp_path, body)
    assert rep["complete"] == rep["total"] == 2
    for r in rep["pairs"]:
        assert r["vias"] == 0 and r["skew"] <= 0.3
        assert [s["from"] for s in r["sections"]] and all(s["ok"] for s in r["sections"])
    assert clean(B, {"TX_P", "TX_N", "RX_P", "RX_N"}) >= CLR


def test_blocked_pair_is_reported_not_dropped(tmp_path):
    cage = PB.keepout(17, 16.5, 23.5, 17) + PB.keepout(17, 23, 23.5, 23.5) + PB.keepout(17, 16.5, 17.5, 23.5) + PB.keepout(23, 16.5, 23.5, 23.5)
    rep, B, txt = lay(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 60, 40, "D_P", "D_N") + cage)
    assert rep["verdict"] == "PAIRS_WARN" and rep["complete"] == 0
    r = rep["pairs"][0]
    assert r["status"] != "complete" and r["why"] and r["blockers"]
    assert "closest approach" in r["why"]
    assert not [s for s in B["segs"] if s["net"] in ("D_P", "D_N")]            # nothing half laid
    assert "PAIR_NOT_LAID" in PR.format_text(rep)


def test_plan_only_writes_nothing_and_returns_corridors(tmp_path):
    b = PB.write(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 40, 30, "D_P", "D_N"))
    rep = PR.run(b, {}, None, None, plan_only=True)
    assert rep["plan_only"] and rep["complete"] == 1
    c = rep["pairs"][0]["sections"][0]["corridor"]
    assert c and c[0]["layer"] == "F.Cu" and len(c[0]["pts"]) >= 2 and c[0]["width"] > 2 * W + GAP


def test_width_and_gap_come_from_the_project_net_class(tmp_path):
    b = PB.write(tmp_path, duo("U1", 20, 20, "USB_DP", "USB_DM", pitch=0.8) + duo("J1", 20, 40, "USB_DP", "USB_DM", pitch=0.8))
    json.dump({"net_settings": {"classes": [
        {"name": "Default", "clearance": 0.15, "track_width": 0.2, "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "USB90", "clearance": 0.15, "track_width": 0.2, "diff_pair_width": 0.26, "diff_pair_gap": 0.18, "priority": 0}],
        "netclass_patterns": [{"netclass": "USB90", "pattern": "USB_*"}]}}, open(str(tmp_path / "b.kicad_pro"), "w"))
    out = str(tmp_path / "o.kicad_pcb")
    rep = PR.run(b, {}, None, out)
    B = RD.load(out)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["class"] == "USB90" and r["width"] == 0.26 and r["gap"] == 0.18
    assert all(abs(s["w"] - 0.26) < 1e-6 for s in B["segs"] if s["net"].startswith("USB_"))
    assert coupled_share(B, "USB_DP", "USB_DM", 0.26 + 0.18) > 0.85
    assert clean(B, {"USB_DP", "USB_DM"}) >= 0.15


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")
def test_laid_copper_passes_kicad_drc(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 40, 30, "D_P", "D_N", layer="B.Cu") + PB.wall("W1", 30, 25, 12)
    b = PB.write(tmp_path, body)
    out = str(tmp_path / "o.kicad_pcb")
    assert PR.run(b, {}, None, out)["complete"] == 1
    rp = str(tmp_path / "drc.json")
    subprocess.run(["kicad-cli", "pcb", "drc", "--severity-all", "--format", "json", "--output", rp, out], capture_output=True, timeout=120)
    d = json.load(open(rp))
    bad = [v for v in d.get("violations", []) if v["type"] in ("clearance", "shorting_items", "tracks_crossing", "hole_clearance", "track_dangling")]
    assert not bad, bad[:3]
    assert not [u for u in d.get("unconnected_items", []) if "D_" in json.dumps(u)]
