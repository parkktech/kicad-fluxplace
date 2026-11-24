"""routediag.load carries the teardrop and remove-unused-layers flags of pads and vias (TF T1b: P8-P10, P15 read them)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import routediag

TD = ('(teardrops (best_length_ratio 0.5) (max_length 1) (best_width_ratio 1) (max_width 2) (curved_edges no) (filter_ratio 0.9) '
      '(enabled yes) (allow_two_segments yes) (prefer_zone_connections yes))')


def test_pad_and_via_flags(tmp_path):
    body = ('(footprint "X" (layer "F.Cu") (at 5 5 0) (property "Reference" "J1") '
            f'(pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu") (remove_unused_layers yes) {TD} (net "N")) '
            '(pad "2" thru_hole circle (at 3 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu") (remove_unused_layers no) (net "M")))\n'
            f'(via (at 20 20) (size 0.46) (drill 0.2) (layers "F.Cu" "B.Cu") (remove_unused_layers yes) {TD} (net "N"))\n'
            '(via (at 22 20) (size 0.46) (drill 0.2) (layers "F.Cu" "B.Cu") (net "N"))\n')
    B = routediag.load(G.board(tmp_path, body))
    pads = {p["num"]: p for p in B["pads"]}
    assert (pads["1"]["td"], pads["1"]["rm_unused"]) == (True, True) and (pads["2"]["td"], pads["2"]["rm_unused"]) == (False, False)
    assert [(v["td"], v["rm_unused"]) for v in B["vias"]] == [(True, True), (False, False)]
