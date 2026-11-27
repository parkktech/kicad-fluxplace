"""w73-renet: a routed board with re-netted pads: --from route strips only the copper that is now wrong."""
import argparse
import os
import sys

import pytest

pytest.importorskip("pcbnew")
from fluxplace import boardwrite as BW, pipeline as PL, renet as RN       # noqa: E402
from tests import planboards as G                                         # noqa: E402

U = iter(range(10_000_000))


def seg(a, b, net, layer="F.Cu"):
    uid = "%08x-0000-4000-8000-%012x" % (0xB0000000 + next(U), next(U))
    return (f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) (layer "{layer}") (net "{net}") (uuid "{uid}"))\n')


def two(ref, x, y, n1, n2):
    return G.fp(ref, x, y, [G.pad("1", -0.5, 0, n1), G.pad("2", 0.5, 0, n2)])


def routed(tmp_path, name, nets):
    """R1(1,2) R2(1,2) R3(1,2) in a row, three nets routed by one segment each; `nets` = pad nets of R2 (re-net target)."""
    a, b = nets
    body = (two("R1", 10, 10, "A", "B") + two("R2", 30, 10, a, b) + two("R3", 50, 10, "A", "P")
            + seg((10.5, 10), (29.5, 10), "B")                   # R1.2 - R2.1 (B)
            + seg((30.5, 10), (30.5, 12), "P") + seg((30.5, 12), (50.5, 12), "P") + seg((50.5, 12), (50.5, 10), "P")      # R2.2 - R3.2 (a pair leg)
            + seg((9.5, 10), (9.5, 20), "A") + seg((9.5, 20), (49.5, 20), "A") + seg((49.5, 20), (49.5, 10), "A"))   # R1.1 - R3.1
    p = G.write(tmp_path, body, name=name)
    (tmp_path / (name[:-10] + ".kicad_pro")).write_text("{}")
    return p


def _args(tmp_path, board, **kw):
    d = dict(board=board, workdir=str(tmp_path / "W"), constraints=None, spec=None, from_stage="route", to_stage="route",
             status=False, waive=[], force=True, api=False, route_budget=60, route_timeout=30, stall_rounds=1,
             replace_on_stall=False, layers=["F.Cu", "B.Cu"], track=0.2, clearance=0.2, router_py=sys.executable,
             router_dir=str(tmp_path / "krt"))
    d.update(kw)
    return argparse.Namespace(**d)


def test_apply_strips_only_the_wrong_copper(tmp_path):
    # re-net: R2.1 B -> Q (plain net), R2.2 P -> A (a leg joins another net); copper keeps the old nets
    b2 = routed(tmp_path, "c.kicad_pcb", ("Q", "A"))
    out = str(tmp_path / "o" / "c.kicad_pcb")
    rep = RN.apply(b2, out)
    assert rep["nets"] == ["B", "P"] and rep["removed"] == {"B": 1, "P": 3} and rep["pads"] == ["R2.1", "R2.2"]
    before, after = BW.net_items(b2), BW.net_items(out)
    assert "B" not in after and "P" not in after
    assert after["A"] == before["A"]                              # the correct net is byte-for-byte the same copper


def test_no_mismatch_changes_nothing(tmp_path):
    b = routed(tmp_path, "b.kicad_pcb", ("B", "P"))
    assert RN.apply(b, str(tmp_path / "o" / "b.kicad_pcb")) is None and not (tmp_path / "o").exists()
    a = _args(tmp_path, b, from_stage="route-ready", to_stage="route-ready")
    assert PL._renet_entry(PL.Ctx(a, a.workdir, b, {}), a.workdir, b, {}) == b


def test_dangling_strand_goes_with_the_stale_copper(tmp_path):
    body = (two("R1", 10, 10, "A", "B") + two("R2", 30, 10, "Q", "B")
            + seg((10.5, 10), (20, 10), "B") + seg((20, 10), (29.5, 10), "B"))      # R1.2 -> R2.1; R2.1 is now Q
    b = G.write(tmp_path, body, name="d.kicad_pcb")
    rep = RN.apply(b, str(tmp_path / "o" / "d.kicad_pcb"))
    assert rep["items"] == 2 and rep["removed"] == {"B": 2}      # the touching segment and the strand it leaves


