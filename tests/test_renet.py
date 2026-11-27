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
            + seg((30.5, 10), (50.5, 10), "P")                   # R2.2 - R3.2  (a pair leg)
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
    b = routed(tmp_path, "b.kicad_pcb", ("B", "P"))              # consistent: R2.1=B, R2.2=P
    assert RN.apply(b, str(tmp_path / "o" / "x.kicad_pcb")) is None
    # re-net: R2.1 B -> Q (plain net), R2.2 P -> A (a leg joins another net); copper keeps the old nets
    b2 = routed(tmp_path, "c.kicad_pcb", ("Q", "A"))
    out = str(tmp_path / "o" / "c.kicad_pcb")
    rep = RN.apply(b2, out)
    assert rep["nets"] == ["B", "P"] and rep["removed"] == {"B": 1, "P": 1} and rep["pads"] == ["R2.1", "R2.2"]
    before, after = BW.net_items(b2), BW.net_items(out)
    assert "B" not in after and "P" not in after
    assert after["A"] == before["A"]                              # the correct net is byte-for-byte the same copper


def test_no_mismatch_changes_nothing(tmp_path):
    b = routed(tmp_path, "b.kicad_pcb", ("B", "P"))
    assert RN.apply(b, str(tmp_path / "o" / "b.kicad_pcb")) is None and not (tmp_path / "o").exists()
    a = _args(tmp_path, b, from_stage="route-ready", to_stage="route-ready")
    PL._renet_entry  # noqa: B018 - the entry check exists
    assert PL._renet_entry(PL.Ctx(a, a.workdir, b, {}), a.workdir, b, {}) == b


def test_dangling_strand_goes_with_the_stale_copper(tmp_path):
    body = (two("R1", 10, 10, "A", "B") + two("R2", 30, 10, "Q", "B")
            + seg((10.5, 10), (20, 10), "B") + seg((20, 10), (29.5, 10), "B"))      # R1.2 -> R2.1; R2.1 is now Q
    b = G.write(tmp_path, body, name="d.kicad_pcb")
    out = str(tmp_path / "o" / "d.kicad_pcb")
    rep = RN.apply(b, out)
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
    assert not os.path.exists(os.path.join(str(tmp_path / "W"), "0-renet"))


def test_partner_leg_is_released_with_a_stripped_pair_leg(tmp_path):
    body = (two("R1", 10, 10, "A", "D_P") + two("R2", 30, 10, "D_N", "Q")
            + seg((10.5, 10), (29.5, 10), "D_P")                          # R1.2 -> R2.1 which is now D_N
            + seg((10.5, 12), (29.5, 12), "D_N"))                         # the partner leg: clear of every re-netted pad
    b = G.write(tmp_path, body, name="p.kicad_pcb")
    (tmp_path / "p.kicad_pro").write_text("{}")
    cons = {"pairs": {"D_": {}}}
    rep = RN.apply(b, str(tmp_path / "o" / "p.kicad_pcb"), cons)
    assert rep["nets"] == ["D_P"] and rep["release"] == ["D_N", "D_P"]
    assert BW.net_items(str(tmp_path / "o" / "p.kicad_pcb"))["D_N"] == BW.net_items(b)["D_N"]      # released, not stripped


def test_t_junction_and_via_inside_a_segment_are_seen(tmp_path):
    # stale copper reaches R2.1 (now Q) through a branch that ends in the INTERIOR of a trunk segment of the same net
    body = (two("R1", 10, 10, "A", "B") + two("R2", 30, 10, "Q", "B") + two("R3", 20, 30, "B", "A") + two("R4", 13, 14, "B", "A")
            + seg((10.5, 10), (10.5, 20), "B") + seg((10.5, 20), (19.5, 20), "B") + seg((19.5, 20), (19.5, 30), "B")    # trunk R1.2 -> R3.1
            + seg((15.5, 20), (15.5, 10), "B") + seg((15.5, 10), (29.5, 10), "B")         # branch: T at (15.5, 20), -> R2.1
            + seg((12.5, 20), (12.5, 14), "B"))                                     # a correct spur: its trunk end lands mid-segment
    b = G.write(tmp_path, body, name="t.kicad_pcb")
    (tmp_path / "t.kicad_pro").write_text("{}")
    rep = RN.apply(b, str(tmp_path / "o" / "t.kicad_pcb"))
    assert rep["removed"] == {"B": 2}                                      # the touching segment and the branch stub; trunk and spur stay
    assert len(BW.net_items(str(tmp_path / "o" / "t.kicad_pcb"))["B"]) == 4
