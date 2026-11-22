"""PHASE3I B2 / F5: replay of region session ledgers onto one merge board. Real boards and real H.lay (kicad-cli DRC);
the R6 cases stub only the measure (GP._remeasure), the guard (GP._accept) runs for real."""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pcbnew = pytest.importorskip("pcbnew")
from fluxplace import handroute as HR, gaps as GP  # noqa: E402
from test_handroute import Brd  # noqa: E402

B_PATH, C_PATH = "F.Cu: 4,-3 16,-3", "F.Cu: 4,3 16,3"
X_PATH = "F.Cu: 10,-6 10,-1"                            # crosses B_PATH only (C runs at y = 3)


def _two_nets(d, extra=False):
    os.makedirs(d, exist_ok=True)
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("B1", "B", 4, -3)
    B.pad("B2", "B", 16, -3)
    B.pad("C1", "C", 4, 3)
    B.pad("C2", "C", 16, 3)
    if extra:
        B.pad("X1", "X", 10, -6)
        B.pad("X2", "X", 10, -1)
    return B.save(d)


def _ledger_of(session):
    return os.path.join(session["session"], "ledger.txt")


def _ledger(path, rows):
    L = [{"n": i + 1, "t": "2026-11-22 10:00:0%d" % i, "op": op, "accepted": acc, "net": net, "what": what,
          "stitched": st} for i, (op, acc, net, what, st) in enumerate(rows)]
    HR._write_text_ledger(L, path)
    return path


def _merge_dir(tmp_path, base):
    dst = tmp_path / "merge"
    dst.mkdir()
    HR._copy_board(base, str(dst / "m0.kicad_pcb"))
    return str(dst)


def _widths(board, net):
    b = pcbnew.LoadBoard(board)
    return {round(pcbnew.ToMM(t.GetWidth()), 3) for t in b.GetTracks() if t.GetNetname() == net}


def test_accepted_lays_parse_ledger_rows_stitch_and_args(tmp_path):
    led = _ledger(str(tmp_path / "ledger.txt"), [
        ("lay", True, "ETH_P0", "(10 10) (20 10)", False),
        ("shove", True, "ETH_P0", "ids", False),
        ("lay", False, "USB_DP", "(0 0) (5 0)", False),
        ("lay", True, "SPI_CLK", "(1 1) (9 9)", True),
    ])
    assert [r[:3] for r in HR.accepted_lays(led)] == [("ETH_P0", "(10 10) (20 10)", False), ("SPI_CLK", "(1 1) (9 9)", True)]
    with open(led, "a") as fh:
        fh.write('    args: {"lock": false, "via": null, "width": 0.4, "any_angle": false}\n')
    assert HR.accepted_lays(led)[-1][3]["width"] == 0.4            # an args line belongs to the row before it
    assert HR.accepted_lays(led)[0][3] == {}


def test_two_sessions_both_merge_and_the_opens_fall_by_the_sum(tmp_path):
    base = _two_nets(str(tmp_path / "base"))
    s1 = HR.session_begin(base, str(tmp_path / "s1.kicad_pcb"))
    s2 = HR.session_begin(base, str(tmp_path / "s2.kicad_pcb"))
    assert s1["unconnected"] == 2
    assert HR.lay(s1["work"], "B", B_PATH, width=0.25)["accepted"]
    assert HR.lay(s2["work"], "C", C_PATH, width=0.25)["accepted"]
    r = HR.merge_sessions([_ledger_of(s1), _ledger_of(s2)], _merge_dir(tmp_path, base))
    assert r["applied"] == ["B", "C"] and r["requeue"] == []
    assert HR.open_nets(r["board"])["connections"] == []


