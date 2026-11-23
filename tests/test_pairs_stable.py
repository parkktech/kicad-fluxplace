"""A7 (REFACTOR-PLAN-W16 3b): the pair lay is a pure function of its inputs. Four property tests on a fixture where three pairs
compete for one-pair gaps (negotiation active): same input twice, shuffled discovery order, workers=1 vs 8, planned => lays."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
import planboards as PB
from fluxplace import pairs as PR
from fluxplace import routediag as RD
from test_pairs import duo

BASE = {"preroute": {"signal_layers": ["F.Cu"]}, "pairlay": {"workers": 2}}


def _wall():
    return (PB.keepout(30, 0, 30.5, 34.4) + PB.keepout(30, 35.6, 30.5, 39.4) + PB.keepout(30, 40.6, 30.5, 44.4)
            + PB.keepout(30, 45.6, 30.5, 80))


def _parts(order=None):
    parts = [duo(f"U{k}", 20, y, f"A{k}_P", f"A{k}_N") + duo(f"J{k}", 40, y, f"A{k}_P", f"A{k}_N")
             for k, y in enumerate((38.0, 40.0, 42.0))]
    if order:
        parts = [parts[i] for i in order]
    return "".join(parts)


def _canon(path):
    """order-free copper fingerprint of the laid pair nets (net, layer, ends, width; vias): no UUIDs, no file order."""
    B = RD.load(path)
    segs = sorted((s["net"], s["layer"], tuple(round(v, 3) for v in sorted([tuple(s["a"]), tuple(s["b"])])[0]),
                   tuple(round(v, 3) for v in sorted([tuple(s["a"]), tuple(s["b"])])[1]), round(s["w"], 3)) for s in B["segs"])
    vias = sorted((v["net"], round(v["x"], 3), round(v["y"], 3), round(v["d"], 3)) for v in B["vias"])
    return segs, vias


def _lay(tmp, name, body, cons=None):
    b = PB.write(tmp, _wall() + body, name=name + ".in.kicad_pcb")
    out = str(tmp / (name + ".kicad_pcb"))
    rep = PR.run(b, cons or BASE, None, out)
    return rep, _canon(out)


def test_same_input_twice_gives_the_same_copper(tmp_path):
    r1, c1 = _lay(tmp_path, "a", _parts())
    r2, c2 = _lay(tmp_path, "b", _parts())
    assert r1["complete"] == 3 and c1[0]
    assert c1 == c2


def test_shuffled_pair_discovery_order_gives_the_same_copper(tmp_path):
    _, c0 = _lay(tmp_path, "a", _parts())
    for k, order in enumerate(([2, 1, 0], [1, 2, 0], random.Random(7).sample(range(3), 3))):
        _, c = _lay(tmp_path, f"s{k}", _parts(order))
        assert c == c0, f"order {order}"


def test_worker_count_does_not_change_the_result(tmp_path):
    _, c1 = _lay(tmp_path, "w1", _parts(), dict(BASE, pairlay={"workers": 1}))
    _, c8 = _lay(tmp_path, "w8", _parts(), dict(BASE, pairlay={"workers": 8}))
    assert c1 == c8


def test_planned_implies_lays(tmp_path):
    b = PB.write(tmp_path, _wall() + _parts(), name="p.in.kicad_pcb")
    plan = PR.run(b, BASE, None, None, plan_only=True)
    planned = {tuple(p["pair"]) for p in plan["pairs"] if p["status"] == "planned"}
    assert planned
    lay = PR.run(b, BASE, None, str(tmp_path / "p.kicad_pcb"))
    laid = {tuple(p["pair"]) for p in lay["pairs"] if p["status"] == "complete"}
    assert planned <= laid, f"planned but not laid: {sorted(planned - laid)}"
