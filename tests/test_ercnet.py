"""Schematic-level ERC (fluxplace/ercnet.py): tiny synthetic netlists, positive and negative per code."""
import os

import pytest

from fluxplace import ercnet as E

SYM74 = "/usr/share/kicad/symbols/74xx.kicad_sym"

IC_PINMAP = {"VDD": "1", "GND": "2", "A": "3", "B": "4", "SDA": "5"}


def _ic(ref="U1", value="IC5", pinmap=IC_PINMAP):
    return {"ref": ref, "value": value, "lib": "x", "fp": "x", "pinmap": pinmap}


def _spec(components, nets):
    return {"components": components, "nets": nets}


def _codes(findings, code):
    return [f for f in findings if f["code"] == code]


def _rail_gnd(extra_nets=None, caps=("C1",)):
    nets = {"+3V3": [["U1", "1"]], "GND": [["U1", "2"]]}
    comps = [_ic()]
    for c in caps:
        comps.append({"ref": c, "value": "100nF", "lib": "x", "fp": "x"})
        nets["+3V3"].append([c, "1"])
        nets["GND"].append([c, "2"])
    nets.update(extra_nets or {})
    return _spec(comps, nets)


# ---- DECAP_MISSING / DECAP_FAR ------------------------------------------------
def test_decap_missing_without_cap():
    f = E.run(_rail_gnd(caps=()))
    hits = _codes(f, "DECAP_MISSING")
    assert len(hits) == 1 and hits[0]["refs"] == ["U1"] and "+3V3" in hits[0]["msg"]
    assert hits[0]["severity"] == "warning"


def test_decap_present_is_clean():
    assert _codes(E.run(_rail_gnd()), "DECAP_MISSING") == []


def test_decap_cap_on_rail_but_not_to_ground_is_missing():
    spec = _rail_gnd(caps=())
    spec["components"].append({"ref": "C2", "value": "100nF", "lib": "x", "fp": "x"})
    spec["nets"]["+3V3"].append(["C2", "1"])
    spec["nets"]["SIG"] = [["C2", "2"], ["U1", "3"]]
    assert len(_codes(E.run(spec), "DECAP_MISSING")) == 1


def test_decap_one_finding_per_ref_and_rail():
    spec = _rail_gnd(caps=())
    spec["nets"]["+3V3"].append(["U1", "5"])          # second power pin on the same rail
    hits = _codes(E.run(spec), "DECAP_MISSING")
    assert len(hits) == 1


def test_decap_far_reports_distance_and_cap():
    facts = {"parts": {"U1": {"pad_abs": {"1": (0.0, 0.0), "2": (0.0, 1.0)}},
                       "C1": {"pad_abs": {"1": (9.0, 0.0), "2": (9.0, 1.0)}}}}
    hits = _codes(E.run(_rail_gnd(), board_facts=facts), "DECAP_FAR")
    assert len(hits) == 1 and "C1" in hits[0]["refs"] and "9.0 mm" in hits[0]["msg"]


def test_decap_near_is_clean_and_limit_from_lint_section():
    facts = {"parts": {"U1": {"pad_abs": {"1": (0.0, 0.0)}},
                       "C1": {"pad_abs": {"1": (3.0, 0.0), "2": (3.0, 1.0)}}}}
    assert _codes(E.run(_rail_gnd(), board_facts=facts), "DECAP_FAR") == []
    # the same 9 mm distance is a finding under a tighter [lint] decap_mm
    far = {"parts": {"U1": {"pad_abs": {"1": (0.0, 0.0)}},
                     "C1": {"pad_abs": {"1": (9.0, 0.0), "2": (9.0, 1.0)}}}}
    assert _codes(E.run(_rail_gnd(), cons={"lint": {"decap_mm": 20}}, board_facts=far), "DECAP_FAR") == []
    assert len(_codes(E.run(_rail_gnd(), cons={"lint": {"decap_mm": 2}}, board_facts=far), "DECAP_FAR")) == 1


# ---- I2C_PULLUP_MISSING --------------------------------------------------------
def _i2c(pull=True):
    comps = [_ic(), _ic("U2", "IC5", {"SDA": "3"})]
    nets = {"SDA": [["U1", "5"], ["U2", "3"]], "+3V3": [["U1", "1"]], "GND": [["U1", "2"]]}
    if pull:
        comps.append({"ref": "R1", "value": "4k7", "lib": "x", "fp": "x"})
        nets["SDA"].append(["R1", "1"])
        nets["+3V3"].append(["R1", "2"])
    return _spec(comps, nets)


def test_i2c_pullup_missing():
    hits = _codes(E.run(_i2c(pull=False)), "I2C_PULLUP_MISSING")
    assert len(hits) == 1 and hits[0]["refs"] == ["U1", "U2"]


def test_i2c_pullup_present_is_clean():
    assert _codes(E.run(_i2c(pull=True)), "I2C_PULLUP_MISSING") == []