def test_second_session_lay_crossing_the_first_is_refused_and_requeued(tmp_path):
    base = _two_nets(str(tmp_path / "base"), extra=True)
    s1 = HR.session_begin(base, str(tmp_path / "s1.kicad_pcb"))
    s2 = HR.session_begin(base, str(tmp_path / "s2.kicad_pcb"))
    assert HR.lay(s1["work"], "B", B_PATH, width=0.25)["accepted"]
    assert HR.lay(s2["work"], "X", X_PATH, width=0.25)["accepted"]     # clear on the base, crossing B after the merge
    assert HR.lay(s2["work"], "C", C_PATH, width=0.25)["accepted"]
    r = HR.merge_sessions([_ledger_of(s1), _ledger_of(s2)], _merge_dir(tmp_path, base))
    assert r["applied"] == ["B", "C"]
    assert [q["net"] for q in r["requeue"]] == ["X"]


def test_lay_width_is_replayed_from_the_ledger(tmp_path):
    base = _two_nets(str(tmp_path / "base"))
    s1 = HR.session_begin(base, str(tmp_path / "s1.kicad_pcb"))
    assert HR.lay(s1["work"], "B", B_PATH, width=0.4)["accepted"]
    led = _ledger_of(s1)
    assert HR.accepted_lays(led)[0][3]["width"] == 0.4
    r = HR.replay(led, _merge_dir(tmp_path, base))
    assert r["applied"] == ["B"] and _widths(r["board"], "B") == {0.4}


def test_session_commit_leaves_the_start_board_bytes_unchanged(tmp_path):
    base = _two_nets(str(tmp_path / "base"))
    s1 = HR.session_begin(base, str(tmp_path / "s1.kicad_pcb"))
    assert HR.lay(s1["work"], "B", B_PATH, width=0.25)["accepted"]
    start = os.path.join(s1["session"], "start.kicad_pcb")
    before = hashlib.sha1(open(start, "rb").read()).hexdigest()
    HR.session_commit(s1["work"], str(tmp_path / "out.kicad_pcb"))
    assert hashlib.sha1(open(start, "rb").read()).hexdigest() == before


def test_r6_worse_ledger_rolls_back_whole(tmp_path, monkeypatch):
    """the ledger's copper adds an rf gap on the candidate: the close guard refuses (R6) and every row is requeued."""
    real = GP._remeasure

    def measure(path, kicad_cli="kicad-cli"):
        m = real(path, kicad_cli)
        m["gaps"] = list(m.get("gaps") or [])
        if os.path.basename(path) == "cand.kicad_pcb":
            m["gaps"] = m["gaps"] + [{"id": "rf1", "kind": "open-gap", "class": "rf", "nets": ["B"], "at": None}]
        return m
    monkeypatch.setattr(GP, "_remeasure", measure)
    monkeypatch.setattr(GP, "_classify_into", lambda *a, **k: None)
    base = _two_nets(str(tmp_path / "base"))
    s1 = HR.session_begin(base, str(tmp_path / "s1.kicad_pcb"))
    assert HR.lay(s1["work"], "B", B_PATH, width=0.25)["accepted"]
    assert HR.lay(s1["work"], "C", C_PATH, width=0.25)["accepted"]
    dst = _merge_dir(tmp_path, base)
    r = HR.replay(_ledger_of(s1), dst)
    assert r["applied"] == [] and [q["net"] for q in r["requeue"]] == ["B", "C"]
    assert all("R6 rf worse" in q["why"] for q in r["requeue"])
    assert r["board"] == os.path.join(dst, "m0.kicad_pcb")


def test_cluster_regions_disjoint_nets_and_boxes():
    gaps = [{"id": "a", "nets": ["ETH_P0"], "at": [10, 10]}, {"id": "b", "nets": ["ETH_P0"], "at": [80, 80]},
            {"id": "c", "nets": ["USB_DP"], "at": [11, 11]}, {"id": "d", "nets": ["SPI"], "at": [50, 5]}]
    regs = GP.cluster_regions(gaps, pad_mm=2.0)
    assert sorted(sorted(r["gaps"]) for r in regs) == [["a", "b", "c"], ["d"]]
    seen = [n for r in regs for n in r["nets"]]
    assert len(seen) == len(set(seen))                           # nets disjoint across regions
