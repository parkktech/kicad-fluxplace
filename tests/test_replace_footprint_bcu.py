"""replace_footprint on a B.Cu part: KiCad 10 segfaults on Flip of a footprint with no parent board (W14, 2026-11-19)."""
import pytest

pcbnew = pytest.importorskip("pcbnew")

_KEEP = []


def _fp(name):
    f = pcbnew.FOOTPRINT(None)
    f.SetFPID(pcbnew.LIB_ID("", name))
    for i, x in enumerate((-1.0, 1.0)):
        p = pcbnew.PAD(f)
        p.SetNumber(str(i + 1)); p.SetAttribute(pcbnew.PAD_ATTRIB_SMD); p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.6), pcbnew.FromMM(0.6)))
        ls = pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); p.SetLayerSet(ls); p.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), 0))
        f.Add(p)
    return f


def test_replace_footprint_keeps_bcu_side_position_and_rotation(tmp_path):
    from fluxplace import kicad_io as K
    lib = tmp_path / "t.pretty"; lib.mkdir()
    (lib / "NEW.kicad_mod").write_text(
        '(footprint "NEW" (version 20240108) (generator "test") (layer "F.Cu")\n'
        ' (pad "1" smd rect (at -1 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask"))\n'
        ' (pad "2" smd rect (at 1 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask"))\n)\n')
    b = pcbnew.BOARD(); _KEEP.append(b)
    old = _fp("OLD"); b.Add(old)
    old.SetReference("U1"); old.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(20)))
    old.Flip(old.GetPosition(), False); old.SetOrientationDegrees(90)
    rot, pos = old.GetOrientationDegrees(), old.GetPosition()
    K.replace_footprint(b, "U1", str(lib), "NEW")
    new = b.FindFootprintByReference("U1")
    assert new.IsFlipped() and new.GetPosition() == pos and abs(new.GetOrientationDegrees() - rot) < 1e-6
    assert new.GetFPID().GetLibItemName() == "NEW"
