"""B1: one net contract (netclass.contract) read by the five former tables; the commit point refuses new copper off its contract
(CONTRACT_CHANGED), also on unprotected RF nets and rails."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
import planboards as PB
from fluxplace import boardwrite as BW, netclass as NC, pipeline as PL, routediag as RD
from test_boardwrite import seg

HEAD4 = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))\n'
CONS = {"rf": {"nets": ["RF_ANT"], "width_mm": 0.3}, "power": {"VRAIL": {"max_current_ma": 2000}}}


def _fp(ref, x, net_a, net_b):
    return PB.fp(ref, x, 20, [PB.pad("1", 0, 0, net_a), PB.pad("2", 0, 2, net_b)])


def mk(tmp_path, name, items):
    body = _fp("U1", 20, "RF_ANT", "VRAIL") + _fp("U2", 40, "RF_ANT", "VRAIL") + items
    p = tmp_path / name
    p.write_text(HEAD4 + PB.EDGE + body + ")\n")
    return str(p)


def seg_w(net, a, b, w, layer="F.Cu"):
    return seg(net, a, b, layer=layer).replace("(width 0.2)", f"(width {w})")


def test_rf_fallback_inner_layer_copper_is_refused_and_the_net_stays_open(tmp_path):
    """The outer path is blocked: a fallback that closes the RF net on an inner layer is refused; the net stays open."""
    prev = mk(tmp_path, "prev.kicad_pcb", "")
    new = mk(tmp_path, "new.kicad_pcb", seg_w("RF_ANT", (20, 20), (40, 20), 0.3, "In1.Cu"))
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new, "line": "routed"}, CONS)
    assert r["verdict"] == "pass" and "stripped 1 off-contract item(s) on RF_ANT" in r["line"]      # w84: the item goes, the stage is kept
    assert not [x for x in RD.load(new)["segs"] if x["net"] == "RF_ANT"] and BW.contract_check(prev, new, CONS)["ok"]
    ok = mk(tmp_path, "ok.kicad_pcb", seg_w("RF_ANT", (20, 20), (40, 20), 0.3))
    assert PL._commit_point("route", prev, {"verdict": "pass", "board": ok}, CONS)["verdict"] == "pass"


def test_production_guard_refuses_a_hand_injected_015_rf_segment(tmp_path):
    prev = mk(tmp_path, "prev.kicad_pcb", "")
    new = mk(tmp_path, "new.kicad_pcb", seg_w("RF_ANT", (20, 20), (40, 20), 0.15))
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new}, CONS)
    assert r["verdict"] == "pass" and "stripped 1 off-contract item(s) on RF_ANT" in r["line"]
    assert not [x for x in RD.load(new)["segs"] if x["net"] == "RF_ANT"]
    short = mk(tmp_path, "short.kicad_pcb", seg_w("RF_ANT", (20, 20), (21, 20), 0.15))                 # a pad neck is exempt
    assert PL._commit_point("route", prev, {"verdict": "pass", "board": short}, CONS)["verdict"] == "pass"


def test_power_default_width_a_rail_with_a_current_figure_cannot_gain_015_copper(tmp_path):
    prev = mk(tmp_path, "prev.kicad_pcb", "")
    new = mk(tmp_path, "new.kicad_pcb", seg_w("VRAIL", (20, 22), (40, 22), 0.15))
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new}, CONS)
    assert r["verdict"] == "pass" and "stripped 1 off-contract item(s) on VRAIL" in r["line"]
    wide = mk(tmp_path, "wide.kicad_pcb", seg_w("VRAIL", (20, 22), (40, 22), 0.8))
    assert PL._commit_point("route", prev, {"verdict": "pass", "board": wide}, CONS)["verdict"] == "pass"
    nofig = {"rf": CONS["rf"], "power": {"VRAIL": {"nominal_v": 3.3}}}                                  # no current figure: no width contract
    assert PL._commit_point("route", prev, {"verdict": "pass", "board": new}, nofig)["verdict"] == "pass"


def test_old_copper_is_not_judged_only_added_copper(tmp_path):
    prev = mk(tmp_path, "prev.kicad_pcb", seg_w("RF_ANT", (20, 20), (40, 20), 0.15))
    new = mk(tmp_path, "new.kicad_pcb", seg_w("RF_ANT", (20, 20), (40, 20), 0.15))
    assert BW.contract_check(prev, new, CONS)["ok"]


def test_the_tables_agree_on_every_net_of_the_corpus_boards(tmp_path):
    """Property: the contract and the five former tables (protected, controlled, lock level, routing rank, owner) are consistent per net."""
    from fluxplace import batchroute as BR, drcguard as DG, lockpolicy as LP, ripolicy as RP
    boards = [mk(tmp_path, "a.kicad_pcb", "")]
    W16 = "/var/kicad/utv-comms-bridge/W16"
    default = os.pathsep.join(os.path.join(W16, x) for x in ("stageA/models-1/out.kicad_pcb", "view/finish2-route.kicad_pcb"))
    for extra in (os.environ.get("FLUXPLACE_CORPUS_BOARDS") or default).split(os.pathsep):
        if extra and os.path.isfile(extra):
            boards.append(extra)
    for path in boards:
        B = RD.load(path)
        cls = NC.classify(B, CONS)
        con = NC.contract(B, CONS, cls=cls)
        names = sorted(con)
        prot = DG.protected_nets(CONS, names)
        ctl = RP.controlled_nets(cls, CONS, names)
        for n, e in con.items():
            assert e["protected"] == (n in prot)
            assert e["controlled"] == ctl.get(n)
            assert e["lock"] == LP.level(n, CONS, names)
            assert e["rank"] == BR.CONTROLLED.get(e["class"])
            if e["protected"] and e["class"] == "rf":
                assert e["lock"] == "hard" and e["owner"] == "rf" and e["layers"]
            if e["class"] == "pair":
                assert e["owner"] == "pairs"
            if n in cls:
                assert cls[n]["owner"] in set(NC.OWNER.values()) | {"router"}


def test_a_rails_step_that_lays_inner_copper_on_an_outer_only_rail_is_stripped_at_the_step_not_at_the_commit(tmp_path):
    """w84 (route-7: VBATT_PROT on In2.Cu, VMODEM on In3.Cu): strip_contract removes only the off-contract NEW items."""
    cons = {"route": {"power_spine": "ipc-outer"}, "power": {"VRAIL": {"max_current_ma": 2000, "pour": True}}}
    keep = seg_w("VRAIL", (20, 22), (40, 22), 0.8, "F.Cu")
    prev = mk(tmp_path, "prev.kicad_pcb", keep)
    new = mk(tmp_path, "new.kicad_pcb", keep + seg_w("VRAIL", (20, 24), (40, 24), 0.8, "In2.Cu") + seg_w("VRAIL", (20, 26), (40, 26), 0.8, "B.Cu"))
    assert not BW.contract_check(prev, new, cons)["ok"]
    st = BW.strip_contract(prev, new, cons)
    assert st == {"removed": 1, "nets": {"VRAIL": 1}}
    layers = sorted(x["layer"] for x in RD.load(new)["segs"] if x["net"] == "VRAIL")
    assert layers == ["B.Cu", "F.Cu"] and BW.contract_check(prev, new, cons)["ok"]
    assert BW.strip_contract(prev, new, cons)["removed"] == 0


def test_rails_ladder_layers_follow_the_contract_per_net():
    from fluxplace import krtchain as KC
    groups = [({"A": 0.6, "B": 0.6}, ["F.Cu", "In2.Cu", "B.Cu"])]
    out = KC._contract_split(groups, {"A": ["F.Cu", "B.Cu"]})
    assert sorted((sorted(g), l) for g, l in out) == [(["A"], ["F.Cu", "B.Cu"]), (["B"], ["F.Cu", "In2.Cu", "B.Cu"])]
    assert KC._contract_split([({"A": 0.6}, ["In2.Cu"])], {"A": ["F.Cu", "B.Cu"]}) == []        # no allowed layer in the call: the rail stays open


def test_the_route_stage_releases_the_pairs_its_pairgroups_step_re_laid(tmp_path):
    """w84: route-7 was refused PROTECTED_CHANGED USB2_M2_DM/DP after its pairgroups step re-laid the pair (its old locked copper is
    replaced by the new lay): the pairs the step reports in `released` are released at the commit point."""
    prev = mk(tmp_path, "prev.kicad_pcb", seg("RF_ANT", (20, 20), (40, 20), True) + seg("VRAIL", (20, 22), (40, 22), True))
    BW.seed(prev, why_by_net={"RF_ANT": "pair RF_ANT/VRAIL", "VRAIL": "pair RF_ANT/VRAIL"})
    new = mk(tmp_path, "new.kicad_pcb", seg("RF_ANT", (20, 20), (30, 21), True) + seg("RF_ANT", (30, 21), (40, 20), True) + seg("VRAIL", (20, 22), (40, 22), True))
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new, "line": "routed"}, None)
    assert r["verdict"] == "fail" and "PROTECTED_CHANGED" in r["line"]
    r = PL._commit_point("route", prev, {"verdict": "pass", "board": new, "line": "routed", "released": ["RF_ANT", "VRAIL"]}, None)
    assert r["verdict"] == "pass"