def test_i2c_by_net_basename_without_pin_names():
    spec = _spec([{"ref": "U9", "value": "x", "lib": "x", "fp": "x"}],
                 {"/BUS/I2C_SCL": [["U9", "1"], ["U9", "2"]]})
    assert len(_codes(E.run(spec), "I2C_PULLUP_MISSING")) == 1


# ---- PASSIVE_VALUE -------------------------------------------------------------
def test_passive_value_unparseable_is_warning():
    spec = _spec([{"ref": "R5", "value": "DNP", "lib": "x", "fp": "x"},
                  {"ref": "C5", "value": "100", "lib": "x", "fp": "x"},
                  {"ref": "L5", "value": "1.0uH", "lib": "x", "fp": "x"}],
                 {"N1": [["R5", "1"], ["C5", "1"], ["L5", "1"]]})
    hits = _codes(E.run(spec), "PASSIVE_VALUE")
    assert sorted(h["refs"][0] for h in hits) == ["C5", "R5"]
    assert all(h["severity"] == "warning" for h in hits)


def test_passive_value_parseable_is_clean():
    spec = _spec([{"ref": "R5", "value": "4k7 1%", "lib": "x", "fp": "x"},
                  {"ref": "C5", "value": "100nF X7R", "lib": "x", "fp": "x"}],
                 {"N1": [["R5", "1"], ["C5", "1"]]})
    assert _codes(E.run(spec), "PASSIVE_VALUE") == []


def test_pull_resistor_outside_range_is_info():
    base = _spec([_ic(), {"ref": "R7", "value": "270R", "lib": "x", "fp": "x"}],
                 {"+3V3": [["U1", "1"], ["R7", "1"]], "EN": [["U1", "3"], ["R7", "2"]],
                  "GND": [["U1", "2"]]})
    hits = [f for f in _codes(E.run(base), "PASSIVE_VALUE") if f["severity"] == "info"]
    assert len(hits) == 1 and hits[0]["refs"] == ["R7"]
    base["components"][1]["value"] = "10k"
    assert [f for f in _codes(E.run(base), "PASSIVE_VALUE") if f["severity"] == "info"] == []


def test_series_led_resistor_is_not_a_pull():
    spec = _spec([_ic(), {"ref": "R8", "value": "270R", "lib": "x", "fp": "x"},
                  {"ref": "D8", "value": "LED", "lib": "x", "fp": "x"}],
                 {"+3V3": [["U1", "1"], ["R8", "1"]], "KEY_LED_A": [["U1", "3"], ["R8", "2"], ["D8", "1"]],
                  "GND": [["U1", "2"]]})
    assert [f for f in _codes(E.run(spec), "PASSIVE_VALUE") if f["severity"] == "info"] == []


def test_parse_ohm():
    assert E.parse_ohm("10k") == 10000
    assert E.parse_ohm("4k7") == 4700
    assert E.parse_ohm("100R") == 100
    assert E.parse_ohm("1M") == 1e6
    assert E.parse_ohm("0R") == 0
    assert E.parse_ohm("100nF") is None


# ---- NET_NO_DRIVER (needs official symbol pin types) ---------------------------
@pytest.mark.skipif(not os.path.exists(SYM74), reason="KiCad 74xx symbol library not installed")
def test_net_no_driver_all_inputs():
    comp = {"ref": "U3", "value": "74HC04", "lib": "74xx", "fp": "x"}
    spec = _spec([comp], {"X": [["U3", "1"], ["U3", "3"]]})   # pins 1 and 3 are inputs of the 74HC04
    hits = _codes(E.run(spec, idx={"74HC04": (SYM74, "74HC04")}), "NET_NO_DRIVER")
    assert len(hits) == 1 and hits[0]["severity"] == "info"


@pytest.mark.skipif(not os.path.exists(SYM74), reason="KiCad 74xx symbol library not installed")
def test_net_with_driver_is_clean():
    comp = {"ref": "U3", "value": "74HC04", "lib": "74xx", "fp": "x"}
    spec = _spec([comp], {"X": [["U3", "1"], ["U3", "2"]]})   # input + output
    assert _codes(E.run(spec, idx={"74HC04": (SYM74, "74HC04")}), "NET_NO_DRIVER") == []


def test_pads_input_runs_without_pin_names():
    pads = [dict(ref="R1", value="DNP", footprint="R", pad="1", net="A"),
            dict(ref="R1", value="DNP", footprint="R", pad="2", net="B")]
    assert len(_codes(E.run(pads), "PASSIVE_VALUE")) == 1
    assert _codes(E.run(pads), "NET_NO_DRIVER") == []


def test_waive_drops_matching_finding():
    f = E.run(_rail_gnd(caps=()))
    assert E.waive(f, ["DECAP_MISSING:U1"]) == []
    assert len(E.waive(f, ["DECAP_MISSING:U9"])) == 1