def test_pipeline_entry_strips_and_protects_the_rest(tmp_path):
    b = routed(tmp_path, "c.kicad_pcb", ("Q", "A"))
    c = PL.Ctx(_args(tmp_path, b), str(tmp_path / "W"), b, {})
    st = {}
    out = PL._renet_entry(c, str(tmp_path / "W"), b, st)
    assert out != b and os.path.isfile(out) and st["renet"]["nets"] == ["B", "P"]
    man = BW.load_manifest(out)
    assert not BW.is_stale(out, man) and "B" not in man["nets"] and "P" not in man["nets"]


def _run_to_stage(tmp_path, monkeypatch, b):
    seen = {}

    def fake(c, n, board):
        seen["board"] = board
        return {"verdict": "pass", "line": "fake", "board": board}
    monkeypatch.setitem(PL.FUNCS, "critroute", fake)
    a = _args(tmp_path, b, from_stage="critroute", to_stage="critroute")
    return PL.run(a), seen, PL.load_state(a.workdir)


def test_from_route_stage_gets_the_stripped_board_and_records_the_report(tmp_path, monkeypatch):
    b = routed(tmp_path, "c.kicad_pcb", ("Q", "A"))
    rc, seen, st = _run_to_stage(tmp_path, monkeypatch, b)
    assert rc == 0 and "0-renet" in seen["board"]
    assert st["stages"]["critroute"]["renet"]["nets"] == ["B", "P"]
    assert RN.apply(seen["board"], str(tmp_path / "again.kicad_pcb")) is None          # no stale copper left, nothing more to strip
    assert BW.net_items(seen["board"])["A"] == BW.net_items(b)["A"]


def test_from_route_without_mismatch_hands_on_the_input_untouched(tmp_path, monkeypatch):
    b = routed(tmp_path, "b.kicad_pcb", ("B", "P"))
    sha = BW.file_sha(b)
    rc, seen, st = _run_to_stage(tmp_path, monkeypatch, b)
    assert rc == 0 and seen["board"] == b and BW.file_sha(b) == sha and "renet" not in st["stages"]["critroute"]
    assert not (tmp_path / "W" / "0-renet").exists()


def test_partner_leg_is_released_with_a_stripped_pair_leg(tmp_path):
    body = (two("R1", 10, 10, "A", "D_P") + two("R2", 30, 10, "D_N", "Q")
            + seg((10.5, 10), (29.5, 10), "D_P")                          # R1.2 -> R2.1 which is now D_N
            + seg((10.5, 12), (29.5, 12), "D_N"))                         # the partner leg: clear of every re-netted pad
    b = G.write(tmp_path, body, name="p.kicad_pcb")
    (tmp_path / "p.kicad_pro").write_text("{}")
    out = str(tmp_path / "o" / "p.kicad_pcb")
    rep = RN.apply(b, out, {"pairs": {"D_": {}}})
    assert rep["nets"] == ["D_P"] and rep["release"] == ["D_N", "D_P"]
    assert BW.net_items(out)["D_N"] == BW.net_items(b)["D_N"]      # released, not stripped


def test_t_junction_and_via_inside_a_segment_are_seen(tmp_path):
    # stale copper reaches R2.1 (now Q) through a branch that ends in the INTERIOR of a trunk segment of the same net
    body = (two("R1", 10, 10, "A", "B") + two("R2", 30, 10, "Q", "B") + two("R3", 20, 30, "B", "A") + two("R4", 13, 14, "B", "A")
            + seg((10.5, 10), (10.5, 20), "B") + seg((10.5, 20), (19.5, 20), "B") + seg((19.5, 20), (19.5, 30), "B")    # trunk R1.2 -> R3.1
            + seg((15.5, 20), (15.5, 10), "B") + seg((15.5, 10), (29.5, 10), "B")         # branch: T at (15.5, 20), -> R2.1
            + seg((12.5, 20), (12.5, 14), "B"))                                     # a correct spur: its trunk end lands mid-segment
    b = G.write(tmp_path, body, name="t.kicad_pcb")
    (tmp_path / "t.kicad_pro").write_text("{}")
    out = str(tmp_path / "o" / "t.kicad_pcb")
    rep = RN.apply(b, out)
    assert rep["removed"] == {"B": 2}                                      # the touching segment and the branch stub; trunk and spur stay
    assert len(BW.net_items(out)["B"]) == 4


