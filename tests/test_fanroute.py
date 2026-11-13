"""Routing with locked fan-out: order by class, locked copper audited, fan-out detection."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import batchroute as BR, routeaudit as RA, routediag as RD, pipeline as PL


def board(tmp, locked=True, group=True):
    body = PB.fp("U1", 20, 20, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)]) + PB.fp("U2", 40, 20, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)])
    lk = "(locked yes) " if locked else ""
    body += f'(segment (start 20 20) (end 22 20) (width 0.15) {lk}(layer "F.Cu") (net "N1"))\n'
    body += f'(via (at 22 20) (size 0.45) (drill 0.2) {lk}(layers "F.Cu" "B.Cu") (net "N1"))\n'
    if group:
        body += '(group "fanout:U1" (uuid "x") (members))\n'
    return PB.write(tmp, body)


def test_locked_flag_loaded_and_fanout_detected(tmp_path):
    p = board(tmp_path)
    B = RD.load(p)
    assert B["segs"][0]["locked"] and B["vias"][0]["locked"]
    assert BR.has_fanout(p)
    assert not BR.has_fanout(board(tmp_path, group=False))


def test_audit_flags_ripped_locked_copper(tmp_path):
    base = RD.load(board(tmp_path))
    new = RD.load(board(tmp_path))
    new["segs"], new["vias"] = [], []
    codes = {v["code"] for v in RA.audit(base, new, {})}
    assert "FANOUT_RIPPED" in codes
    assert not RA.audit(base, RD.load(board(tmp_path)), {})


def test_order_controlled_first_with_fanout(monkeypatch):
    cls = {"A": {"class": "signal"}, "P": {"class": "pair"}, "R": {"class": "rf"}, "K": {"class": "clock"}, "S": {"class": "slow"}}
    B = {"parts": {}, "pads": []}
    monkeypatch.setattr(BR.NC, "net_pads", lambda B_: {})
    monkeypatch.setattr(BR.NC, "net_length", lambda B_, n, ps=None: {"A": 1.0, "P": 9.0, "R": 5.0, "K": 3.0, "S": 0.5}[n])
    order = BR.order_nets(B, ["A", "S", "K", "R", "P"], cls, fanout=True)
    assert order[:3] == ["P", "R", "K"] and order[3:] == ["S", "A"]


def test_constructive_flow_stage_names():
    assert PL.FLOWS["constructive"][0] == "ready"
    assert PL.FLOWS["constructive"][1:5] == ["modules", "floorplan", "fanout", "plan-route"]
    assert PL.FLOWS["constructive"][5] == "route"
    for n in PL.FLOWS["constructive"]:
        assert n in PL.FUNCS
