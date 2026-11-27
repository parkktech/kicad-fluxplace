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


def test_only_an_identical_item_id_collides_kicad_keys_items_by_the_whole_uuid(tmp_path):
    import pcbnew
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    a = seg("SIG", (20, 23), (30, 23))
    b = a.replace("(end 30 23)", "(end 30 24)")                                  # same uuid string, different copper
    uid = a.split('(uuid "')[1][:36]
    same_first8 = b.replace(uid, uid[:8] + "-1111-4000-8000-ffffffffffff")
    body = seg("D_P", (20, 20), (30, 20), True) + seg("D_N", (20, 21), (30, 21), True)
    ok = board(tmp_path, "ok.kicad_pcb", body + a + same_first8)
    assert sum(1 for t in pcbnew.LoadBoard(ok).GetTracks() if t.GetNetname() == "SIG") == 2        # KiCad keeps both: 8 hex digits do not collide
    assert BW.verify(base, ok)["ok"]
    bad = board(tmp_path, "bad.kicad_pcb", body + a + b)
    assert "UUID_COLLISION" in BW.verify(base, bad)["why"]


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


# ---- review findings 2 and 3: protect the existing items, not the net; the manifest knows the board it describes
def test_copper_may_be_added_to_a_net_whose_locked_copper_stays(tmp_path):
    """W15: 242 nets are all-locked fan-out stubs and 182 still have opens; closing one adds copper to the net."""
    base = board(tmp_path, "base.kicad_pcb", BASE())
    man = BW.seed(base)
    assert man["nets"]["D_P"]["why"] == "locked"
    grown = board(tmp_path, "grown.kicad_pcb", BASE() + seg("D_P", (30, 20), (35, 22)))          # an unlocked segment joins the locked stub
    assert BW.verify(base, grown)["ok"]
    moved = board(tmp_path, "moved.kicad_pcb", seg("D_P", (20, 20), (30, 20.5), True) + seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)) + seg("D_P", (30, 20), (35, 22)))
    assert BW.verify(base, moved)["changed"] == ["D_P"]                                       # the locked item moved: refused even with additions
    vanished = board(tmp_path, "gone.kicad_pcb", seg("D_N", (20, 21), (30, 21), True) + seg("SIG", (20, 23), (30, 23)))
    assert BW.verify(base, vanished)["changed"] == ["D_P"]


