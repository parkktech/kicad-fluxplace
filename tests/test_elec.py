import json
import os
import pytest

from fluxplace import elec as EL, power as PW


def _spec():
    return {"components": [
        {"ref": "U1", "value": "sw", "mpn": "SW5", "pinmap": {"IN": "5", "OUT": "1", "ILIM": "6"}},
        {"ref": "U2", "value": "ldo", "mpn": "LDO", "pinmap": {"VIN": "1", "VOUT": "5"}},
        {"ref": "U3", "value": "nofacts", "mpn": "NOPE", "pinmap": {"VDD": "1"}},
        {"ref": "J1", "value": "sock", "mpn": "SOCK"},
        {"ref": "C1", "value": "10uF 10V"}, {"ref": "C2", "value": "10uF 50V"}, {"ref": "C3", "value": "1uF"},
        {"ref": "R1", "value": "100k"}],
        "nets": [["+3V3", [["U1", "IN"], ["U1", "ILIM"], ["U3", "VDD"], ["C1", "1"]]], ["+5V", [["U2", "VIN"], ["C2", "1"], ["C3", "1"]]],
                 ["GND", [["C1", "2"]]], ["VM", [["J1", "1"], ["J1", "2"], ["U2", "VOUT"]]], ["ILIM_REF", [["U1", "OUT"], ["R1", "1"]]]]}


FACTS = {"SW5": {"vin_min": 4.5, "vin_max": 5.5, "supply_pins": ["IN"], "i_max_ma": 100,
                 "pin_rules": [{"pin": "ILIM", "rule": "needs_resistor", "allow_net_of": "IN", "note": "75 mA mode"}]},
         "LDO": {"vin_min": 2.7, "vin_max": 6.0, "supply_pins": ["VIN"], "i_out_max_a": 0.5, "i_max_ma": 5},
         "SOCK": {"contact_a": 0.5}}
CONS = {"power": {"+3V3": {"nominal_v": 3.3, "min_v": 3.135, "max_v": 3.465, "source": "U9", "source_a": 0.15},
                  "+5V": {"min_v": 4.75, "max_v": 5.25, "source": "U2", "load_extra_ma": 470, "max_current_ma": 500},
                  "VM": {"max_current_ma": 3000}}}


def _codes(f, code=None):
    return [x for x in f if x["code"] == code] if code else f


def test_supply_range_fails_below_min_quiet_when_inside():
    f = EL.check_spec(_spec(), CONS, facts=FACTS)
    sr = _codes(f, "SUPPLY_RANGE")
    assert len(sr) == 1 and sr[0]["refs"] == ["U1"] and "3.135" in sr[0]["msg"]      # LDO 2.7-6 V on +5V stays quiet


def test_missing_supply_fact_on_a_declared_rail_warns():
    f = _codes(EL.check_spec(_spec(), CONS, facts=FACTS), "SUPPLY_RANGE_UNKNOWN")
    assert len(f) == 1 and f[0]["level"] == "WARN" and f[0]["refs"] == ["U3"]


def test_not_evaluated_is_reported_never_silent():
    cons = {"power": {}}                          # no declared rail: only an INFO
    f = _codes(EL.check_spec(_spec(), cons, facts=FACTS), "ELEC_NOT_EVALUATED")
    assert any("no supply-range fact" in x["msg"] and "U3" in x["msg"] for x in f)
    assert all(x["level"] == "INFO" for x in f)
    assert not _codes(EL.check_spec(_spec(), cons, facts=FACTS), "SUPPLY_RANGE_UNKNOWN")


def test_tps2051c_like_4v5_part_on_3v3_rail_fails_with_ranges():
    sp = {"components": [{"ref": "U1", "mpn": "SW", "pinmap": {"IN": "5"}}], "nets": [["+3V3", [["U1", "IN"]]]]}
    fx = {"SW": {"vin_min": 4.5, "vin_max": 5.5, "supply_pins": ["IN"], "_src": {"vin_min": "X.pdf#p5"}}}
    f = _codes(EL.check_spec(sp, {"power": {"+3V3": {"min_v": 3.2, "max_v": 3.4}}}, facts=fx), "SUPPLY_RANGE")
    assert len(f) == 1 and f[0]["level"] == "FAIL" and "3.2..3.4" in f[0]["msg"] and "4.5..5.5" in f[0]["msg"] and "X.pdf#p5" in f[0]["msg"]


