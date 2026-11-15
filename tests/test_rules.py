import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import rules as RU, constraints as C, review as RV

FP = {"ref": "U1", "net": "USB_DP", "netclass": "usb", "type": "track", "layer": "F.Cu",
      "props": {"MPN": "LM1117-3.3", "Value": "10k"}}


def one(select, value, **kw):
    """A cons dict with a single [[rule]] that sets width_mm = value."""
    r = {"select": select, "set": {"width_mm": value}}
    r.update(kw)
    return {"rule": [r]}


def w(cons, obj=FP):
    return RU.load(cons).resolve(obj, "width_mm")


# ---- precedence: each level beats the next -------------------------------------------------

LEVELS = [
    ({"net": "^USB_DP$"}, "exact net"),
    ({"net": "^USB_.*"}, "net regex / ref glob tier"),
    ({"prop": {"MPN": "1117"}}, "prop"),
    ({"pair": "USB_"}, "pair"),
    ({"netclass": "usb"}, "netclass"),
    ({"layer": "F.Cu"}, "type/layer"),
    ({}, "global default"),
]


@pytest.mark.parametrize("hi,lo", list(zip(LEVELS, LEVELS[1:])), ids=lambda x: x[1] if isinstance(x, tuple) else "")
def test_each_level_beats_next(hi, lo):
    # the winner is written SECOND, so a level tie would also pick it: write it FIRST
    cons = {"rule": [
        {"select": hi[0], "set": {"width_mm": 0.2}},
        {"select": lo[0], "set": {"width_mm": 0.1}},
    ]}
    v, prov = w(cons)
    assert v == 0.2, f"{hi[1]} should beat {lo[1]}"


def test_exact_ref_and_exact_net_tie_later_wins():
    cons = {"rule": [{"select": {"ref": "U1"}, "set": {"width_mm": 0.1}},
                     {"select": {"net": "^USB_DP$"}, "set": {"width_mm": 0.2}}]}
    assert w(cons)[0] == 0.2
    cons2 = {"rule": [{"select": {"net": "^USB_DP$"}, "set": {"width_mm": 0.2}},
                      {"select": {"ref": "U1"}, "set": {"width_mm": 0.1}}]}
    assert w(cons2)[0] == 0.1


def test_longer_pair_family_wins_at_pair_level():
    cons = {"pairs": {"USB_": {"skew_mm": 3.0}, "USB_DP": {"skew_mm": 0.5}}}
    assert RU.load(cons).resolve({"net": "USB_DP"}, "skew_mm")[0] == 0.5


def test_tie_later_in_file_wins():
    cons = {"rule": [{"select": {"layer": "F.Cu"}, "set": {"width_mm": 0.1}},
                     {"select": {"layer": "F.Cu"}, "set": {"width_mm": 0.3}}]}
    assert w(cons)[0] == 0.3


def test_important_beats_more_specific_rule():
    cons = {"rule": [{"select": {"net": "^USB_DP$"}, "set": {"width_mm": 0.2}},
                     {"select": {}, "set": {"width_mm": 0.5}, "important": True}]}
    v, prov = w(cons)
    assert v == 0.5
    assert prov == RU.load(cons)[-1].label


def test_important_among_important_uses_normal_order():
    cons = {"rule": [{"select": {"net": "^USB_DP$"}, "set": {"width_mm": 0.2}, "important": True},
                     {"select": {"ref": "U*"}, "set": {"width_mm": 0.4}, "important": True}]}
    assert w(cons)[0] == 0.2


def test_resolve_is_per_key():
    # a rule that sets only width does not hide clearance from a less specific rule
    cons = {"rule": [{"select": {"net": "^USB_DP$"}, "set": {"width_mm": 0.2}},
                     {"select": {"netclass": "usb"}, "set": {"clearance_mm": 0.15}}]}
    rs = RU.load(cons)
    assert rs.resolve(FP, "clearance_mm") == (0.15, rs[-1].label)
    assert rs.resolve(FP, "width_mm")[0] == 0.2


