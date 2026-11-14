"""[lock.at] is authoritative: a part away from its lock is moved there before the first placement stage."""
import argparse, os, sys
import pytest
pcbnew = pytest.importorskip("pcbnew")   # imported at collection: test_core later parks a stub in sys.modules
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import planboards as PB
from fluxplace import lockat as LA, pipeline as PL, review as R


def _b(tmp_path):
    two = lambda r, x, y, a, b: PB.fp(r, x, y, [PB.pad("1", -0.5, 0, a), PB.pad("2", 0.5, 0, b)])
    return PB.write(tmp_path, two("H1", 10, 10, "", "") + two("H2", 90, 70, "", "") + two("R1", 40, 30, "A", "B"))


def _pos(path, ref):
    import pcbnew
    for f in pcbnew.LoadBoard(path).GetFootprints():
        if f.GetReference() == ref:
            return pcbnew.ToMM(f.GetPosition().x), pcbnew.ToMM(f.GetPosition().y)


def test_apply_moves_to_lock(tmp_path):
    b = _b(tmp_path)
    cons = {"lock": {"at": {"H1": [20.5, 15.25, "F"], "H2": [80, 60]}}}
    out = str(tmp_path / "o.kicad_pcb")
    moved = LA.apply(b, out, cons)
    assert sorted(m[0] for m in moved) == ["H1", "H2"]
    assert _pos(out, "H1") == pytest.approx((20.5, 15.25), abs=1e-3)
    assert _pos(out, "R1") == pytest.approx((40, 30), abs=1e-3)
    assert LA.apply(out, str(tmp_path / "o2.kicad_pcb"), cons) == []      # idempotent


def test_conflicts_fail_loudly(tmp_path):
    b = _b(tmp_path)
    for at, word in (({"H1": [30, 30], "H2": [30, 30]}, "share"), ({"H1": [500, 5]}, "outside"), ({"NOPE": [1, 1]}, "NOPE")):
        with pytest.raises(LA.LockError, match=word):
            LA.apply(b, str(tmp_path / "x.kicad_pcb"), {"lock": {"at": at}})


def test_pipeline_first_stage_uses_locked_board(tmp_path):
    b = _b(tmp_path)
    cp = tmp_path / "c.toml"
    cp.write_text('[lock]\n[lock.at]\nH1 = [22.0, 18.0, "F"]\n')
    a = argparse.Namespace(constraints=str(cp))
    out = PL._apply_lock_at(a, str(tmp_path / "W"), b)
    assert out != b and _pos(out, "H1") == pytest.approx((22.0, 18.0), abs=1e-3)
    assert _pos(b, "H1") == pytest.approx((10, 10), abs=1e-3)              # the seed is untouched
    from fluxplace import constraints as C, boardfile as BF
    assert R.check_locks(BF.facts_from_file(out), C.load(str(cp))) == []        # the position gate agrees
    assert [f["code"] for f in R.check_locks(BF.facts_from_file(b), C.load(str(cp)))] == ["LOCK_MOVED"]