def test_supply_range_cites_vin_max_when_that_is_the_only_cited_limit():
    sp = {"components": [{"ref": "U1", "mpn": "LV", "pinmap": {"IN": "5"}}], "nets": [["+5V", [["U1", "IN"]]]]}
    fx = {"LV": {"vin_max": 3.6, "supply_pins": ["IN"], "_src": {"vin_max": "Y.pdf#p7"}}}
    f = _codes(EL.check_spec(sp, {"power": {"+5V": {"min_v": 4.9, "max_v": 5.1}}}, facts=fx), "SUPPLY_RANGE")
    assert len(f) == 1 and "part ?..3.6 V, Y.pdf#p7" in f[0]["msg"]


def test_source_overload_fail_and_warn_band():
    f = _codes(EL.check_spec(_spec(), CONS, facts=FACTS), "SOURCE_OVERLOAD")
    assert [x["level"] for x in f] == ["WARN"]                       # +5V: 5 + 470 = 475 of 500 mA (within 10 %); +3V3 100 of 150 quiet
    c = json.loads(json.dumps(CONS))
    c["power"]["+5V"]["load_extra_ma"] = 600
    assert [x["level"] for x in _codes(EL.check_spec(_spec(), c, facts=FACTS), "SOURCE_OVERLOAD")] == ["FAIL"]


def test_contact_current():
    f = _codes(EL.check_spec(_spec(), CONS, facts=FACTS), "CONTACT_CURRENT")
    assert len(f) == 1 and "2 contact(s) x 0.5 A = 1 A < 3 A" in f[0]["msg"]
    c = json.loads(json.dumps(CONS))
    c["power"]["VM"]["max_current_ma"] = 900
    assert not _codes(EL.check_spec(_spec(), c, facts=FACTS), "CONTACT_CURRENT")


def test_pin_strap_documented_mode_is_info_other_rail_warns_resistor_quiet():
    f = _codes(EL.check_spec(_spec(), CONS, facts=FACTS), "PIN_STRAP")
    assert [x["level"] for x in f] == ["INFO"] and "documented" in f[0]["msg"]
    sp = _spec()
    sp["nets"].append(["+5V", [["U1", "ILIM"]]])                      # not the IN net
    sp["nets"][0][1].remove(["U1", "ILIM"])
    assert [x["level"] for x in _codes(EL.check_spec(sp, CONS, facts=FACTS), "PIN_STRAP")] == ["WARN"]
    sp = _spec()
    sp["nets"][0][1].remove(["U1", "ILIM"])
    sp["nets"].append(["ILIM_N", [["U1", "ILIM"], ["R1", "2"]]])
    assert not _codes(EL.check_spec(sp, CONS, facts=FACTS), "PIN_STRAP")


def test_fuse_sizing():
    c = {"power": {"+5V": {"nominal_v": 5.0, "max_current_ma": 4200, "input_eff": 0.93}, "VM": {"nominal_v": 3.7, "max_current_ma": 3000, "input_eff": 0.91}},
         "fuse": {"rating_a": 5.0, "v_min": 9.0, "also_v": [6.0]}, "env": {"temp_max_c": 85}}
    f = _codes(EL.check_spec(_spec(), c, facts={}), "FUSE_SIZING")
    assert [x["level"] for x in f] == ["WARN", "WARN"]               # 3.8 A at 9 V is over 75 % of 5 A; 6 V is a sensitivity (never FAIL)
    c["fuse"]["rating_a"] = 8.0
    assert not _codes(EL.check_spec(_spec(), c, facts={}), "FUSE_SIZING")
    c["fuse"].update(rating_a=3.0)
    assert [x["level"] for x in _codes(EL.check_spec(_spec(), c, facts={}), "FUSE_SIZING")][0] == "FAIL"


def test_cap_voltage_margin():
    c = {"power": {"+3V3": {"max_v": 9.0, "surge_v": 12.0}, "+5V": {"max_v": 5.25}}}
    f = _codes(EL.check_spec(_spec(), c, facts={}), "CAP_VOLTAGE_MARGIN")
    assert [x["refs"] for x in f] == [["C1"]] and f[0]["level"] == "FAIL"      # 10 V cap under a 12 V surge; 50 V cap and unrated C3 are not failures
    nv = _codes(EL.check_spec(_spec(), c, facts={}), "ELEC_NOT_EVALUATED")
    assert any("capacitor has no voltage rating" in x["msg"] and "C3" in x["msg"] for x in nv)