def test_and_of_select_keys():
    cons = one({"net": "^USB_DP$", "layer": "B.Cu"}, 0.9)
    assert w(cons) == (None, None)
    cons = one({"net": "^USB_DP$", "layer": "F.Cu"}, 0.9)
    assert w(cons)[0] == 0.9


def test_prop_partial_match():
    assert w(one({"prop": {"MPN": "1117"}}, 0.6))[0] == 0.6
    assert w(one({"prop": {"MPN": "1117", "Value": "22k"}}, 0.6)) == (None, None)
    assert w(one({"prop": {"Missing": "x"}}, 0.6)) == (None, None)


def test_provenance_names_rule_or_location():
    cons = one({"net": "^USB_DP$"}, 0.2, name="usb dp")
    assert w(cons)[1] == '"usb dp"'
    assert w(one({"net": "^USB_DP$"}, 0.2))[1] == "[[rule]] #1"


# ---- validation -----------------------------------------------------------------------------

@pytest.mark.parametrize("cons,code", [
    ({"rule": [{"select": {"nett": "X"}, "set": {"width_mm": 1}}]}, "RULE_UNKNOWN_KEY"),
    ({"rule": [{"select": {"net": "X"}, "set": {"widht_mm": 1}}]}, "RULE_UNKNOWN_KEY"),
    ({"rule": [{"select": {"net": "("}, "set": {"width_mm": 1}}]}, "RULE_BAD_REGEX"),
    ({"rule": [{"select": {"type": "wire"}, "set": {"width_mm": 1}}]}, "RULE_BAD_VALUE"),
    ({"rule": [{"select": {"net": "X"}, "set": {"width_mm": "wide"}}]}, "RULE_BAD_VALUE"),
    ({"rule": [{"select": {"net": "X"}, "set": {"pour": "yes"}}]}, "RULE_BAD_VALUE"),
    ({"rule": [{"select": {"net": "X"}, "set": {"width_mm": 1}, "prio": 2}]}, "RULE_UNKNOWN_KEY"),
])
def test_validation_errors(cons, code):
    with pytest.raises(RU.RuleError) as e:
        RU.load(cons)
    assert e.value.code == code


def test_matches_nothing_warns():
    cons = {"rule": [{"name": "ghost", "select": {"net": "^NOPE$"}, "set": {"width_mm": 1}},
                     {"select": {"ref": "U1"}, "set": {"width_mm": 1}}]}
    warn = RU.check_matches(RU.load(cons), nets=["USB_DP"], refs=["U1"])
    assert [r.name for r, _ in warn] == ["ghost"]


def test_review_reports_rule_errors_and_warnings():
    facts = {"net_pads": {"USB_DP": [("U1", "1")]}, "parts": {"U1": {}}}
    bad = {"rule": [{"select": {"nett": "X"}, "set": {"width_mm": 1}}]}
    f = RV.check_rule_selectors(facts, bad)
    assert [(x["level"], x["code"]) for x in f] == [("FAIL", "RULE_UNKNOWN_KEY")]
    ghost = {"rule": [{"select": {"net": "^NOPE$"}, "set": {"width_mm": 1}}]}
    f = RV.check_rule_selectors(facts, ghost)
    assert [(x["level"], x["code"]) for x in f] == [("WARN", "RULE_MATCHES_NOTHING")]


# ---- legacy blocks translate to rules with identical values ---------------------------------

def test_legacy_nets_equals_rule():
    legacy = {"nets": {"PTT_THRU": {"straight_copper": ["J1A:1", "J2A:1"], "max_vias": 0}}}
    new = {"rule": [{"select": {"net": "^PTT_THRU$"},
                     "set": {"straight_copper": ["J1A:1", "J2A:1"], "max_vias": 0}}]}
    a, b = RU.load(legacy), RU.load(new)
    o = {"net": "PTT_THRU"}
    assert {k: v for k, (v, _p) in a.resolve_all(o).items()} == {k: v for k, (v, _p) in b.resolve_all(o).items()}
    assert a.resolve(o, "straight_copper")[0] == ["J1A:1", "J2A:1"]


