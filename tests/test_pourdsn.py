"""Pour-aware router job (pourdsn.py): satisfied pour nets lose their pins; a partly tied net keeps only its untied pins plus
one satisfied anchor as a derived net, mapped back to the real name in the session file."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pourdsn as PD

DSN = """(pcb x
  (network
    (net GND
      (pins A-1 B-1 C-1)
    )
    (net +3V3
      (pins D-1 E-1)
    )
    (net SIG
      (pins F-1 G-1)
    )
  )
  (wiring
    (wire (path F.Cu 150 0 0 1 1)(net GND)(type fix))
  )
)"""

REC = {
    "nets": {"GND": {"pads": 3}, "+3V3": {"pads": 2}},
    "satisfied": [{"net": "GND", "ref": "A", "pad": "1", "at": [0, 0]},
                  {"net": "GND", "ref": "B", "pad": "1", "at": [9, 0]},
                  {"net": "+3V3", "ref": "D", "pad": "1", "at": [0, 0]},
                  {"net": "+3V3", "ref": "E", "pad": "1", "at": [1, 0]}],
    "untied": [{"net": "GND", "ref": "C", "pad": "1", "at": [8, 0], "reason": "pad outside"}],
}


def test_satisfied_net_loses_pins_and_partial_net_gets_a_derived_subnet():
    out, rep = PD.apply(DSN, REC)
    assert "(net +3V3)" in out and rep["satisfied_nets"] == ["+3V3"]        # every pad satisfied: no routing job
    assert "(net GND)" in out and "GND__PT (pins C-1 B-1)" in out           # untied C + nearest satisfied anchor B
    assert rep["derived"]["GND"] == ["C-1", "B-1"]
    assert "(net SIG" in out and "(pins F-1 G-1)" in out                    # untouched net unchanged
    assert "(wire (path F.Cu 150 0 0 1 1)(net GND)" in out                  # wiring of the real net stays


def test_unmatched_pin_tokens_are_reported_not_silently_dropped():
    rec = dict(REC, satisfied=REC["satisfied"] + [{"net": "GND", "ref": "Z", "pad": "9", "at": [0, 0]}])
    out, rep = PD.apply(DSN, rec)
    assert "GND Z-9" in rep["unmatched"]


def test_session_file_names_map_back_to_the_real_net(tmp_path):
    ses = tmp_path / "out.ses"
    ses.write_text('(routes (network_out (net GND__PT (wire (path F.Cu 150 1 0 0 1) ))) (net SIG (wire ...)))')
    assert PD.unmap_ses(str(ses)) == 1
    txt = ses.read_text()
    assert "(net GND (wire" in txt and "__PT" not in txt and "(net SIG" in txt


def test_no_pour_record_leaves_the_dsn_untouched():
    assert PD.apply(DSN, None)[0] == DSN