def test_signal_net_leaves_unknown_rails():
    cons = {"nets": {"VREF": {"role": "signal"}}}
    assert PW.signal_nets(cons) == {"VREF"}
    unk = PW.unknown_rails(cons, {"VREF": [("U1", "1"), ("R1", "1")], "VBUSX": [("U1", "2"), ("R1", "2")]})
    assert [n for n, _v, _w in unk] == ["VBUSX"]


def test_fact_store_roundtrip_and_page_evidence(tmp_path, monkeypatch):
    from fluxplace import partdocs as PD
    (tmp_path / "X.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "datasheets.json").write_text(json.dumps({"X1": {"file": "X.pdf"}}))
    monkeypatch.setattr(PD, "page_text", lambda pdf, pages=None: "VIN Input voltage 4.5 5.5 V")
    EL.set_facts(str(tmp_path), "X1", {"vin_min": 4.5, "vin_max": 5.5, "supply_pins": ["IN"]}, "X.pdf#p7")
    assert EL.load_facts(str(tmp_path))["X1"]["vin_min"] == 4.5
    try:
        EL.set_facts(str(tmp_path), "X1", {"vin_min": 2.7}, "X.pdf#p7")
        assert False
    except ValueError as e:
        assert "not on" in str(e)
    for bad in ("X.pdf", "nope.pdf#p1"):
        try:
            EL.set_facts(str(tmp_path), "X1", {"vin_min": 4.5}, bad)
            assert False
        except ValueError:
            pass
    m = json.loads((tmp_path / "datasheets.json").read_text())
    m["X1"]["facts"]["vin_max"].pop("source")
    (tmp_path / "datasheets.json").write_text(json.dumps(m))
    assert "vin_max" not in EL.load_facts(str(tmp_path))["X1"]        # an uncited fact is never trusted


def test_per_pin_supply_limits():
    sp = {"components": [{"ref": "U1", "mpn": "M", "pinmap": {"CORE": "1", "IO": "2"}}],
          "nets": [["+1V8", [["U1", "CORE"]]], ["+3V3", [["U1", "IO"]]]]}
    fx = {"M": {"vin_min": {"CORE": 1.5, "IO": 1.1}, "vin_max": {"CORE": 1.95, "IO": 3.6}, "supply_pins": ["CORE", "IO"]}}
    c = {"power": {"+1V8": {"min_v": 1.71, "max_v": 1.89}, "+3V3": {"min_v": 3.1, "max_v": 3.5}}}
    assert not [x for x in EL.check_spec(sp, c, facts=fx) if x["code"] == "SUPPLY_RANGE"]
    c["power"]["+3V3"]["max_v"] = 3.7
    assert [x["refs"] for x in EL.check_spec(sp, c, facts=fx) if x["code"] == "SUPPLY_RANGE"] == [["U1"]]


def test_support_distance_facts_store_and_read_with_source(tmp_path, monkeypatch):
    import json
    from fluxplace import partdocs as PD
    (tmp_path / "X.pdf").write_bytes(b"x")
    monkeypatch.setattr(PD, "page_text", lambda path, pages: "decoupling within 3 mm; bulk 12 mm")
    man = {"X1": {"mpn": "X1"}}
    (tmp_path / PD.MANIFEST).write_text(json.dumps(man))
    EL.set_facts(str(tmp_path), "X1", {"decap_mm": 3, "bulk_mm": {"VIN": 12}, "supply_pins": ["VIN"]}, "X.pdf#p4")
    fx = EL.load_facts(str(tmp_path))["X1"]
    assert fx["decap_mm"] == 3 and fx["bulk_mm"] == {"VIN": 12} and fx["_src"]["decap_mm"] == "X.pdf#p4"
    assert EL.per_pin(fx, "decap_mm") == {"VIN": 3} and EL.per_pin(fx, "bulk_mm") == {"VIN": 12} and EL.per_pin(fx, "nope") == {}
    with pytest.raises(ValueError):
        EL.set_facts(str(tmp_path), "X1", {"bogus_mm": 3}, "X.pdf#p4")


def test_no_supply_fact_silences_supply_range_not_evaluated():
    spec = {"components": [{"ref": "U9", "mpn": "TVS1", "value": "TVS"}], "nets": {"N": [["U9", "1"]]}}
    f = EL.check_spec(spec, {}, facts={"TVS1": {"no_supply": True, "_src": {"no_supply": "x.pdf#p1"}}})
    assert not [x for x in f if "SUPPLY_RANGE" in x["msg"]]
    f = EL.check_spec(spec, {}, facts={})
    assert any("SUPPLY_RANGE not evaluated" in x["msg"] for x in f)