def test_a_partly_locked_net_protects_its_locked_items_only(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", seg("SIG", (20, 23), (30, 23), True) + seg("SIG", (30, 23), (35, 23)) + seg("D_P", (20, 20), (30, 20)))
    man = BW.seed(base)
    assert set(man["nets"]) == {"SIG"} and len(man["nets"]["SIG"]["items"]) == 1
    cand = board(tmp_path, "c.kicad_pcb", seg("SIG", (20, 23), (30, 23), True) + seg("SIG", (30, 23), (35, 25)) + seg("D_P", (20, 20), (30, 22)))
    assert BW.verify(base, cand)["ok"]                                                         # the unlocked copper may be re-routed


def test_a_complete_pair_is_protected_whole(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", seg("D_P", (20, 20), (30, 20)) + seg("D_N", (20, 21), (30, 21)))
    BW.seed(base, {"D_P": "pair D_P/D_N", "D_N": "pair D_P/D_N"}, locked=False)
    grown = board(tmp_path, "grown.kicad_pcb", seg("D_P", (20, 20), (30, 20)) + seg("D_N", (20, 21), (30, 21)) + seg("D_P", (30, 20), (31, 20)))
    assert BW.verify(base, grown)["changed"] == ["D_P"]                                       # an added item changes a pair: it was complete


def test_a_stale_manifest_is_refused_with_the_diff_and_reseeded_only_when_the_registry_recorded_it(tmp_path, monkeypatch):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    old = BW.seed(base)
    assert not BW.is_stale(base)
    txt = open(base).read().replace("(end 30 20)", "(end 30 22)", 1)                            # edited outside the flow
    open(base, "w").write(txt)
    assert BW.is_stale(base)
    r = BW.verify(base, base)
    assert not r["ok"] and "MANIFEST_STALE" in r["why"]
    msgs = []
    reg = tmp_path / "boards.json"
    monkeypatch.setenv("FLUXPLACE_VERSIONS", str(reg))
    reg.write_text(json.dumps({"versions": {"W1": {"history": [{"board": "x.kicad_pcb"}]}}}))      # an entry with no sha explains nothing
    with pytest.raises(BW.ManifestStale) as ei:
        BW.fresh_manifest(base, msgs.append)
    assert any(ln.startswith("D_P: -1 +1 items") for ln in ei.value.diff) and any("D_P: -1 +1" in m for m in msgs)
    assert BW.is_stale(base) and BW.load_manifest(base)["nets"] == old["nets"]                      # the old manifest is kept
    reg.write_text(json.dumps({"versions": {"W1": {"history": [{"board": "x.kicad_pcb", "sha": BW.file_sha(base)}]}}}))
    man = BW.fresh_manifest(base, msgs.append)                                                        # `versions accept` recorded this file
    assert not BW.is_stale(base) and set(man["nets"]) == {"D_P", "D_N"} and man["reseeded"]


def test_a_stage_that_edits_its_input_in_place_is_checked(tmp_path):
    base = board(tmp_path, "base.kicad_pcb", BASE())
    BW.seed(base)
    txt = open(base).read().replace("(end 30 20)", "(end 30 22)", 1)
    open(base, "w").write(txt)
    man = BW.load_manifest(base)
    r = BW.verify(base, base, manifest=man)                                                  # same path: the sealed manifest is the baseline
    assert not r["ok"] and r["changed"] == ["D_P"]


def test_via_identity_includes_the_layer_pair(tmp_path):
    via = lambda b: (f'(via blind (at 25 21) (size 0.6) (drill 0.3) (layers "F.Cu" "{b}") (net "D_N") (locked yes) (uuid "{next(U):08x}-0000-4000-8000-{next(U):012x}"))\n')   # noqa: E731
    head = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal))\n' + PB.EDGE
    f1, f2 = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    f1.write_text(head + via("B.Cu") + ")\n")
    f2.write_text(head + via("In1.Cu") + ")\n")
    assert BW.net_items(str(f1)) != BW.net_items(str(f2))


# ---- B8: trace-finish writes only the items it names (no stage-wide release) --------------------------------------------------

def test_trace_finish_may_drop_a_named_duplicate_on_a_protected_net_but_not_any_other_segment(tmp_path):
    from fluxplace import tracefinish as TF
    base = board(tmp_path, "base.kicad_pcb", seg("D_P", (20, 20), (30, 20), True) + seg("D_P", (20, 20), (30, 20), True)
                 + seg("D_P", (30, 20), (40, 20), True) + seg("SIG", (20, 23), (30, 23)))
    BW.seed(base)
    nodup = board(tmp_path, "nodup.kicad_pcb", seg("D_P", (20, 20), (30, 20), True) + seg("D_P", (30, 20), (40, 20), True) + seg("SIG", (20, 23), (30, 23)))
    h = BW.net_rows(base)["D_P"][0][0]
    # the clean pass names the exact duplicate it removed: authorized, committed
    ok = TF.authorize(base, nodup, [["D_P", h, "duplicate"]])
    assert BW.verify(base, nodup, allow=ok)["ok"]
    assert not BW.verify(base, nodup)["ok"]                                  # without the write set the same change is PROTECTED_CHANGED
    # a different, non-duplicate segment deleted on the protected net: nothing authorizes it
    gone = board(tmp_path, "gone.kicad_pcb", seg("D_P", (20, 20), (30, 20), True) + seg("D_P", (20, 20), (30, 20), True) + seg("SIG", (20, 23), (30, 23)))
    bad = TF.authorize(base, gone, [["D_P", h, "duplicate"]])               # the named hash is a different item than the one deleted
    r = BW.verify(base, gone, allow=bad)
    assert not r["ok"] and "PROTECTED_CHANGED D_P" in r["why"]
    # and a stage-level check: the commit point refuses it as well
    from fluxplace import pipeline as PL
    res = PL._commit_point("trace-finish", base, {"verdict": "pass", "board": gone, "authorized": bad})
    assert res["verdict"] == "fail" and "PROTECTED_CHANGED" in res["line"]
