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


def test_pair_signature_changes_only_with_the_legs_copper_or_open_state(tmp_path):
    import planboards as PB

    def board(extra, name):
        body = (PB.fp("A", 0, 0, [PB.pad("1", 10, 10, "D_P"), PB.pad("2", 10, 12, "D_N")])
                + '(segment (start 10 10) (end 30 10) (width 0.2) (layer "F.Cu") (net "D_P"))\n' + extra)
        return PB.write(tmp_path, body, name)
    a = board('(segment (start 50 50) (end 60 50) (width 0.2) (layer "F.Cu") (net "OTHER"))\n', "a.kicad_pcb")
    b = board('(segment (start 50 50) (end 70 50) (width 0.2) (layer "F.Cu") (net "OTHER"))\n', "b.kicad_pcb")
    c = board('(segment (start 10 12) (end 30 12) (width 0.2) (layer "F.Cu") (net "D_N"))\n', "c.kicad_pcb")
    sig = lambda f, opn=(): GP._pair_sig(f, "D_P", "D_N", set(opn), "k")
    assert sig(a) == sig(b)                                  # an unrelated net's copper: the pair row is reused
    assert sig(a) != sig(c)                                  # a leg got copper
    assert sig(a) != sig(a, {"D_N"}) and sig(a) != GP._pair_sig(a, "D_P", "D_N", set(), "other-cons")


def test_pair_audit_measures_only_the_pairs_whose_signature_is_new(tmp_path, monkeypatch):
    from fluxplace import qualaudit as QA
    import planboards as PB
    body = PB.fp("A", 0, 0, [PB.pad("1", 10, 10, "D_P"), PB.pad("2", 10, 12, "D_N")])
    f = PB.write(tmp_path, body)
    calls = []
    monkeypatch.setattr(QA, "pair_rows", lambda B, cls, cons, opn, pairs=None: calls.append(pairs) or
                        [{"pair": f"{p}/{n}", "p": p, "n": n, "status": "open"} for p, n in pairs])
    from fluxplace import pairaudit as PA
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("D_P", "D_N")])
    GP._PAIR_ROWS.clear()
    r1 = GP._pair_audit(f, {}, set())[2]
    r2 = GP._pair_audit(f, {}, set())[2]
    assert calls == [[("D_P", "D_N")]] and r1 == r2          # the second audit of the same legs measured nothing


def test_a_spent_budget_still_gives_the_needed_drc_its_floor_and_critroute_logs_without_a_path(tmp_path):
    from fluxplace import tune as TU, critroute as CR
    t = [0.0]
    b = TU.Budget(10, 0, clock=lambda: t[0])
    t[0] = 500.0                                          # spent: remaining < 0
    assert b.drc_timeout() >= 120.0 and TU.Budget(None, 0).drc_timeout() == 600
    import inspect
    assert "if attempts:" in inspect.getsource(CR.apply)  # attempts=None (close driver) must not crash the rung


def test_slow_rungs_get_a_share_of_the_close_budget_not_the_full_cap():
    assert GP._rung_cap({}, "rfroute", 2400) == 240 and GP._rung_cap({}, "handrepair", 2400) == 360 and GP._rung_cap({}, "krt", 2400) == 900
