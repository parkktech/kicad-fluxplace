"""Tests for fluxplace.review — facts fixtures, no pcbnew, no network.

Each test is one failure class from the utv-comms V1.4 external review
(2026-09-03) that reached a human reviewer with every tool green.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import review as R
from fluxplace import partdata as PD
from fluxplace import constraints as C
from fluxplace import intake as IN


# PCBWay 6-layer 1.6 mm, as written into the V1.4 board
STACK6 = [
    {"type": "copper", "name": "F.Cu", "thickness": 0.035},
    {"type": "prepreg", "name": None, "thickness": 0.1, "epsilon_r": 4.1},
    {"type": "copper", "name": "In1.Cu", "thickness": 0.0152},
    {"type": "core", "name": None, "thickness": 0.2, "epsilon_r": 4.5},
    {"type": "copper", "name": "In2.Cu", "thickness": 0.0152},
    {"type": "prepreg", "name": None, "thickness": 0.8692, "epsilon_r": 4.4},
    {"type": "copper", "name": "In3.Cu", "thickness": 0.0152},
    {"type": "core", "name": None, "thickness": 0.2, "epsilon_r": 4.5},
    {"type": "copper", "name": "In4.Cu", "thickness": 0.0152},
    {"type": "prepreg", "name": None, "thickness": 0.1, "epsilon_r": 4.1},
    {"type": "copper", "name": "B.Cu", "thickness": 0.035},
]


def part(value, fp, pads, **kw):
    d = {"value": value, "footprint": fp, "pads": pads,
         "connectable_pads": len(pads), "mech": False, "tht": False}
    d.update(kw)
    return d


def facts(**over):
    f = {
        "size_mm": (76.5, 86.6), "copper_layers": 6,
        "parts": {
            "J1A": part("TB5M", "utv:XLR5", {"1": "PTT_THRU", "2": "GND"}),
            "J2A": part("TB5M", "utv:XLR5", {"1": "PTT_THRU", "2": "GND"}),
        },
        "net_pads": {"PTT_THRU": [("J1A", "1"), ("J2A", "1")], "GND": []},
        "net_tracks": {},
        "stackup": STACK6, "plane_layers": ["In1.Cu", "In4.Cu"],
        "mpn_map": {},
    }
    f.update(over)
    return f


def codes(finds, level=None):
    return {f["code"] for f in finds if level is None or f["level"] == level}


# ----------------------------------------------------------------- net rules
def test_straight_copper_flags_esd_on_ptt():
    f = facts()
    f["parts"]["D7"] = part("SP0504", "Package_TO_SOT_SMD:SOT-23-5",
                            {"1": "PTT_THRU", "2": "GND"})
    f["net_pads"]["PTT_THRU"].append(("D7", "1"))
    cons = {"nets": {"PTT_THRU": {"straight_copper": ["J1A:1", "J2A:1"]}}}
    out = R.check_net_rules(f, cons)
    assert codes(out) == {"NET_STRAIGHT_COPPER"}
    assert out[0]["level"] == "FAIL" and "D7.1" in out[0]["msg"]


def test_straight_copper_quiet_when_clean():
    cons = {"nets": {"PTT_THRU": {"straight_copper": ["J1A:1", "J2A:1"]}}}
    assert R.check_net_rules(facts(), cons) == []


# ---------------------------------------------------------------- diff pairs
def test_pair_skew_and_layer_mismatch():
    f = facts(net_pads={"ETH_L0_P": [], "ETH_L0_N": []},
              net_tracks={
                  "ETH_L0_P": {"length": 35.1, "layers": {"In2.Cu": 31.5, "F.Cu": 3.6},
                               "vias": 1, "segments": []},
                  "ETH_L0_N": {"length": 80.4, "layers": {"In3.Cu": 78.4, "F.Cu": 2.0},
                               "vias": 3, "segments": []}})
    out = R.check_pairs(f, {})
    assert "PAIR_SKEW" in codes(out, "FAIL")
    assert {"PAIR_LAYER_MISMATCH", "PAIR_VIA_MISMATCH"} <= codes(out, "WARN")


def test_is_rf_is_token_based():
    assert R.is_rf("RF_GNSS") and R.is_rf("RF_INT_SW") and R.is_rf("ANT1_RF")
    assert not R.is_rf("PWRFAIL") and not R.is_rf("ANT_SEL1") and not R.is_rf("GNSS_RX")


def test_audio_pair_skew_is_advice_not_stop_ship():
    f = facts(net_pads={"MIC_P": [], "MIC_N": []},
              net_tracks={"MIC_P": {"length": 57.9, "layers": {"F.Cu": 57.9}, "vias": 3, "segments": []},
                          "MIC_N": {"length": 68.0, "layers": {"In2.Cu": 68.0}, "vias": 4, "segments": []}})
    out = R.check_pairs(f, {})
    assert codes(out, "WARN") == {"PAIR_SKEW"} and not codes(out, "FAIL")
    out = R.check_pairs(f, {"pairs": {"MIC_": {"skew_mm": 1.0}}})
    assert "PAIR_SKEW" in codes(out, "FAIL")


def test_pair_within_family_limit_is_quiet():
    f = facts(net_pads={"ETH_L0_P": [], "ETH_L0_N": []},
              net_tracks={
                  "ETH_L0_P": {"length": 35.1, "layers": {"In2.Cu": 35.1}, "vias": 1, "segments": []},
                  "ETH_L0_N": {"length": 36.0, "layers": {"In2.Cu": 36.0}, "vias": 1, "segments": []}})
    assert R.check_pairs(f, {"pairs": {"ETH_": {"skew_mm": 2.0}}}) == []


# ------------------------------------------------------------- RF impedance
def test_outer_layer_0p15_is_50_ohm_inner_is_not():
    g_outer = R.layer_geometry(STACK6, {"In1.Cu", "In4.Cu"}, "F.Cu")
    z, model = R.z0_on_layer(0.15, g_outer)
    assert model == "microstrip" and 45 < z < 55
    g_in2 = R.layer_geometry(STACK6, {"In1.Cu", "In4.Cu"}, "In2.Cu")
    z2, model2 = R.z0_on_layer(0.15, g_in2)
    assert model2 == "stripline" and z2 > 58        # ~66 ohm: the V1.4 miss


def test_rf_graded_on_actual_layer():
    f = facts(net_pads={"RF_GNSS": []},
              net_tracks={"RF_GNSS": {"length": 41.1, "vias": 2,
                                      "layers": {"In2.Cu": 34.8, "F.Cu": 3.0, "B.Cu": 3.3},
                                      "segments": [("In2.Cu", 0.15, 34.8),
                                                   ("F.Cu", 0.15, 3.0),
                                                   ("B.Cu", 0.15, 3.3)]}})
    out = R.check_rf(f, {})
    assert "RF_IMPEDANCE_OFF" in codes(out, "FAIL")
    assert "RF_VIA_COUNT" in codes(out)
    msg = next(o["msg"] for o in out if o["code"] == "RF_IMPEDANCE_OFF")
    assert "In2.Cu" in msg


def test_rf_outer_only_is_quiet():
    f = facts(net_pads={"RF_GNSS": []},
              net_tracks={"RF_GNSS": {"length": 20.0, "vias": 0,
                                      "layers": {"F.Cu": 20.0},
                                      "segments": [("F.Cu", 0.15, 20.0)]}})
    assert R.check_rf(f, {}) == []


# ------------------------------------------------------------- packages
def test_package_key_readings():
    assert ("SOT23", 5) in R.package_key("SOT-23-5")
    assert ("SOT23", 5) in R.package_key("SC-74A, SOT-753")
    assert ("SOT23", 6) in R.package_key("SOT-23-6 Thin, TSOT-23-6")
    assert ("SOIC", 8) in R.package_key('8-SOIC (0.154", 3.90mm Width)')
    assert ("SOIC", 8) in R.package_key("SOIC-8_3.9x4.9mm_P1.27mm")
    assert ("SOIC", 8) in R.package_key("HSOP-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.1mm")
    assert ("POWERDI", 8) in R.package_key("8-PowerDI5060")
    assert ("POWERDI", 8) in R.package_key("PowerDI5060-8")
    assert ("CHIP", "0603") in R.package_key("0603 (1608 Metric)")
    assert ("CHIP", "0603") in R.package_key("R_0603_1608Metric")
    assert ("SMC", 2) in R.package_key("DO-214AB, SMC")
    assert ("SMC", 2) in R.package_key("D_SMC")
    assert ("DFN", 10) in R.package_key("10-WSON (2x3)")
    assert ("DFN", 10) in R.package_key("WSON-10-1EP_2x3mm_P0.5mm_EP0.84x2.4mm")


def test_packages_agree_verdicts():
    assert R.packages_agree("8-PowerDI5060", "SOIC-8_3.9x4.9mm_P1.27mm")[0] == "family"
    assert R.packages_agree("SOT-23-6", "SOT-23-5")[0] == "pins"
    assert R.packages_agree("SOT-23-5", "SOT-23-5")[0] == "ok"
    assert R.packages_agree('8-SOIC (0.154", 3.90mm Width)', "SOIC-8_3.9x4.9mm_P1.27mm")[0] == "ok"
    assert R.packages_agree("Module", "ublox_MAX")[0] == "unknown"
    assert R.packages_agree("1008 (2520 Metric)", "L_0805_2012Metric")[0] == "family"
    assert R.packages_agree("0603 (1608 Metric)", "C_0603_1608Metric")[0] == "ok"


def test_check_parts_q1_powerdi_on_soic():
    f = facts(mpn_map={"Q1": "DMP4015SPS-13"})
    f["parts"]["Q1"] = part("DMP4015SPS", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                            {str(i): "N" for i in range(1, 9)})
    pd = {"DMP4015SPS-13": {"package": "8-PowerDI5060", "pins": 8,
                            "temp_min": -55, "temp_max": 150, "lifecycle": "Active"}}
    out = R.check_parts(f, None, pd, {})
    assert "FOOTPRINT_PACKAGE_MISMATCH" in codes(out, "FAIL")


def test_temp_rating_against_env():
    f = facts(mpn_map={"T3": "749020111A"})
    f["parts"]["T3"] = part("749020111A", "utv:WE_749020111A_SOIC24",
                            {str(i): "N" for i in range(1, 25)})
    pd = {"749020111A": {"package": None, "pins": None, "temp_min": 0,
                         "temp_max": 70, "lifecycle": "Active"}}
    cons = {"env": {"temp_min_c": -30, "temp_max_c": 85}}
    out = R.check_parts(f, None, pd, cons)
    assert "TEMP_RATING" in codes(out, "FAIL")
    assert R.check_parts(f, None, pd, {}) == [] or "TEMP_RATING" not in codes(
        R.check_parts(f, None, pd, {}))


def test_unchecked_parts_are_reported_not_passed():
    f = facts(mpn_map={"U9": "NOPE-123"})
    f["parts"]["U9"] = part("x", "lib:SOT-23-6", {"1": "A"})
    out = R.check_parts(f, None, {"NOPE-123": None}, {})
    assert "PART_DATA_UNAVAILABLE" in codes(out, "FAIL")      # strict docs by default
    out = R.check_parts(f, None, {"NOPE-123": None}, {"docs": {"strict": False}})
    assert "PART_DATA_UNAVAILABLE" in codes(out, "WARN")


# ------------------------------------------------------ pinmap vs library
SYM = '''(kicad_symbol_lib
\t(symbol "SP0504BAHT"
\t\t(property "Value" "SP0504BAHT" (at 0 0 0))
\t\t(symbol "SP0504BAHT_0_1"
\t\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t\t(name "A" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "2" (effects (font (size 1.27 1.27))))
\t\t\t)
\t\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t\t(name "K" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))
\t\t\t)
\t\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t\t(name "K" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "3" (effects (font (size 1.27 1.27))))
\t\t\t)
\t\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t\t(name "K" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "4" (effects (font (size 1.27 1.27))))
\t\t\t)
\t\t\t(pin passive line (at 0 0 0) (length 2.54)
\t\t\t\t(name "K" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "5" (effects (font (size 1.27 1.27))))
\t\t\t)
\t\t)
\t)
\t(symbol "Other_1"
\t\t(property "Value" "x" (at 0 0 0))
\t)
)
'''


def _libdir():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "Power_Protection.kicad_sym"), "w") as fh:
        fh.write(SYM)
    return d


def test_lib_index_and_pins():
    idx = R.lib_index([_libdir()])
    assert "SP0504BAHT" in idx
    path, name = idx["SP0504BAHT"]
    assert R.lib_pins(path, name) == {"2": "A", "1": "K", "3": "K", "4": "K", "5": "K"}
    assert R.lib_symbol_for("SP0504BAHTG", "SP0504", idx)[1] == "SP0504BAHT"
    assert R.lib_symbol_for("LM", "LM", idx) is None


def test_pinmap_gnd_on_wrong_pin_fails():
    idx = R.lib_index([_libdir()])
    f = facts(mpn_map={"D7": "SP0504BAHTG"})
    f["parts"]["D7"] = part("SP0504", "Package_TO_SOT_SMD:SOT-23-5",
                            {"1": "PTT", "2": "SPK", "3": "SPK_N", "4": "GND", "5": "MIC"})
    spec = {"components": [{"ref": "D7", "value": "SP0504", "fp": "SOT-23-5",
                            "pinmap": {"IO1": "1", "IO2": "2", "IO3": "3",
                                       "IO4": "5", "GND": "4"}}]}
    out = R.check_parts(f, spec, {}, {}, idx=idx)
    roles = [o for o in out if o["code"] == "PINMAP_ROLE_MISMATCH"]
    assert roles and all(o["level"] == "FAIL" for o in roles)
    assert any("GND on pin 4" in o["msg"] for o in roles)
    assert any("IO2 on pin 2" in o["msg"] for o in roles)


def test_pinmap_correct_is_quiet():
    idx = R.lib_index([_libdir()])
    f = facts(mpn_map={"D7": "SP0504BAHTG"})
    f["parts"]["D7"] = part("SP0504", "Package_TO_SOT_SMD:SOT-23-5",
                            {"1": "A", "2": "GND", "3": "B", "4": "C", "5": "D"})
    spec = {"components": [{"ref": "D7", "value": "SP0504", "fp": "SOT-23-5",
                            "pinmap": {"IO1": "1", "GND": "2", "IO2": "3",
                                       "IO3": "4", "IO4": "5"}}]}
    assert R.check_parts(f, spec, None, {}, idx=idx) == []


def test_pinmap_without_evidence_warns():
    f = facts()
    f["parts"]["U3"] = part("MAX-M10S", "RF_GPS:ublox_MAX", {"1": "GND"})
    spec = {"components": [{"ref": "U3", "value": "MAX-M10S", "fp": "ublox_MAX",
                            "pinmap": {"GND1": "1"}}]}
    out = R.check_parts(f, spec, None, {}, idx=R.lib_index([_libdir()]))
    assert "PINMAP_UNVERIFIED" in codes(out, "WARN")
    spec["components"][0]["pinmap_source"] = "MAX-M10S integration manual p.12"
    assert R.check_parts(f, spec, None, {}, idx=R.lib_index([_libdir()])) == []


def test_pin_role():
    for n, r in (("GND", "GND"), ("AGND", "GND"), ("VSS", "GND"), ("VCC", "PWR"),
                 ("VIN", "PWR"), ("+5V", "PWR"), ("A", "COMMON"), ("K", "IO"),
                 ("NC", "NC"), ("SDA", "IO"), ("GND12", "GND"), ("V-", "GND")):
        assert R.pin_role(n) == r, n


# ----------------------------------------------------------------- spec sync
def test_spec_sync_size_layers_components():
    spec = {"board": {"width_mm": 65, "height_mm": 50, "layer_count": 4},
            "components": [{"ref": "J1A"}, {"ref": "J2A"}, {"ref": "U99"}]}
    out = R.check_spec_sync(facts(), spec)
    assert codes(out, "FAIL") == {"SPEC_SIZE_MISMATCH", "SPEC_LAYER_MISMATCH",
                                  "SPEC_COMPONENT_MISMATCH"}
    good = {"board": {"width_mm": 76.5, "height_mm": 86.6, "layer_count": 6},
            "components": [{"ref": "J1A"}, {"ref": "J2A"}]}
    assert R.check_spec_sync(facts(), good) == []


# ------------------------------------------------------------------ power
def test_holdup_math():
    f = facts(net_pads={"+5V": [], "GND": []})
    f["parts"]["C12"] = part("1500uF", "Capacitor_THT:CP_Radial", {"1": "+5V", "2": "GND"})
    f["parts"]["C13"] = part("1500uF", "Capacitor_THT:CP_Radial", {"1": "+5V", "2": "GND"})
    cons = {"power": {"+5V": {"holdup_ms": 20, "nominal_v": 5.0, "min_v": 4.75,
                              "load_a": 1.0}}}
    out = R.check_power(f, cons)
    assert codes(out, "FAIL") == {"HOLDUP_SHORT"}
    assert "0.75 ms" in out[0]["msg"]
    cons["power"]["+5V"]["holdup_ms"] = 0.5
    assert R.check_power(f, cons) == []


def test_tvs_margin():
    cons = {"protection": {"tvs_ref": "D1", "downstream_max_v": 40, "clamp_v": 38.9}}
    out = R.check_power(facts(), cons)
    assert codes(out, "WARN") == {"TVS_MARGIN"}
    cons["protection"]["clamp_v"] = 45
    assert codes(R.check_power(facts(), cons), "FAIL") == {"TVS_MARGIN"}
    cons["protection"]["clamp_v"] = 30
    assert R.check_power(facts(), cons) == []


# -------------------------------------------------------------------- env
def test_env_undefined_warns_and_profile_parses():
    assert "ENV_UNDEFINED" in codes(R.check_env(facts(), {}), "WARN")
    cons = {"env": {"temp_min_c": -40, "temp_max_c": 85, "vibration": "HIGH"}}
    assert R.check_env(facts(), cons) == []
    assert C.env_profile(cons)["vibration"] == "high"
    assert C.env_profile({"env": {"vibration": "high"}}) is None
    assert C.env_toml({"temp_min_c": -40, "temp_max_c": 85, "vibration": "high"}) \
        .startswith("[env]\ntemp_min_c = -40\ntemp_max_c = 85\n")


def test_intake_environment_answers_and_prompt():
    # pre-filled answers pass straight through
    ans = {"interfaces": [], "mounting": {"holes": False},
           "environment": {"temp_min_c": -40, "temp_max_c": 85}}
    assert IN.run(answers=ans)["environment"]["temp_min_c"] == -40
    # driven prompt: choose 'exposed-vehicle' then 'auto-12v'
    seq = iter(["3", "2"])
    env = IN.ask_environment(lambda p: next(seq), lambda s: None)
    assert env["temp_min_c"] == -40 and env["vibration"] == "high"
    assert env["moisture"] == "outdoor" and env["transient"] == "auto-12v"


# ---------------------------------------------------------------- partdata
def test_partdata_parsers():
    assert PD.parse_temp_range("-40°C ~ 125°C (TJ)") == (-40, 125)
    assert PD.parse_temp_range("0°C ~ 70°C") == (0, 70)
    assert PD.parse_temp_range("-40 C to +85 C") == (-40, 85)
    assert PD.parse_temp_range("125°C") is None
    assert PD.parse_pins("8-Pin") == 8 and PD.parse_pins("SOT-23-5") is None
    assert R._pins_from_pkg("SOT-23-5") == 5 and R._pins_from_pkg("SOT-23") == 3
    assert PD.parse_clamp_v("38.9V") == 38.9
    n = PD.normalize({"Package / Case": "SOT-23-5",
                      "Operating Temperature": "-40°C ~ 125°C"})
    assert n["package"] == "SOT-23-5" and n["pins"] is None and n["temp_max"] == 125
    n = PD.normalize({"Minimum Operating Temperature": "- 40 C",
                      "Maximum Operating Temperature": "+ 85 C"})
    assert (n["temp_min"], n["temp_max"]) == (-40, 85)


# -------------------------------------------------------------- run/waive
def test_run_orders_and_waives():
    f = facts()
    f["parts"]["D7"] = part("SP0504", "lib:SOT-23-5", {"1": "PTT_THRU"})
    f["net_pads"]["PTT_THRU"].append(("D7", "1"))
    cons = {"nets": {"PTT_THRU": {"straight_copper": ["J1A:1", "J2A:1"]}}}
    out = R.run(f, cons=cons)
    assert out[0]["level"] == "FAIL" and out[0]["code"] == "NET_STRAIGHT_COPPER"
    out = R.run(f, cons=cons, waivers=["NET_STRAIGHT_COPPER:PTT_THRU"])
    assert "NET_STRAIGHT_COPPER" not in codes(out)


def test_parse_stackup_text():
    raw = '''(general)
\t(layers)
\t(setup
\t\t(stackup
\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))
\t\t\t(layer "F.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.035)
\t\t\t)
\t\t\t(layer "dielectric 1"
\t\t\t\t(type "prepreg")
\t\t\t\t(thickness 0.1)
\t\t\t\t(material "FR4")
\t\t\t\t(epsilon_r 4.1)
\t\t\t\t(loss_tangent 0.02)
\t\t\t)
\t\t\t(layer "In1.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.0152)
\t\t\t)
\t\t\t(copper_finish "ENIG")
\t\t)
\t\t(pad_to_mask_clearance 0)
\t)
'''
    rows = R.parse_stackup_text(raw)
    assert [r["type"] for r in rows] == ["copper", "prepreg", "copper"]
    assert rows[1]["thickness"] == 0.1 and rows[1]["epsilon_r"] == 4.1
    assert rows[2]["name"] == "In1.Cu"


# ------------------------------------------------------------- drcfix parse
def test_drc_item_description_regex():
    from fluxplace import drcfix as DF
    m = DF._DESC.match("Track [RF_GNSS] on In2.Cu, length 17.6112 mm")
    assert m.groups() == ("Track", None, "RF_GNSS", None, "In2.Cu", "17.6112")
    m = DF._DESC.match("Via [ETH_P0_P] on F.Cu - B.Cu")
    assert m.group(1) == "Via" and m.group(3) == "ETH_P0_P"
    m = DF._DESC.match("Pad 5 [VBATT_IN] of Q1 on F.Cu")
    assert m.group(2) == "5" and m.group(3) == "VBATT_IN" and m.group(4) == "Q1"
    assert DF._DESC.match("Reference field of Q1") is None


def test_review_waivers_from_constraints_shape():
    # [review] waive entries are plain CODE:REGEX strings, same as --waive
    cons = {"review": {"waive": ["TEMP_RATING:^T3 "]}}
    f = facts(mpn_map={"T3": "X"})
    f["parts"]["T3"] = part("X", "lib:SOIC-24", {"1": "A"})
    pd = {"X": {"package": None, "pins": None, "temp_min": 0, "temp_max": 70}}
    env = {"env": {"temp_min_c": -40, "temp_max_c": 85}}
    out = R.run(f, cons=env, partdata=pd, waivers=cons["review"]["waive"])
    assert "TEMP_RATING" not in codes(out)


def test_landpattern_geometry_and_gate(tmp_path):
    """Pulse HX5084: 24 pads, 0.99 pitch, two rows 8.99 apart. A footprint
    drawn at 1.27 pitch against a spec citing 0.99 must FAIL; a matching one
    with an unreadable drawing page WARNs; an uncited project footprint FAILs."""
    import os
    from fluxplace import review as R
    def pads(pitch):
        g = []
        for i in range(12):
            x = round((i - 5.5) * pitch, 3)
            g.append((str(i + 1), x, 4.495, 0.64, 1.78))
            g.append((str(24 - i), x, -4.495, 0.64, 1.78))
        return g
    geo = R.landpattern_geometry(pads(0.99))
    assert geo["pitch"] == 0.99 and geo["rows"] == 8.99 and geo["pins"] == 24 and geo["pad"] == (0.64, 1.78)
    lib = tmp_path / "proj.pretty"
    lib.mkdir()
    (lib / "Pulse_H5084_SMD24.kicad_mod").write_text("(footprint)")
    (lib / "Other_Drawn.kicad_mod").write_text("(footprint)")
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "HX.pdf").write_bytes(b"%PDF-1.4\n")   # unreadable drawing
    facts = {"parts": {
        "T3": {"footprint": "Pulse_H5084_SMD24", "pad_geom": pads(1.27), "mech": False},
        "T4": {"footprint": "Pulse_H5084_SMD24", "pad_geom": pads(0.99), "mech": False},
        "X1": {"footprint": "Other_Drawn", "pad_geom": pads(0.99), "mech": False},
        "C1": {"footprint": "C_0402_1005Metric", "pad_geom": [], "mech": False},
    }}
    lp = {"source": "HX.pdf#p3", "pitch": 0.99, "pad": [0.64, 1.78], "rows": 8.99, "pins": 24}
    spec = {"components": [{"ref": "T3", "landpattern": lp}, {"ref": "T4", "landpattern": lp},
                           {"ref": "X1"}, {"ref": "C1"}]}
    finds = R.check_landpattern(facts, spec, str(ds), project_libs=[str(lib)], strict=True)
    codes = {(f["code"], f["refs"][0], f["level"]) for f in finds}
    assert ("LANDPATTERN_MISMATCH", "T3", "FAIL") in codes
    assert ("LANDPATTERN_PAGE_UNREADABLE", "T4", "WARN") in codes
    assert not any(c == "LANDPATTERN_MISMATCH" and r == "T4" for c, r, _ in codes)
    assert ("LANDPATTERN_UNCITED", "X1", "FAIL") in codes
    assert not any(r == "C1" for _, r, _ in codes)


def test_models_gate(tmp_path):
    from fluxplace import review as R
    good = tmp_path / "ok.step"
    good.write_text("x")
    facts = {"board_path": str(tmp_path / "b.kicad_pcb"), "parts": {
        "U1": {"footprint": "X", "models": [str(good)], "mech": False},
        "U2": {"footprint": "Y", "models": [], "mech": False},
        "J1": {"footprint": "Z", "models": [str(tmp_path / "nope.step")], "mech": False},
        "MH1": {"footprint": "Hole", "models": [], "mech": True},
    }}
    codes = {(f["code"], f["refs"][0]) for f in R.check_models(facts)}
    assert codes == {("MODEL_MISSING", "U2"), ("MODEL_FILE_MISSING", "J1")}
