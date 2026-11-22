"""PHASE3I B2: replay of region session ledgers onto one merge board (fake lay; no KiCad, no router)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import handroute as HR, gaps as GP


def _ledger(path, rows):
    L = [{"n": i + 1, "t": "2026-11-22 10:00:0%d" % i, "op": op, "accepted": acc, "net": net, "what": what,
          "stitched": st} for i, (op, acc, net, what, st) in enumerate(rows)]
    HR._write_text_ledger(L, path)
    return path


def _fake_lay(calls, refuse=()):
    def lay(board, net, path, out=None, constraints=None, kicad_cli="kicad-cli", stitch=False, **kw):
        calls.append((os.path.basename(board), net, path, stitch))
        if net in refuse:
            raise ValueError("probe failed: clearance")
        open(out, "w").write(open(board).read() + f"\n;{net}\n")
        return {"accepted": True}
    return lay


def test_accepted_lays_parse_ledger_rows_and_stitch(tmp_path):
    led = _ledger(str(tmp_path / "ledger.txt"), [
        ("lay", True, "ETH_P0", "(10 10) (20 10)", False),
        ("shove", True, "ETH_P0", "ids", False),
        ("lay", False, "USB_DP", "(0 0) (5 0)", False),
        ("lay", True, "SPI_CLK", "(1 1) (9 9)", True),
    ])
    assert HR.accepted_lays(led) == [("ETH_P0", "(10 10) (20 10)", False), ("SPI_CLK", "(1 1) (9 9)", True)]


def test_replay_applies_in_order_and_requeues_refused(tmp_path, monkeypatch):
    dst = tmp_path / "merge"
    dst.mkdir()
    (dst / "m0.kicad_pcb").write_text("board0")
    led = _ledger(str(tmp_path / "a.txt"), [
        ("lay", True, "ETH_P0", "(10 10) (20 10)", False),
        ("lay", True, "CONFLICT", "(0 0) (3 3)", False),
        ("lay", True, "SPI_CLK", "(1 1) (9 9)", True),
    ])
    calls = []
    monkeypatch.setattr(HR, "lay", _fake_lay(calls, refuse={"CONFLICT"}))
    r = HR.replay(led, str(dst))
    assert r["applied"] == ["ETH_P0", "SPI_CLK"]
    assert [q["net"] for q in r["requeue"]] == ["CONFLICT"] and "clearance" in r["requeue"][0]["why"]
    assert r["board"] == str(dst / "m2.kicad_pcb")               # m1 = ETH_P0, refused lay wrote nothing, m2 = SPI_CLK
    assert calls[2] == ("m1.kicad_pcb", "SPI_CLK", "(1 1) (9 9)", True)   # stitched flag passed through
    assert (dst / "m2.kicad_pcb").read_text().endswith(";SPI_CLK\n")


def test_merge_two_region_sessions_final_board_has_both(tmp_path, monkeypatch):
    dst = tmp_path / "merge"
    dst.mkdir()
    (dst / "m0.kicad_pcb").write_text("board0")
    la = _ledger(str(tmp_path / "A.txt"), [("lay", True, "ETH_P0", "(10 10) (20 10)", False)])
    lb = _ledger(str(tmp_path / "B.txt"), [("lay", True, "ETH_P0", "(30 30) (40 30)", False),
                                             ("lay", True, "USB_DP", "(0 0) (5 0)", False)])
    calls = []
    monkeypatch.setattr(HR, "lay", _fake_lay(calls))
    r = HR.merge_sessions([la, lb], str(dst))
    assert r["applied"] == ["ETH_P0", "ETH_P0", "USB_DP"] and r["requeue"] == []
    assert r["board"] == str(dst / "m3.kicad_pcb")


def test_cluster_regions_disjoint_nets_and_boxes():
    gaps = [{"id": "a", "nets": ["ETH_P0"], "at": [10, 10]}, {"id": "b", "nets": ["ETH_P0"], "at": [80, 80]},
            {"id": "c", "nets": ["USB_DP"], "at": [11, 11]}, {"id": "d", "nets": ["SPI"], "at": [50, 5]}]
    regs = GP.cluster_regions(gaps, pad_mm=2.0)
    assert sorted(sorted(r["gaps"]) for r in regs) == [["a", "b", "c"], ["d"]]
    seen = [n for r in regs for n in r["nets"]]
    assert len(seen) == len(set(seen))                           # nets disjoint across regions
