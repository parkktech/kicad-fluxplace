"""solder_mask_bridge (real kicad-cli DRC): a [thermal.*] mask opening must expose only copper of its net.  A foreign track that crosses
the block with BOTH ends outside it used to stay in the aperture (the carve looked at track ends only), and one such track makes KiCad
report every GND item of the aperture as bridging."""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import thermal as TH  # noqa: E402

CONS = {"thermal": {"t": {"rect": [6, 6, 20, 18], "side": "B", "net": "GND", "cut_mm": 0.0, "mask_open": True, "grow_mm": 0.25, "sliver_mm": 0.5}}}


def _board(path):
    PN = pcbnew
    b = PN.BOARD()
    mm = PN.FromMM
    V = lambda x, y: PN.VECTOR2I(mm(x), mm(y))
    gnd, sig = PN.NETINFO_ITEM(b, "GND"), PN.NETINFO_ITEM(b, "SIG")
    b.Add(gnd)
    b.Add(sig)
    for a, c in (((0, 0), (40, 0)), ((40, 0), (40, 30)), ((40, 30), (0, 30)), ((0, 30), (0, 0))):
        s = PN.PCB_SHAPE(b)
        s.SetShape(PN.SHAPE_T_SEGMENT)
        s.SetStart(V(*a))
        s.SetEnd(V(*c))
        s.SetLayer(PN.Edge_Cuts)
        s.SetWidth(mm(.1))
        b.Add(s)
    z = PN.ZONE(b)
    z.SetLayer(PN.B_Cu)
    z.SetNet(gnd)
    o = z.Outline()
    o.NewOutline()
    for p in ((1, 1), (39, 1), (39, 29), (1, 29)):
        o.Append(mm(p[0]), mm(p[1]))
    b.Add(z)
    for x in (10, 12):
        v = PN.PCB_VIA(b)
        v.SetPosition(V(x, 10))
        v.SetWidth(mm(.46))
        v.SetDrill(mm(.2))
        v.SetNet(gnd)
        v.SetViaType(PN.VIATYPE_THROUGH)
        v.SetLayerPair(PN.F_Cu, PN.B_Cu)
        b.Add(v)
    t = PN.PCB_TRACK(b)                                  # crosses the block (x 6..20) with both ends outside it
    t.SetStart(V(3, 12))
    t.SetEnd(V(25, 12))
    t.SetWidth(mm(.2))
    t.SetLayer(PN.B_Cu)
    t.SetNet(sig)
    b.Add(t)
    PN.SaveBoard(str(path), b)
    open(os.path.splitext(str(path))[0] + ".kicad_pro", "w").write("{}")


def _bridges(path):
    out = str(path) + ".json"
    subprocess.run(["kicad-cli", "pcb", "drc", "--refill-zones", "--format", "json", "--severity-all", "--output", out, str(path)], capture_output=True)
    return sum(1 for v in json.load(open(out))["violations"] if v["type"] == "solder_mask_bridge")


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli missing")
def test_recut_carves_a_track_that_crosses_the_block_with_both_ends_outside(tmp_path):
    src, dst = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    _board(src)
    r = TH.recut(str(src), CONS, str(dst))
    assert r["blocks"] == 1 and "1 track(s)" in r["log"][0]
    assert _bridges(dst) == 0
