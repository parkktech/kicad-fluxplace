"""w69: blocker data on a real board (pcbnew, no kicad-cli)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import closeexec as CX
from fluxplace import gaps as GP



def _blocker_board(tmp_path):
    import planboards as PB

    def seg(net, x0, y0, x1, y1):
        return f'(segment (start {x0} {y0}) (end {x1} {y1}) (width 0.2) (layer "F.Cu") (net "{net}"))\n'
    body = (PB.fp("A", 0, 0, [PB.pad("1", 10, 10, "T1"), PB.pad("2", 10, 14, "T2"), PB.pad("3", 80, 60, "T3")])
            + seg("X", 8, 12, 12, 12) + seg("Y", 10, 5, 10, 18) + seg("LONG", 0, 12, 90, 12))
    return PB.write(tmp_path, body)


def test_blockers_are_read_from_a_real_board_per_gap_and_for_a_gap_without_a_position(tmp_path):
    f = _blocker_board(tmp_path)
    g1 = {"id": "g1", "nets": ["T1"], "at": [10, 10]}
    assert GP._blockers_of(f, [g1], r=2.5) == ["LONG", "X", "Y"]
    assert GP._blockers_of(f, [g1], skip={"LONG"}, r=2.5) == ["X", "Y"]
    assert GP._blockers_of(f, [{"id": "g2", "nets": ["T2"], "at": None}], r=2.5) == ["LONG", "X", "Y"]      # no `at`: windowed at the net's pads
    assert GP._blockers_of(f, [{"id": "g0", "nets": ["NOPADS"], "at": None}]) is None


def test_gaps_group_by_shared_copper_not_by_a_net_that_runs_through_every_window(tmp_path):
    f = _blocker_board(tmp_path)
    gs = [{"id": "g1", "nets": ["T1"], "at": [10, 10]}, {"id": "g2", "nets": ["T2"], "at": [10, 14]},
          {"id": "g3", "nets": ["T3"], "at": [80, 60]}]
    items = {g["id"]: GP._blocker_items(f, [g], (), 2.5) for g in gs}
    assert items["g1"] & items["g2"] and not items["g1"] & items["g3"]          # g1/g2 see the same X/Y copper; LONG copper is the same item only where both windows meet
    groups = CX.group_by_shared([(g["id"], g["nets"], items[g["id"]]) for g in gs])
    assert sorted(map(sorted, groups)) == [["g1", "g2"], ["g3"]]
