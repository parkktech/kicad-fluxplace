"""suggest carries its path through the final via and onto the target pad (layer and centre); synthetic 2-layer boards."""
import math
import shutil

import pytest

pcbnew = pytest.importorskip("pcbnew")
if not shutil.which("kicad-cli"):
    pytest.skip("kicad-cli missing", allow_module_level=True)

from fluxplace import handroute as H          # noqa: E402
from test_handroute import Brd, V, mm         # noqa: E402


def _pad(B, ref, net, x, y, w, h, layer):
    fp = B.pad(ref, net, x, y, w, h)
    p = fp.Pads()[0]
    ls = pcbnew.LSET()
    ls.AddLayer(layer)
    p.SetLayerSet(ls)
    return fp


def _end_board(d, ring=False, wall=False, tgt=(18.02, 0.03)):
    B = Brd()
    B.edge(-2, -8, 24, 8)
    _pad(B, "A1", "A", 2, 0, 0.5, 0.5, pcbnew.F_Cu)
    _pad(B, "A2", "A", tgt[0], tgt[1], 1.5, 1.5, pcbnew.B_Cu if ring else pcbnew.F_Cu)
    if ring:    # copper ring on the target layer 0.3 mm outside the pad: the pad is reachable only by a via inside it
        cx, cy = tgt
        for i, (dx, dy, w, h) in enumerate(((0, -1.55, 3.5, 1.0), (0, 1.55, 3.5, 1.0), (-1.55, 0, 1.0, 3.5), (1.55, 0, 1.0, 3.5))):
            _pad(B, f"R{i}", "RING", cx + dx, cy + dy, w, h, pcbnew.B_Cu)
    if wall:
        B.track("X", [(10, -7), (10, 7)], layer=pcbnew.F_Cu)
        B.track("X", [(10, -7), (10, 7)], layer=pcbnew.B_Cu)
    return B.save(d)


def _last(path):
    chunk = path.split(";")[-1]
    lay, pts = chunk.split(":") if ":" in chunk else (None, None)
    x, y = pts.split()[-1].split(",")
    return lay.strip(), float(x), float(y)


def test_suggest_ends_on_target_pad_layer_and_centre(tmp_path):
    p = _end_board(tmp_path, ring=True)
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18.02, 0.03), pitch=0.05, margin=3.0)
    assert R["found"], R
    assert "via" in R["path"] and R["vias"] == 1
    lay, x, y = _last(R["path"])
    assert lay == "B.Cu" and math.hypot(x - 18.02, y - 0.03) < 1e-3
    assert H.probe(p, "A", R["path"])["ok"]


def test_suggest_extends_grid_end_to_pad_centre(tmp_path):
    p = _end_board(tmp_path)
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18.02, 0.03), pitch=0.05, margin=3.0)
    assert R["found"], R
    lay, x, y = _last(R["path"])
    assert lay == "F.Cu" and math.hypot(x - 18.02, y - 0.03) < 1e-3
    first = R["path"].split(";")[0].split(":")[1].split()[0].split(",")
    assert math.hypot(float(first[0]) - 2, float(first[1])) < 1e-3


def test_explore_reports_then_found_for_corridor_needing_extension(tmp_path):
    p = _end_board(tmp_path, ring=True, wall=True)
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18.02, 0.03), pitch=0.05, margin=3.0, explore=True)
    assert not R["found"]
    ok = [e for e in R["explore"] if e["then_found"]]
    assert ok and ok[0]["remove"][0]["net"] == "X" and ok[0]["path"]
