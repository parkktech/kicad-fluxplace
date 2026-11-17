"""Explicit, recorded locked-copper re-escape and its commit closure contract."""
import json
import shutil

import pytest

pcbnew = pytest.importorskip("pcbnew")
if not shutil.which("kicad-cli"):
    pytest.skip("kicad-cli missing", allow_module_level=True)

from fluxplace import handroute as H  # noqa: E402
from test_handroute import Brd, gap_board, mm  # noqa: E402


SAFE = "F.Cu: 4,-3 4,-7.2 16,-7.2 16,-3"
RESTORE = "F.Cu: 2,0 18,0"


def _uid(path, net):
    return next(t.m_Uuid.AsString() for t in pcbnew.LoadBoard(path).GetTracks() if t.GetNetname() == net)


def test_allow_locked_requires_session_and_explicit_ids(tmp_path):
    src = gap_board(tmp_path, a_locked=True)
    uid = _uid(src, "A")
    out = str(tmp_path / "outside.kicad_pcb")
    r = H.shove(src, "A", out=out, ids=[uid], allow_locked=True)
    assert not r["accepted"] and "requires a session" in r["reason"]
    work = str(tmp_path / "work.kicad_pcb")
    H.session_begin(src, work)
    r = H.shove(work, "A", window_box=(8, -1, 4, 2), allow_locked=True)
    assert not r["accepted"] and "explicit --ids" in r["reason"]
    r = H.shove(work, "A", ids=[uid], allow_locked=True)
    assert r["accepted"], r.get("reason")
    item = r["info"]["removed"][0]
    assert item["unlocked_and_removed"] and item["was_locked"]
    assert item["geometry"] == {"type": "track", "start": [mm(2), mm(0)], "end": [mm(18), mm(0)],
                                "width": mm(0.25), "layer": pcbnew.F_Cu}
    ledger = json.load(open(work + ".session/ledger.json"))
    assert ledger[-1]["removed_locked"][0]["geometry"] == item["geometry"]
    assert "unlocked-and-removed" in open(work + ".session/ledger.txt").read()


def test_commit_requires_closed_and_relocked_reescape(tmp_path):
    src = gap_board(tmp_path, a_locked=True)
    work = str(tmp_path / "work.kicad_pcb")
    out = str(tmp_path / "committed.kicad_pcb")
    H.session_begin(src, work)
    assert H.shove(work, "A", ids=[_uid(work, "A")], allow_locked=True)["accepted"]
    rejected = H.session_commit(work, out)
    assert not rejected["accepted"]
    assert any("locked re-escape left opened" in reason for reason in rejected["reasons"])
    assert H.lay(work, "A", RESTORE)["accepted"]
    assert H.lay(work, "B", SAFE, lock=True)["accepted"]
    rejected = H.session_commit(work, out)
    assert not rejected["accepted"]
    assert any("still unlocked" in reason for reason in rejected["reasons"])
    assert H.lock_net(work, "A")["accepted"]
    accepted = H.session_commit(work, out)
    assert accepted["accepted"], accepted["reasons"]
    assert accepted["locked_reescape"] == ["A"]
    assert all(t.IsLocked() for t in pcbnew.LoadBoard(out).GetTracks())


def test_explore_include_locked_names_separate_board_locked_candidates(tmp_path):
    board = Brd()
    board.edge(-2, -8, 22, 8)
    board.pad("B1", "B", 4, -3)
    board.pad("B2", "B", 16, -3)
    board.track("A", [(10, -7), (10, 7)], locked=True)
    src = board.save(tmp_path)
    r = H.suggest(src, "B", src_pt=(4, -3), dst_pt=(16, -3), layers=["F.Cu"], pitch=0.1, explore=True, include_locked=True)
    assert "locked_explore" in r
    assert r["locked_explore"]
    items = [item for candidate in r["locked_explore"] for item in candidate["remove"]]
    assert any(item["net"] == "A" and item["lock_origin"] == "board" for item in items)
    assert all(item["locked"] for item in items)
    assert all(not item.get("locked") for candidate in r["explore"] for item in candidate["remove"])
