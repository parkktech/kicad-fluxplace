"""Tests for fluxplace.lint — pad-list fixtures, no pcbnew needed."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lint as L


def pad(ref, num, net, fp="Conn_PinHeader_2x05", val="", drill=False):
    return dict(ref=ref, footprint=fp, value=val, pad=num, net=net,
                drill=drill)


def codes(findings):
    return {f["code"] for f in findings}


def ic(ref, nets, fp="QFN-32", val="U"):
    return [pad(ref, str(i + 1), n, fp=fp, val=val)
            for i, n in enumerate(nets)]


def test_clean_board_is_quiet():
    pads = (ic("U1", ["+3V3", "GND", "SIG_A", "SIG_B"]) +
            [pad("J1", "1", "+3V3", fp="JST_VH_B2P", val="power in"),
             pad("J1", "2", "GND", fp="JST_VH_B2P"),
             pad("J2", "1", "SIG_A", fp="Conn_JST_PH"),
             pad("J2", "2", "SIG_B", fp="Conn_JST_PH"),
             pad("J2", "3", "GND", fp="Conn_JST_PH")])
    f = L.run(pads)
    assert f == [], f


def test_no_power_entry_and_dead_end():
    pads = (ic("U1", ["+3V3", "GND", "SIG_A", "NC_NET"]) +
            [pad("J2", "1", "SIG_A", fp="Conn_JST_PH"),
             pad("J2", "2", "GND", fp="Conn_JST_PH")])
    f = L.run(pads)
    assert "no-power-entry" in codes(f)
    assert "dead-end-net" in codes(f)       # NC_NET reaches only U1


def test_unwired_connector_and_no_io():
    f = L.run(ic("U1", ["+5V", "GND", "A", "A"]))
    assert "no-io-connector" in codes(f)
    pads = (ic("U1", ["+5V", "GND", "A", "A"]) +
            [pad("J9", "1", "", fp="Conn_JST_PH"),
             pad("J9", "2", "", fp="Conn_JST_PH"),
             pad("J8", "1", "+5V", fp="JST_VH"),
             pad("J8", "2", "GND", fp="JST_VH")])
    f = L.run(pads)
    assert "unwired-connector" in codes(f)
    assert "no-io-connector" not in codes(f)


def test_no_gnd_on_part():
    pads = (ic("U2", ["+3V3", "SIG", "SIG2", "SIG3"]) +
            [pad("J1", "1", "+3V3", fp="JST_VH"),
             pad("J1", "2", "GND", fp="JST_VH")])
    f = L.run(pads)
    assert "no-gnd-on-part" in codes(f)


def test_barrel_jack_flagged():
    pads = [pad("J1", "1", "+12V", fp="BarrelJack_Horizontal", val="DC in"),
            pad("J1", "2", "GND", fp="BarrelJack_Horizontal")]
    f = L.run(pads)
    assert "barrel-jack" in codes(f)
    msg = next(x for x in f if x["code"] == "barrel-jack")["msg"]
    assert "LATCH" in msg.upper()


def test_power_on_friction_header():
    pads = (ic("U1", ["+12V", "GND", "A", "B"]) +
            [pad("J1", "1", "+12V", fp="PinHeader_1x02_P2.54mm"),
             pad("J1", "2", "GND", fp="PinHeader_1x02_P2.54mm")])
    f = L.run(pads)
    assert "power-on-friction-header" in codes(f)


def test_latching_connectors_not_flagged():
    pads = [pad("J1", "1", "+12V", fp="JST_VH_B02B", val="pwr"),
            pad("J1", "2", "GND", fp="JST_VH_B02B"),
            pad("J2", "1", "+12V", fp="Molex_MicroFit_2x02", val="pwr"),
            pad("J2", "2", "GND", fp="Molex_MicroFit_2x02")]
    f = L.run(pads)
    assert "barrel-jack" not in codes(f)
    assert "power-on-friction-header" not in codes(f)


def test_nc_pads_are_info_only():
    pads = (ic("U1", ["+3V3", "GND", "A", ""]) +
            [pad("J1", "1", "+3V3", fp="JST_VH"),
             pad("J1", "2", "GND", fp="JST_VH"),
             pad("J2", "1", "A", fp="Conn_JST_PH"),
             pad("J2", "2", "GND", fp="Conn_JST_PH")])
    f = L.run(pads)
    nn = [x for x in f if x["code"] == "no-net-pads"]
    assert len(nn) == 1 and nn[0]["severity"] == "info"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(name, "OK")


def test_suffixed_and_hierarchical_power_names():
    pads = [pad("J15", "1", "+12V_IN", fp="Molex_Micro-Fit_3.0_43045", val="pwr"),
            pad("J15", "2", "GND", fp="Molex_Micro-Fit_3.0_43045"),
            pad("J13", "1", "/POWER_ENTRY/FAN_12V", fp="PinHeader_1x04_P2.54mm"),
            pad("J13", "2", "GND", fp="PinHeader_1x04_P2.54mm")]
    f = L.run(pads)
    assert "no-power-entry" not in codes(f)          # +12V_IN counts as power
    assert "power-on-friction-header" in codes(f)    # fan header flagged


def test_waivers_suppress():
    pads = (ic("U1", ["ETH_P0_P", "GND", "+3V3", "A"]) +
            [pad("J1", "1", "+3V3", fp="JST_VH"),
             pad("J1", "2", "GND", fp="JST_VH"),
             pad("J2", "1", "A", fp="Conn_JST_PH"),
             pad("J2", "2", "GND", fp="Conn_JST_PH")])
    f = L.run(pads)
    assert "dead-end-net" in codes(f)
    f = L.run(pads, waivers=["dead-end-net:^net 'ETH_"])
    assert "dead-end-net" not in codes(f)


def test_rf_friction_coax_flagged():
    from fluxplace.lint import run
    pads = [dict(ref="J4", footprint="U.FL_Hirose_U.FL-R-SMT-1_Vertical",
                 value="U.FL", pad="1", net="RF_EXT", drill=False),
            dict(ref="J4", footprint="U.FL_Hirose_U.FL-R-SMT-1_Vertical",
                 value="U.FL", pad="2", net="GND", drill=False)]
    codes = {f["code"] for f in run(pads)}
    assert "rf-friction-coax" in codes


def test_locking_coax_not_flagged():
    from fluxplace.lint import run
    pads = [dict(ref="J4", footprint="IPEX_MHF_I_LK_20278", value="MHF LK",
                 pad="1", net="RF_EXT", drill=False),
            dict(ref="J4", footprint="IPEX_MHF_I_LK_20278", value="MHF LK",
                 pad="2", net="GND", drill=False)]
    codes = {f["code"] for f in run(pads)}
    assert "rf-friction-coax" not in codes
