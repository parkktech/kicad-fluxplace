"""Tests for the 2026-11-10 review gates — facts fixtures, no pcbnew, no network.

Each one is a defect the utv-comms V1.5 board carried with every tool green:
a pad never put on its net, 25 parts under a mezzanine module, X5R caps on a
+85 C product, and (for V1.6) a bolt pattern nothing was guarding.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import review as R


def part(pads, pos=(0.0, 0.0), side="F", size=1.0, fp="R_0402_1005Metric", **kw):
    d = {"value": "x", "footprint": fp, "pads": pads, "mech": False,
         "pos": pos, "side": side,
         "bbox": (pos[0] - size / 2, pos[1] - size / 2,
                  pos[0] + size / 2, pos[1] + size / 2)}
    d.update(kw)
    return d


def facts(parts, **over):
    net_pads = {}
    for ref, p in parts.items():
        for num, net in p["pads"].items():
            if net:
                net_pads.setdefault(net, []).append((ref, num))
    f = {"parts": parts, "net_pads": net_pads, "mpn_map": {}, "partdata": {}}
    f.update(over)
    return f


def codes(finds):
    return [f["code"] for f in finds]


# ------------------------------------------------------------- spec nets
SPEC = {"components": [{"ref": "U13", "pinmap": {"INT1": "4"}}, {"ref": "J10"}],
        "nets": {"IMU_INT1": [["U13", "INT1"], ["J10", "24"]]}}


def test_single_pad_net_is_a_fail():
    f = facts({"U13": part({"4": "IMU_INT1"}), "J10": part({"24": ""})})
    assert "NET_SINGLE_PAD" in codes(R.check_spec_nets(f, None))


def test_spec_net_split_names_the_unnetted_pad():
    f = facts({"U13": part({"4": "IMU_INT1"}), "J10": part({"24": ""})})
    split = [x for x in R.check_spec_nets(f, SPEC) if x["code"] == "SPEC_NET_SPLIT"]
    assert split and "NO NET: J10.24" in split[0]["msg"]


def test_spec_net_wired_is_clean_and_pinmap_resolves():
    f = facts({"U13": part({"4": "IMU_INT1"}), "J10": part({"24": "IMU_INT1"})})
    assert R.check_spec_nets(f, SPEC) == []


def test_unconnected_autonet_and_twin_pads_are_not_single():
    f = facts({"U1": part({"7": "unconnected-(U1-Pad7)"}),
               "J1": part({"1": "SIG"}), "J2": part({"1": "SIG"})})
    assert R.check_spec_nets(f, None) == []


def test_unresolved_spec_pin_warns_once():
    spec = {"components": [{"ref": "K1"}], "nets": {"A": [["K1", "NC_A"]]}}
    out = R.check_spec_nets(facts({"K1": part({"3": "A", "4": "A"})}), spec)
    assert codes(out) == ["SPEC_PIN_UNRESOLVED"]


# --------------------------------------------------------------- keep-outs
def _cm5():
    return {"MK1": part({"1": "GND"}, (54, 8.5)), "MK3": part({"1": "GND"}, (6, 41.5)),
            "C20": part({"1": "+5V"}, (40, 15), side="B"),
            "U5": part({"1": "X"}, (40, 25), side="F"),
            "R1": part({"1": "X"}, (70, 60), side="B")}


def test_keepout_flags_only_the_forbidden_side_inside_the_region():
    cons = {"keepout": [{"name": "under CM5", "side": "B",
                         "around": ["MK*"], "allow": ["MK*"]}]}
    out = R.check_keepouts(facts(_cm5()), cons)
    assert [x["refs"] for x in out] == [["C20"]]


def test_keepout_height_limit_passes_short_parts_and_fails_unknown():
    parts = _cm5()
    parts["C21"] = part({"1": "+5V"}, (36, 15), side="B")
    f = facts(parts, mpn_map={"C20": "TALL", "C21": "SHORT"},
              partdata={"TALL": {"raw_params": {"Height - Seated (Max)": '0.057" (1.45mm)'}},
                        "SHORT": {"raw_params": {"Thickness (Max)": '0.022" (0.55mm)'}}})
    cons = {"keepout": [{"side": "B", "rect": [6, 8.5, 54, 41.5], "max_height_mm": 0.6}]}
    out = R.check_keepouts(f, cons)
    assert [x["refs"] for x in out] == [["C20"]] and "1.45 mm" in out[0]["msg"]
    f["mpn_map"] = {}
    assert len(R.check_keepouts(f, cons)) == 2          # unknown height cannot pass


def test_keepout_that_resolves_to_nothing_warns():
    out = R.check_keepouts(facts(_cm5()), {"keepout": [{"around": ["ZZ*"]}]})
    assert codes(out) == ["KEEPOUT_UNDEFINED"]


# ------------------------------------------------------------------ locks
def test_lock_moved_missing_and_clean():
    f = facts({"MH1": part({"1": "GND"}, (-3.33, -11.87))})
    assert R.check_locks(f, {"lock": {"at": {"MH1": [-3.33, -11.87, "F"]}}}) == []
    assert codes(R.check_locks(f, {"lock": {"at": {"MH1": [-3.3, -11.87]}}})) == ["LOCK_MOVED"]
    assert codes(R.check_locks(f, {"lock": {"at": {"MH9": [0, 0]}}})) == ["LOCK_MISSING"]
    assert codes(R.check_locks(f, {"lock": {"at": {"MH1": [-3.33, -11.87, "B"]}}})) == ["LOCK_MOVED"]


def test_lock_toml_round_trips():
    f = facts({"MH1": part({"1": "GND"}, (-3.33, -11.87)), "R1": part({"1": "X"})})
    text = R.lock_toml(f, ["MH*"])
    assert 'MH1 = [-3.33, -11.87, "F"]' in text and "R1" not in text


# ----------------------------------------------------------------- policy
POL = {"policy": {"grade": "automotive", "passive_max": "0402",
                  "mlcc_dielectrics": ["X7R", "C0G"], "exempt": ["J*"]}}


def test_policy_dielectric_aec_and_size():
    parts = {"C1": part({"1": "A"}, fp="C_0805_2012Metric"),
             "C2": part({"1": "A"}), "R1": part({"1": "A"}), "J1": part({"1": "A"})}
    f = facts(parts, mpn_map={"C1": "X5", "C2": "X7", "R1": "RES", "J1": "CON"},
              partdata={"X5": {"raw_params": {"Temperature Coefficient": "X5R"}},
                        "X7": {"raw_params": {"Temperature Coefficient": "X7R",
                                              "Features": "Automotive AEC-Q200"}},
                        "RES": {"raw_params": {"Temperature Coefficient": "±100ppm/°C",
                                               "Grade": "Automotive"}},
                        "CON": {"raw_params": {}}})
    out = R.check_policy(f, POL)
    by = {c: [x["refs"][0] for x in out if x["code"] == c] for c in set(codes(out))}
    assert by == {"POLICY_DIELECTRIC": ["C1"], "POLICY_NOT_AEC": ["C1"],
                  "POLICY_PASSIVE_SIZE": ["C1"]}


def test_policy_without_distributor_data_says_so_and_no_policy_is_silent():
    f = facts({"U1": part({"1": "A"}, fp="QFN-32")})
    assert codes(R.check_policy(f, POL)) == ["POLICY_UNCHECKED"]
    assert R.check_policy(f, {}) == []


def test_run_wires_the_new_gates_in():
    f = facts({"U13": part({"4": "IMU_INT1"})}, size_mm=(10, 10), copper_layers=2,
              net_tracks={}, stackup=[], plane_layers=[])
    out = R.run(f, cons={"env": {"temp_min_c": -40, "temp_max_c": 85},
                         "lock": {"at": {"MH1": [0, 0]}}})
    assert {"NET_SINGLE_PAD", "LOCK_MISSING"} <= set(codes(out))
