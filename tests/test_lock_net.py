"""Guarded copper locking for hand edits and whole/selected nets."""
import json
import shutil

import pytest

pcbnew = pytest.importorskip("pcbnew")
if not shutil.which("kicad-cli"):
    pytest.skip("kicad-cli missing", allow_module_level=True)

from fluxplace import handroute as H  # noqa: E402
from test_handroute import gap_board  # noqa: E402
from test_handroute_ends import _copy_pair  # noqa: E402


SAFE = "F.Cu: 4,-3 4,-7.2 16,-7.2 16,-3"


def _geometry(path):
    b = pcbnew.LoadBoard(path)
    return sorted((t.GetClass(), t.GetNetname(), t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y,
                   t.GetLayer(), t.GetWidth()) for t in b.GetTracks())


def test_lay_lock_locks_laid_copper_on_acceptance(tmp_path):
    src = gap_board(tmp_path)
    out = str(tmp_path / "locked.kicad_pcb")
    r = H.lay(src, "B", SAFE, out=out, lock=True)
    assert r["accepted"], r.get("reason")
    b = pcbnew.LoadBoard(out)
    laid = [t for t in b.GetTracks() if t.GetNetname() == "B"]
    assert laid and all(t.IsLocked() for t in laid)
    assert not next(t for t in b.GetTracks() if t.GetNetname() == "A").IsLocked()


def test_copy_net_lock_locks_copied_copper_on_acceptance(tmp_path):
    src, dst = _copy_pair(str(tmp_path))
    out = str(tmp_path / "copy-locked.kicad_pcb")
    r = H.copy_net(src, dst, "A", out=out, lock=True)
    assert r["accepted"], r.get("reason")
    copied = [t for t in pcbnew.LoadBoard(out).GetTracks() if t.GetNetname() == "A"]
    assert len(copied) == 5 and all(t.IsLocked() for t in copied)


def test_lock_net_selected_id_and_unlock_preserve_geometry(tmp_path):
    src = gap_board(tmp_path)
    uid = next(t.m_Uuid.AsString() for t in pcbnew.LoadBoard(src).GetTracks() if t.GetNetname() == "A")
    locked = str(tmp_path / "net-locked.kicad_pcb")
    unlocked = str(tmp_path / "net-unlocked.kicad_pcb")
    r = H.lock_net(src, "A", out=locked, ids=[uid[:12]])
    assert r["accepted"] and len(r["info"]["changed"]) == 1
    assert _geometry(src) == _geometry(locked)
    r = H.lock_net(locked, "A", out=unlocked, ids=[uid], unlock=True)
    assert r["accepted"] and r["info"]["changed"][0]["uuid"] == uid
    assert not next(t for t in pcbnew.LoadBoard(unlocked).GetTracks() if t.GetNetname() == "A").IsLocked()
    assert _geometry(src) == _geometry(unlocked)


def test_lock_drc_guard_ignores_only_open_endpoint_relabeling():
    base = {"keys": {("Uunconnected_items", ("old endpoint",)): 1, ("clearance", ("track",)): 1}, "unc_by_net": {"A": 1}}
    relabeled = {"keys": {("Uunconnected_items", ("new endpoint",)): 1, ("clearance", ("track",)): 1}, "unc_by_net": {"A": 1}}
    assert H._lock_drc_unchanged(relabeled, base) is None
    changed_error = {"keys": {("Uunconnected_items", ("new endpoint",)): 1, ("clearance", ("other",)): 1}, "unc_by_net": {"A": 1}}
    assert H._lock_drc_unchanged(changed_error, base) == "DRC keys changed"
    changed_open = dict(relabeled, unc_by_net={"A": 2})
    assert H._lock_drc_unchanged(changed_open, base) == "open connections by net changed"


def test_lock_net_skips_zone_refill(tmp_path, monkeypatch):
    src = gap_board(tmp_path)
    seen = []
    original = H._model

    def model(board, constraints=None, refill=True):
        seen.append(refill)
        return original(board, constraints, refill=refill)

    monkeypatch.setattr(H, "_model", model)
    r = H.lock_net(src, "A", out=str(tmp_path / "no-refill.kicad_pcb"))
    assert r["accepted"], r.get("reason")
    assert seen == [False]


def test_session_unlock_is_recorded_and_commit_preserves_geometry(tmp_path):
    src = gap_board(tmp_path, a_locked=True)
    work = str(tmp_path / "session.kicad_pcb")
    out = str(tmp_path / "committed.kicad_pcb")
    H.session_begin(src, work)
    r = H.lock_net(work, "A", unlock=True)
    assert r["accepted"], r.get("reason")
    assert H.lay(work, "B", SAFE)["accepted"]
    before = _geometry(work)
    r = H.session_commit(work, out)
    assert r["accepted"], r["reasons"]
    assert _geometry(out) == before
    ledger = json.load(open(work + ".session/ledger.json"))
    assert any(e["op"] == "lock-net" and e["accepted"] for e in ledger)
    assert not next(t for t in pcbnew.LoadBoard(out).GetTracks() if t.GetNetname() == "A").IsLocked()
