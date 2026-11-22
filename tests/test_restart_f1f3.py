"""RESTART-F1-F3: dedupe (exact duplicate copper, span removal), pinstrip (gated legs + neighbour fan-out),
impedance counts in pairs-only 'pair ok'. Synthetic text boards; no pcbnew."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_pairsonly as TP
from fluxplace import boardtext as BT
from fluxplace import dedupe as DD
from fluxplace import gaps as GP
from fluxplace import pairsonly as PO
from fluxplace import pinstrip as PS
from fluxplace import routediag as RD


def lk_seg(a, b, net, locked, layer="F.Cu", w=0.15):
    lk = "(locked yes) " if locked else ""
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) {lk}(layer "{layer}") (net "{net}"))\n'


def lk_via(x, y, net, locked):
    lk = "(locked yes) " if locked else ""
    return f'(via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {lk}(net "{net}"))\n'


def test_dedupe_keeps_the_locked_copy_and_removes_each_extra_by_span():
    a = lk_seg((1, 1), (2, 1), "N1", locked=False)
    a_lock = lk_seg((1, 1), (2, 1), "N1", locked=True)
    v = lk_via(3, 3, "N1", locked=False)
    uniq = lk_seg((5, 5), (6, 5), "N2", locked=False)
    text = "(kicad_pcb\n" + a + a_lock + a + v + v + uniq + ")\n"
    out, n = DD.dedupe_text(text)
    assert n == 3
    blocks = [b for _i, _j, _k, b in BT.scan(out)]
    segs = [b for b in blocks if b.startswith("(segment")]
    vias = [b for b in blocks if b.startswith("(via")]
    assert len(vias) == 1 and len(segs) == 2
    assert sum("(start 1 1)" in b for b in segs) == 1
    kept = next(b for b in segs if "(start 1 1)" in b)
    assert BT.is_locked(kept)
    assert any("(start 5 5)" in b for b in segs)
    # idempotent
    out2, n2 = DD.dedupe_text(out)
    assert n2 == 0 and out2 == out


def test_dedupe_unlocked_first_locked_later_keeps_locked_survivor():
    a = lk_seg((1, 1), (2, 1), "N1", locked=False)
    a_lock = lk_seg((1, 1), (2, 1), "N1", locked=True)
    out, n = DD.dedupe_text("(kicad_pcb\n" + a + a_lock + ")\n")
    assert n == 1 and BT.is_locked(out)


def test_strip_escape_removes_gated_legs_and_neighbour_fanout_only(monkeypatch):
    pads = [
        {"ref": "J1", "num": "1", "x": 0.0, "y": 0.0, "net": "USB_DP"},
        {"ref": "J1", "num": "2", "x": 0.4, "y": 0.0, "net": "USB_DN"},
        {"ref": "J1", "num": "3", "x": 0.8, "y": 0.0, "net": "GND"},
        {"ref": "J1", "num": "9", "x": 5.0, "y": 0.0, "net": "GND"},
    ]
    monkeypatch.setattr(RD, "load", lambda path, **kw: {"pads": pads})
    monkeypatch.setattr(PS, "gated_legs", lambda board, cons, spec=None: ["USB_DN", "USB_DP"])
    text = ("(kicad_pcb\n"
            + lk_seg((0, 0), (0.4, 0), "USB_DP", locked=True)
            + lk_seg((0.8, 0), (0.8, 0.6), "GND", locked=True)
            + lk_via(0.8, 0.6, "GND", locked=True)
            + lk_seg((5, 0), (5, 0.5), "GND", locked=False)
            + ")\n")
    out, rep = PS.strip_escape(text, "x.kicad_pcb", {})
    assert rep["legs"] == ["USB_DN", "USB_DP"]
    assert rep["neighbours"] == ["J1.3 GND"]
    assert rep["removed"] == 3
    assert rep["by_net"] == {"USB_DP": 1, "GND": 2}
    left = [b for _i, _j, _k, b in BT.scan(out)]
    assert len(left) == 1 and "(start 5 0)" in left[0]


def test_impedance_off_nets_empty_on_exception(monkeypatch):
    def boom(path):
        raise RuntimeError("no board")
    import fluxplace.boardfile as BF
    monkeypatch.setattr(BF, "facts_from_file", boom)
    assert GP.impedance_off_nets("nope.kicad_pcb", {}) == set()


def test_pair_with_impedance_miss_is_not_already_ok(tmp_path, monkeypatch):
    import planboards as PB
    body = (TP.duo("U1", 20, 20, "A_P", "A_N") + TP.duo("J1", 20, 40, "A_P", "A_N")
            + TP.seg((20, 20), (20, 40), "A_P") + TP.seg((20.5, 20), (20.5, 40), "A_N"))
    src = PB.write(tmp_path, body)
    monkeypatch.setattr(GP, "impedance_off_nets", lambda board, cons: {"A_P"})
    rep = PO.reroute(src, str(tmp_path / "o.kicad_pcb"), ["A_P"], {}, None, drc=TP.no_drc(), log=lambda m: None)
    row = rep["pairs"][0]
    assert row["status"] != "already_ok"
    assert row["impedance_off"] == ["A_P"]