def test_legacy_power_equals_rule_and_ampacity():
    legacy = {"power": {"+5V": {"max_current_ma": 4000, "pour": True}}}
    new = {"rule": [{"select": {"net": "^\\+5V$"}, "set": {"width_mm": 2.0, "pour": True}}]}
    o = {"net": "+5V"}
    from fluxplace import power as PW        # width now comes from the current (IPC-2221, power.py), not 0.5 mm/A
    assert RU.load(legacy).resolve(o, "width_mm")[0] == PW.rail(legacy, "+5V")["track_mm"]
    assert RU.load(new).resolve(o, "width_mm")[0] == pytest.approx(2.0)
    assert RU.load(legacy).resolve(o, "pour")[0] is True


def test_legacy_pairs_equal_rule_and_longest_family():
    legacy = {"pairs": {"USB_": {"impedance_diff": 90, "skew_mm": 3.0},
                        "USB_DP": {"skew_mm": 0.5}}}
    rs = RU.load(legacy)
    assert rs.resolve({"net": "USB_DP"}, "skew_mm")[0] == 0.5
    assert rs.resolve({"net": "USB_DM"}, "skew_mm")[0] == 3.0
    assert rs.resolve({"net": "USB_DM"}, "impedance_diff")[0] == 90
    assert rs.resolve({"net": "USB_DM"}, "skew_mm")[1] == "[pairs.USB_]"


def test_skew_accessor_text_unchanged():
    cons = {"pairs": {"ETH_": {"skew_mm": 1.0}}}
    assert C.skew_limit_source(cons, "ETH_CX0") == (1.0, "[pairs.ETH_] skew_mm")
    assert C.skew_limit_source({}, "ETH_CX0")[1] == "[si] default_skew_mm"


def test_legacy_route_floors_translate():
    rs = RU.load({"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127, "mode": "serial"}})
    assert rs.resolve({"net": "X"}, "min_track_mm") == (0.127, "[route]")
    assert rs.resolve({"net": "X"}, "mode") == (None, None)


def test_review_straight_copper_from_rule_fails_extra_pad():
    facts = {"net_pads": {"PTT_THRU": [("J1A", "1"), ("J2A", "1"), ("R9", "1")]}, "parts": {}}
    cons = {"rule": [{"select": {"net": "^PTT_THRU$"}, "set": {"straight_copper": ["J1A:1", "J2A:1"]}}]}
    f = RV.check_net_rules(facts, cons)
    assert [x["code"] for x in f] == ["NET_STRAIGHT_COPPER"]
    assert f[0]["refs"] == ["R9"]


# ---- routing integration: a [[rule]] width reaches the generated net class -------------------

def test_rule_width_reaches_injected_netclass(tmp_path, monkeypatch):
    import json
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {"classes": [], "netclass_patterns": []}}))
    monkeypatch.setattr(RU, "board_objects",
                        lambda path: ([{"net": "USB_DP", "netclass": "Default"},
                                       {"net": "GND", "netclass": "Default"}], []))
    cons = {"rule": [{"name": "usb dp", "select": {"net": "^USB_DP$"},
                      "set": {"width_mm": 0.15, "clearance_mm": 0.2}}],
            "power": {"+5V": {"max_current_ma": 1000}}}
    C.inject_netclasses(str(pro), cons, log=lambda *_: None, board="unused.kicad_pcb")
    d = json.loads(pro.read_text())
    cls = {c["name"]: c for c in d["net_settings"]["classes"]}
    assert cls["RULE_usb_dp"]["track_width"] == 0.15
    assert cls["RULE_usb_dp"]["clearance"] == 0.2
    pats = [p for p in d["net_settings"]["netclass_patterns"] if p["netclass"] == "RULE_usb_dp"]
    assert pats == [{"netclass": "RULE_usb_dp", "pattern": "USB_DP"}]


