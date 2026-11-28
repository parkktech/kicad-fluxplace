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


def _blocked(path, locked=False, far_pad=False):
    """make()'s board plus a wide SIG track on B.Cu over every via site around the GND pad (SIG has a pad of its own: a net with
    no pad is not kept on load)."""
    make(path)
    b = pcbnew.LoadBoard(path)
    sig = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(sig)
    for x in ((17, 5) if far_pad else (17,)):            # far_pad: SIG joins two pads, so ripping its track opens it
        fp = pcbnew.FOOTPRINT(b)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(10)))
        pad = pcbnew.PAD(fp)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(10)))
        pad.SetNet(sig)
        pad.SetNumber("1")
        fp.Add(pad)
        b.Add(fp)
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(10)))
    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(17), pcbnew.FromMM(10)))
    tr.SetWidth(pcbnew.FromMM(5))
    tr.SetLayer(b.GetLayerID("B.Cu"))
    tr.SetNet(sig)
    tr.SetLocked(locked)
    b.Add(tr)
    b.Save(path)
    return path


def _cons(tmp_path, rail=False):
    """Constraints present (no constraints = every net hard, nothing is ripped); rail: SIG is a [power] rail."""
    p = tmp_path / "c.toml"
    p.write_text("[route]\nrail_min_pads = 8\n" + ('[power."SIG"]\nmax_current_ma = 500\n' if rail else ""))
    return str(p)


def test_k15_sealed_island_rips_the_unlocked_blocking_track_and_lays_the_via(tmp_path):
    src = _blocked(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert result["accepted"], result.get("reason")
    assert result["action"].startswith("rip+") and result["ripped"] == {"SIG": 1}
    assert vias(out) == 1 and H.open_nets(out)["open"] == 0


def test_k15_locked_blocking_track_is_never_ripped(tmp_path):
    src = _blocked(str(tmp_path / "in.kicad_pcb"), locked=True)
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert not result["accepted"] and not os.path.exists(out)


def test_k15_a_rails_copper_is_ripped_only_when_nothing_opens(tmp_path):
    src = _blocked(str(tmp_path / "in.kicad_pcb"), far_pad=True)
    out = str(tmp_path / "out.kicad_pcb")
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path, rail=True))
    assert not result["accepted"] and not os.path.exists(out)                # the rip would open the rail SIG: refused


def test_k15_a_rip_that_alone_lets_the_pour_reach_the_island_is_the_repair(tmp_path, monkeypatch):
    """W16 C112.2: ripping BT_INT_M let the F.Cu GND fill reach the pad, so the lay on the stripped copy saw nothing left to close."""
    src = _blocked(str(tmp_path / "in.kicad_pcb"))
    out = str(tmp_path / "out.kicad_pcb")
    monkeypatch.setattr(H, "lay", lambda *a, **k: {"accepted": False, "reason": "target not closed: open connections of GND 0 -> 0"})
    result = PI.repair(src, out, "GND", constraints=_cons(tmp_path))
    assert result["accepted"] and result["action"] == "rip" and result["ripped"] == {"SIG": 1}
    assert os.path.exists(out) and "SIG" in open(out).read() and vias(out) == 0
