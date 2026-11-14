"""Propagation delay (ps/mm) from stackup geometry, and ps spellings of skew limits."""
import math

import pytest

from fluxplace import constraints as C
from fluxplace import impedance as IM

C_MM_PER_PS = 0.299792458

# GND1 / 0.2 mm FR4 / S1 / 0.2 mm FR4 / GND2 : S1 is a true stripline
STRIP = [{"type": "copper", "name": "GND1", "thickness": 0.035},
         {"type": "dielectric", "thickness": 0.2, "epsilon_r": 4.3},
         {"type": "copper", "name": "S1", "thickness": 0.035},
         {"type": "dielectric", "thickness": 0.2, "epsilon_r": 4.3},
         {"type": "copper", "name": "GND2", "thickness": 0.035}]
# F.Cu microstrip over a 0.2 mm FR4 core to GND1
MICRO = [{"type": "copper", "name": "F.Cu", "thickness": 0.035},
         {"type": "dielectric", "thickness": 0.2, "epsilon_r": 4.3},
         {"type": "copper", "name": "GND1", "thickness": 0.035}]


def test_stripline_fr4_matches_closed_form():
    g = IM.geometry(STRIP, ["GND1", "GND2"], "S1")
    assert g["model"] == "stripline"
    assert IM.delay_ps_per_mm(g) == pytest.approx(math.sqrt(4.3) / C_MM_PER_PS, rel=1e-12)
    assert IM.delay_ps_per_mm(g) == pytest.approx(6.92, abs=0.01)


def test_microstrip_uses_effective_er_and_is_faster_than_stripline_limit():
    g = IM.geometry(MICRO, ["GND1"], "F.Cu")
    assert g["model"] == "microstrip"
    d = IM.delay_ps_per_mm(g)
    # effective Er lies between 1 and the dielectric Er, so delay is below the bulk FR4 value
    assert math.sqrt(1.0) / C_MM_PER_PS < d < math.sqrt(4.3) / C_MM_PER_PS
    # the 50 ohm width of the layer gives the same answer as passing that width explicitly
    w50 = IM._width_for_z0(g, 50.0)
    assert IM._z0(w50, g) == pytest.approx(50.0, abs=0.05)
    assert IM.delay_ps_per_mm(g, w_mm=w50) == pytest.approx(d, rel=1e-9)


def test_hammerstad_er_eff_limits():
    # wide trace -> Er_eff tends to Er; very narrow -> tends to (Er+1)/2 plus Hammerstad's
    # 0.04 (1-u)^2 correction term, (Er-1)/2 * 0.04 = 0.066 for Er 4.3
    assert IM._hammerstad_er_eff(1e6, 0.2, 4.3) == pytest.approx(4.3, rel=1e-3)
    assert IM._hammerstad_er_eff(1e-6, 0.2, 4.3) == pytest.approx((4.3 + 1) / 2 + (4.3 - 1) / 2 * 0.04, rel=1e-3)


def test_no_reference_plane_has_no_delay():
    stack = [{"type": "copper", "name": "F.Cu", "thickness": 0.035},
             {"type": "dielectric", "thickness": 0.2, "epsilon_r": 4.3},
             {"type": "copper", "name": "B.Cu", "thickness": 0.035}]
    assert IM.delay_ps_per_mm(IM.geometry(stack, [], "F.Cu")) is None


def test_route_delay_is_length_weighted_over_layers():
    planes = ["GND1", "GND2"]
    both = IM.route_delay_ps_per_mm(STRIP, planes, {"S1": 10.0})
    assert both == pytest.approx(IM.delay_ps_per_mm(IM.geometry(STRIP, planes, "S1")))
    assert IM.route_delay_ps_per_mm(STRIP, planes, {}) is None
    assert IM.route_delay_ps_per_mm(STRIP, planes, {"NOPE": 5.0}) is None