def test_no_rules_writes_no_rule_classes(tmp_path, monkeypatch):
    import json
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {}}))
    monkeypatch.setattr(RU, "board_objects", lambda path: (_ for _ in ()).throw(AssertionError("not called")))
    C.inject_netclasses(str(pro), {"power": {"+5V": {"max_current_ma": 1000}}},
                        log=lambda *_: None, board="unused.kicad_pcb")
    names = [c["name"] for c in json.loads(pro.read_text())["net_settings"]["classes"]]
    assert names == ["PWR_5V"]


def test_net_gets_every_resolved_value_across_rules(tmp_path, monkeypatch):
    # fix-c #1: A2 wins width from {net="^A"} and clearance from {net="^A2$"}; it must keep both.
    import json
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {"classes": [], "netclass_patterns": []}}))
    monkeypatch.setattr(RU, "board_objects",
                        lambda path: ([{"net": "A1", "netclass": "Default"},
                                       {"net": "A2", "netclass": "Default"}], []))
    cons = {"rule": [{"name": "wide", "select": {"net": "^A"}, "set": {"width_mm": 0.4}},
                     {"name": "gap", "select": {"net": "^A2$"}, "set": {"clearance_mm": 0.3}}]}
    C.inject_netclasses(str(pro), cons, log=lambda *_: None, board="unused.kicad_pcb")
    d = json.loads(pro.read_text())
    cls = {c["name"]: c for c in d["net_settings"]["classes"]}
    pats = {p["pattern"]: p["netclass"] for p in d["net_settings"]["netclass_patterns"]}
    a2 = cls[pats["A2"]]
    assert a2["track_width"] == 0.4 and a2["clearance"] == 0.3
    a1 = cls[pats["A1"]]
    assert a1["track_width"] == 0.4 and "clearance" not in a1


def test_longer_pair_family_beats_extra_select_key(tmp_path):
    # fix-c #2: pair family length decides before the number of select keys.
    cons = {"rule": [{"select": {"pair": "USB"}, "set": {"clearance_mm": 0.3}},
                     {"select": {"pair": "USB_DP"}, "set": {"clearance_mm": 0.2}}]}
    obj = {"net": "USB_DP", "type": "track", "layer": "F.Cu"}
    cons2 = {"rule": [{"select": {"pair": "USB_DP"}, "set": {"clearance_mm": 0.2}},
                      {"select": {"pair": "USB", "type": "track"}, "set": {"clearance_mm": 0.3}}]}
    assert RU.load(cons2).resolve(obj, "clearance_mm")[0] == 0.2
    assert RU.load(cons).resolve(obj, "clearance_mm")[0] == 0.2


def test_pair_plus_type_rule_not_false_warned_without_board():
    # fix-c #3: pair + type used to always emit RULE_MATCHES_NOTHING (type never checkable from names)
    cons = {"rule": [{"name": "usb tracks", "select": {"pair": "USB_", "type": "track"}, "set": {"width_mm": 0.2}}]}
    notes = []
    assert RU.check_matches(RU.load(cons), nets=["USB_DP"], refs=["U1"], notes=notes) == []
    assert any("type not checked" in n for n in notes)


def test_pair_plus_type_rule_evaluated_against_board_objects():
    cons = {"rule": [{"name": "usb vias", "select": {"pair": "USB_", "type": "via"}, "set": {"width_mm": 0.2}},
                     {"name": "usb tracks", "select": {"pair": "USB_", "type": "track"}, "set": {"width_mm": 0.2}}]}
    objs = [{"net": "USB_DP", "netclass": "Default", "type": "track", "layer": "F.Cu"},
            {"ref": "U1", "net": "USB_DP", "type": "pad", "layer": "F.Cu", "props": {}}]
    warn = RU.check_matches(RU.load(cons), nets=["USB_DP"], refs=["U1"], objects=objs)
    assert [r.name for r, _ in warn] == ["usb vias"]


def test_matched_nets_shows_regex_scope():
    rs = RU.load({"rule": [{"name": "usb", "select": {"net": "USB"}, "set": {"width_mm": 0.2}}]})
    nets = [{"net": "USB_DP"}, {"net": "XUSB_Q"}, {"net": "GND"}]
    assert RU.matched_nets(rs[0], nets) == ["USB_DP", "XUSB_Q"]
