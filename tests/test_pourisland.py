import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import pourisland as PI, handroute as H
from test_planetie import make, vias


def test_guarded_stitching_via_joins_open_pour_island(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    before = H.open_nets(src)["open"]
    result = PI.repair(src, out, "GND")
    assert result["accepted"], result
    assert result["action"] == "via" and vias(out) == 1
    assert H.open_nets(out)["open"] < before and H.open_nets(out)["open"] == 0


def test_net_without_pour_is_refused_without_output(tmp_path):
    src = make(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "missing.kicad_pcb")
    result = PI.repair(src, out, "NO_SUCH_NET")
    assert not result["accepted"] and not os.path.exists(out)