# ---- constraints: _ps spellings -----------------------------------------------
def test_mm_limits_unchanged():
    cons = {"pairs": {"ETH_": {"skew_mm": 0.5}}}
    assert C.skew_limit_source(cons, "ETH_L0_N") == (0.5, "[pairs.ETH_] skew_mm")
    assert C.skew_limit_mm({}, "X_P") == 1.0
    assert C.skew_limit_source({"si": {"audio_skew_mm": 4}}, "AUDIO_L")[0] == 4


def test_ps_limit_converts_with_given_delay():
    cons = {"pairs": {"ETH_": {"skew_ps": 30}}}
    mm, label = C.skew_limit_source(cons, "ETH_L0_N", ps_per_mm=6.0)
    assert mm == pytest.approx(5.0) and label == "[pairs.ETH_] skew_ps"


def test_ps_limit_accepts_callable_per_net_delay():
    cons = {"pairs": {"ETH_": {"skew_ps": 30}}}
    seen = []
    def pp(master):
        seen.append(master)
        return 5.0
    assert C.skew_limit_mm(cons, "ETH_P", ps_per_mm=pp) == pytest.approx(6.0)
    assert seen == ["ETH_P"]


def test_ps_limit_without_delay_is_refused_not_guessed():
    cons = {"pairs": {"ETH_": {"skew_ps": 30}}}
    with pytest.raises(ValueError):
        C.skew_limit_mm(cons, "ETH_P")
    with pytest.raises(ValueError):
        C.skew_limit_mm(cons, "ETH_P", ps_per_mm=lambda m: None)


def test_si_ps_keys_by_kind_and_defaults():
    cons = {"si": {"default_skew_ps": 12}}
    assert C.skew_limit_source(cons, "PLAIN_NET", ps_per_mm=6.0) == (2.0, "[si] default_skew_ps")
    cons = {"si": {"audio_skew_ps": 60}}
    mm, label = C.skew_limit_source(cons, "AUDIO_SPK_P", ps_per_mm=6.0)
    assert mm == pytest.approx(10.0)
    assert label.startswith("[si] audio_skew_ps")
    cons = {"si": {"skew_ps_by_kind": {"usb2": 60}}}
    from fluxplace import netclass as NC
    if NC.pair_kind("USB_DP") == "usb2":
        assert C.skew_limit_mm(cons, "USB_DP", ps_per_mm=6.0) == pytest.approx(10.0)


# ---- review: pair findings print ps next to mm --------------------------------
def _pair_facts(n_len):
    tr = lambda L, layers: {"length": L, "vias": 0, "layers": layers}
    return {"stackup": STRIP, "plane_layers": ["GND1", "GND2"],
            "net_pads": {"ETH_P": [], "ETH_N": []},
            "net_tracks": {"ETH_P": tr(10.0, {"S1": 10.0}), "ETH_N": tr(n_len, {"S1": n_len})}}


def test_review_pair_skew_in_ps_converts_through_the_layer():
    from fluxplace import review as R
    cons = {"pairs": {"ETH_": {"skew_ps": 30}}}
    # 5 mm of skew = 34.6 ps > 30 ps limit (limit 4.34 mm): a hard finding, ps printed next to mm
    hits = [f for f in R.check_pairs(_pair_facts(5.0), cons) if f["code"] == "PAIR_SKEW"]
    assert len(hits) == 1 and hits[0]["level"] == R.FAIL
    assert "[34.6 ps / limit 30.0 ps]" in hits[0]["msg"]
    # 2 mm of skew = 13.8 ps: clean
    assert [f for f in R.check_pairs(_pair_facts(8.0), cons) if f["code"] == "PAIR_SKEW"] == []


def test_review_pair_ps_limit_without_stackup_is_reported_not_guessed():
    from fluxplace import review as R
    cons = {"pairs": {"ETH_": {"skew_ps": 30}}}
    facts = _pair_facts(5.0)
    facts["stackup"] = []
    out = R.check_pairs(facts, cons)
    assert [f["code"] for f in out] == ["PAIR_DELAY_UNKNOWN"]