def test_renet_pre_answers_rip_existing_never_unless_the_owner_answered(tmp_path):
    b = routed(tmp_path, "c.kicad_pcb", ("Q", "A"))
    c = PL.Ctx(_args(tmp_path, b), str(tmp_path / "W"), b, {"stages": {}})
    PL._renet_entry(c, str(tmp_path / "W"), b, {})
    assert c.answers["rip-existing"] == "never"
    c2 = PL.Ctx(_args(tmp_path, b, answer=["rip-existing=low-speed"]), str(tmp_path / "W2"), b, {"stages": {}})
    PL._renet_entry(c2, str(tmp_path / "W2"), b, {})
    assert c2.answers["rip-existing"] == "low-speed"
    ok = routed(tmp_path, "b.kicad_pcb", ("B", "P"))
    c3 = PL.Ctx(_args(tmp_path, ok), str(tmp_path / "W3"), ok, {"stages": {}})
    PL._renet_entry(c3, str(tmp_path / "W3"), ok, {})
    assert "rip-existing" not in c3.answers                      # no mismatch: nothing pre-answered


def test_copper_running_across_a_new_pad_is_stripped(tmp_path):
    """in-place footprint swap: a shipped track crosses a NEW pad of another net without ending in it; a via annulus reaches another."""
    body = (two("R1", 10, 10, "A", "G") + two("R2", 40, 10, "G", "A") + two("R3", 25, 20, "N", "M")      # R3 is the swapped part: pads N, M
            + seg((10.5, 10), (39.5, 10), "G")                                                          # G, clear of R3
            + seg((24.5, 18), (24.5, 22), "G")                                                          # G runs across R3.1 (N): crosses the body, no end in it
            + seg((9.5, 10), (9.5, 14), "A") + seg((40.5, 10), (40.5, 14), "A"))
    b = G.write(tmp_path, body, name="x.kicad_pcb")
    (tmp_path / "x.kicad_pro").write_text("{}")
    rep = RN.apply(b, str(tmp_path / "o" / "x.kicad_pcb"))
    assert rep["pads"] == ["R3.1"] and rep["removed"] == {"G": 1}
    assert sorted(rep["release"]) == ["G", "N"]                      # the pad's new net is released too


def test_incremental_seat_scopes_the_route_to_the_new_parts_nets(tmp_path, monkeypatch):
    b = routed(tmp_path, "b.kicad_pcb", ("B", "P"))
    seen = {}

    def fake(c, n, board):
        seen["scope"] = dict(c.state.get("route_scope") or {})
        return {"verdict": "pass", "line": "fake", "board": board, "incremental": True, "placed": {"R2": [30, 10, 0]}}
    monkeypatch.setitem(PL.FUNCS, "floorplan", fake)
    a = _args(tmp_path, b, from_stage="floorplan", to_stage="floorplan", flow="constructive")
    assert PL.run(a) == 0
    st = PL.load_state(a.workdir)
    assert st["route_scope"]["nets"] == ["B", "P"]                      # R2's pad nets
    a2 = _args(tmp_path, str(tmp_path / "other.kicad_pcb"), from_stage="floorplan", to_stage="floorplan", flow="constructive", workdir=str(tmp_path / "W"))
    import shutil
    shutil.copy(b, a2.board)
    shutil.copy(str(tmp_path / "b.kicad_pro"), str(tmp_path / "other.kicad_pro"))
    PL.run(a2)
    assert PL.load_state(a2.workdir)["route_scope"]["input"] == os.path.abspath(a2.board)      # another BOARD: the old scope is dropped, a new one made


def _via(x, y, net):
    return f'(via (at {x} {y}) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def test_foreign_via_inside_the_clearance_of_a_swapped_pad_is_stripped(tmp_path):
    """in-place swap: a GND stitching via sits 0.10 mm (< clearance) from the new pad R3.1 without touching it; the same gap at an unchanged part stays."""
    body = (two("R1", 10, 10, "A", "G") + two("R3", 25, 20, "N", "M") + _via(24.5, 19.45, "G") + _via(10, 9.45, "G") + seg((9.5, 10), (9.5, 14), "A"))
    b = G.write(tmp_path, body, name="v.kicad_pcb")
    (tmp_path / "v.kicad_pro").write_text("{}")
    out = str(tmp_path / "o" / "v.kicad_pcb")
    assert RN.apply(b, out) is None                                          # a plain re-net run ignores clearance
    rep = RN.apply(b, out, None, {"R3"})
    assert rep["pads"] == ["R3.1"] and rep["removed"] == {"G": 1} and rep["release"] == ["G"]
    text = open(out).read()
    assert "(at 24.5 19.45)" not in text and "(at 10 9.45)" in text           # only the swapped part's neighbour went
    assert RN.changed_refs(b, out) == set()                                  # same pads: refs compare by pads, not copper
