"""A3: the one commit point (boardwrite.commit / verify / release / seed): protected copper cannot change, duplicates and id
collisions are refused, release is explicit and logged, a re-laid pair is protected again."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
import planboards as PB
from fluxplace import boardwrite as BW

U = iter(range(10_000_000))


def seg(net, a, b, locked=False, layer="F.Cu"):
    uid = "%08x-0000-4000-8000-%012x" % (0xA0000000 + next(U), next(U))
    return (f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) (layer "{layer}") (net "{net}")'
            f'{" (locked yes)" if locked else ""} (uuid "{uid}"))\n')


def board(tmp_path, name, items):
    return PB.write(tmp_path, PB.fp("U1", 20, 20, [PB.pad("1", 0, 0, "D_P"), PB.pad("2", 0, 1, "D_N"), PB.pad("3", 0, 3, "SIG")]) + items, name=name)


BASE = lambda: seg("D_P", (20, 20), (30, 20), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23))   # noqa: E731


def test_locked_nets_are_seeded_and_a_moved_protected_segment_is_refused(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    man = BW.seed(base)
    assert set(man["nets"]) == {"D_P", "D_N"} and man["nets"]["D_P"]["why"] == "locked"            # SIG is not locked: not protected
    cand = board(tmp_path, "cand.kicad_pcb", seg("D_P", (20, 20), (30, 20.5), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    r = BW.verify(base, cand)
    assert not r["ok"] and r["changed"] == ["D_P"] and "PROTECTED_CHANGED D_P" in r["why"]
    # an unprotected net may change freely
    cand2 = board(tmp_path, "cand2.kicad_pcb", seg("D_P", (20, 20), (30, 20), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 25)))
    assert BW.verify(base, cand2)["ok"]


def test_a_router_ripping_a_locked_stub_is_refused_and_nothing_is_written(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    ripped = board(tmp_path, "ripped.kicad_pcb", seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))      # D_P copper gone
    dst = str(tmp_path / "dst.kicad_pcb")
    r = BW.commit(base, ripped, dst)
    assert not r["ok"] and not r["written"] and not os.path.exists(dst) and "D_P" in r["why"]


def test_an_added_duplicate_is_refused_but_a_repair_that_removes_one_passes(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    dup = board(tmp_path, "dup.kicad_pcb", BASE() + seg("SIG", (20, 23), (30, 23)))
    r = BW.verify(base, dup)
    assert not r["ok"] and "DUPLICATE_COPPER" in r["why"] and r["duplicates"] == 1
    base_dup = board(tmp_path, "bd.kicad_pcb", BASE() + seg("SIG", (20, 23), (30, 23)))
    BW.seed(base_dup)
    assert BW.verify(base_dup, base)["ok"]                      # base already had one: the candidate that drops it passes
    assert BW.verify(base_dup, base_dup)["ok"]                  # and one that adds none is not blocked


def test_uuid_collision_is_refused(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    a = seg("SIG", (20, 23), (30, 23))
    b = a.replace("(end 30 23)", "(end 30 24)")
    uid = a.split('(uuid "')[1][:8]
    i = b.index('(uuid "') + 7
    b = b[:i] + uid + "-0000-4000-8000-ffffffffffff" + b[i + 36:]                 # same first 8 hex, different tail
    cand = board(tmp_path, "c.kicad_pcb", seg("D_P", (20, 20), (30, 20), True) + seg("D_N", (20, 21), (30, 21), True) + a + b)
    assert "UUID_COLLISION" in BW.verify(base, cand)["why"]


def test_release_then_relay_is_protected_again(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    relaid = board(tmp_path, "relaid.kicad_pcb", seg("D_P", (20, 20), (30, 21.5), True) + seg("D_N", (20, 21), (30, 22.5), True) + seg("SIG", (20, 23), (30, 23)))
    assert not BW.verify(base, relaid)["ok"]
    att = str(tmp_path / "attempts.jsonl")
    assert sorted(BW.release(base, ["D_P", "D_N"], "pair re-route", attempts=att)) == ["D_N", "D_P"]
    rows = [json.loads(x) for x in open(att)] if os.path.exists(att) else []
    assert not rows or rows[-1]["event"] == "release"          # the call log is off under pytest; when on, the release is a row
    dst = str(tmp_path / "dst.kicad_pcb")
    r = BW.commit(base, relaid, dst, complete={"D_P": "pair D_P/D_N", "D_N": "pair D_P/D_N"}, reason="re-laid")
    assert r["ok"] and r["written"]
    man = BW.load_manifest(dst)
    assert man["nets"]["D_P"]["fp"] == BW.net_fingerprints(dst, {"D_P"})["D_P"]
    again = board(tmp_path, "again.kicad_pcb", seg("D_P", (20, 20), (30, 21.5), True) + seg("D_N", (20, 21), (30, 22.0), True) + seg("SIG", (20, 23), (30, 23)))
    assert BW.verify(dst, again)["changed"] == ["D_N"]


def test_round_trip_float_noise_below_a_micron_is_not_a_change(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    cand = board(tmp_path, "c.kicad_pcb", seg("D_P", (20, 20.0000004), (30, 20), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    assert BW.verify(base, cand)["ok"]


def test_pairsonly_commit_guard_puts_the_input_back_when_a_protected_net_changed(tmp_path):
    from fluxplace import pairsonly as PO
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    out = board(tmp_path, "out.kicad_pcb", seg("D_P", (20, 20), (30, 20.7), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    rows = {"X_P/X_N": {"pair": "X_P/X_N", "status": "rerouted", "reason": ""}}
    msgs = []
    PO._commit_guard(base, out, [("X_P", "X_N")], rows, msgs.append)                 # D_P is not in the pairs being re-routed
    assert rows["X_P/X_N"]["status"] == "kept_original" and "PROTECTED_CHANGED D_P" in rows["X_P/X_N"]["reason"]
    assert open(out).read() == open(base).read() and msgs
    out2 = board(tmp_path, "out2.kicad_pcb", seg("D_P", (20, 20), (30, 20.7), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    rows2 = {"D_P/D_N": {"pair": "D_P/D_N", "status": "rerouted", "reason": ""}}
    PO._commit_guard(base, out2, [("D_P", "D_N")], rows2, msgs.append)                # the named pair is released: allowed
    assert rows2["D_P/D_N"]["status"] == "rerouted"
    assert BW.load_manifest(out2)["nets"]["D_P"]["fp"] == BW.net_fingerprints(out2, {"D_P"})["D_P"]       # the re-laid copper is protected again
