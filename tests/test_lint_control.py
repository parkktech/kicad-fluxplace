"""Control-pin default-level lint (lint.control_pins): pulls, Hi-Z drivers, intent, DNP, open-drain, undriven inputs."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lint as L
import cli

R = lambda ref, v="10k": {"ref": ref, "value": v, "lib": "Resistor_SMD", "fp": "R_0402_1005Metric"}


def spec(nets, extra=()):
    comps = [{"ref": "U1", "value": "REG", "lib": "x", "fp": "QFN", "pinmap": {"EN": "1", "PGOOD": "2", "RESET_N": "3", "SENSE": "4"}},
             {"ref": "U2", "value": "IOEXP", "lib": "x", "fp": "TSSOP", "pinmap": {"P00": "4", "P01": "5", "P02": "6"}},
             {"ref": "Q1", "value": "NMOS", "lib": "x", "fp": "SOT-23", "pinmap": {"G": "1", "S": "2", "D": "3"}},
             {"ref": "J1", "value": "Header", "lib": "Connector_PinHeader", "fp": "PinHeader_1x04"},
             R("R1"), R("R2"), R("R3"), dict(R("R4"), dnp=True), R("R5", "1k")] + list(extra)
    return {"components": comps, "nets": nets}


def codes(f):
    return sorted(x["code"] for x in f)


def test_hi_z_driver_without_pull_warns_and_pull_reports_the_level():
    s = spec({"GPIO_EN": [["U1", "EN"], ["U2", "P00"]], "GND": [["U1", "5"]]})
    f = L.control_pins(s)
    assert codes(f) == ["control-pin-floating"] and "U2.P00 (GPIO output, Hi-Z until configured)" in f[0]["msg"]
    s["nets"]["GPIO_EN"].append(["R1", "1"])
    s["nets"]["GND"].append(["R1", "2"])
    f = L.control_pins(s)
    assert codes(f) == ["control-pin-default"] and "pull-down R1 10k to GND -> default LOW" in f[0]["msg"]
    f = L.control_pins(s, {"lint": {"default_level": {"GPIO_EN": "high"}}})
    assert codes(f) == ["control-pin-default", "control-pin-intent"]


def test_active_low_reset_pulled_low_is_reported_asserted():
    s = spec({"RST_LINE_N": [["U1", "RESET_N"], ["U2", "P01"], ["R2", "1"]], "GND": [["R2", "2"]]})
    f = L.control_pins(s)
    assert "active-low: ASSERTED at power-up" in f[0]["msg"]
    s = spec({"RST_LINE_N": [["U1", "RESET_N"], ["U2", "P01"], ["R2", "1"]], "+3V3": [["R2", "2"]]})
    assert "default HIGH (active-low: released" in L.control_pins(s)[0]["msg"]


def test_gate_from_connector_with_dnp_pull_and_control_named_net_is_not_a_rail():
    s = spec({"PWR_HOLD": [["Q1", "G"], ["J1", "3"], ["R4", "1"]], "GND": [["R4", "2"]]})
    f = L.control_pins(s)
    assert codes(f) == ["control-pin-dnp"] and f[0]["refs"] == ["Q1", "R4"]


def test_series_resistor_hop_brings_the_far_pull():
    s = spec({"G_DRV": [["Q1", "G"], ["R5", "2"]], "HOLD": [["R5", "1"], ["R3", "1"], ["U2", "P02"]], "GND": [["R3", "2"]]})
    f = [x for x in L.control_pins(s) if "Q1.G" in x["msg"]]
    assert codes(f) == ["control-pin-default"] and "pull-down R3 10k to GND via R5" in f[0]["msg"]


def test_open_drain_needs_a_pull_up_and_typed_inputs_need_a_driver():
    s = spec({"PG": [["U1", "PGOOD"], ["U2", "P02"]]})
    assert codes(L.control_pins(s)) == ["open-drain-no-pullup"]
    s["nets"]["PG"].append(["R1", "1"])
    s["nets"]["+3V3"] = [["R1", "2"]]
    assert codes(L.control_pins(s)) == []
    s = spec({"SNS": [["U1", "SENSE"], ["R5", "1"]], "X": [["R5", "2"]]})
    s["components"][0]["pin_types"] = {"SENSE": "input"}
    assert codes(L.control_pins(s)) == ["input-undriven"]


def test_waiver_and_cli(tmp_path, capsys):
    s = spec({"GPIO_EN": [["U1", "EN"], ["U2", "P00"]]})
    assert L.control_pins(s, waivers=["control-pin-floating:GPIO_EN"]) == []
    p = tmp_path / "s.json"
    p.write_text(json.dumps(s))
    with pytest.raises(SystemExit) as e:
        cli.main(["--full", "lint", "--spec", str(p), "--fail-on", "warning"])
    assert e.value.code == 1 and "control-pin-floating" in capsys.readouterr().out


def test_schematic_pin_types(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text('(kicad_sch (lib_symbols (symbol "lib:IC" (symbol "IC_1_1" '
                   '(pin input line (at 0 0 0) (length 1) (name "SENSE") (number "4")) '
                   '(pin open_collector line (at 0 0 0) (length 1) (name "FLAG") (number "2")))))'
                   ' (symbol (lib_id "lib:IC") (property "Reference" "U9" (at 0 0 0))))')
    t = L.sch_pin_types(str(sch))
    assert t == {"U9": {"4": ("SENSE", "input"), "2": ("FLAG", "open_collector")}}
    s = spec({"F": [["U9", "2"], ["U2", "P00"]]}, extra=[{"ref": "U9", "value": "IC", "lib": "x", "fp": "SOIC"}])
    assert codes(L.control_pins(s, sch_types=t)) == ["open-drain-no-pullup"]
