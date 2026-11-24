"""PHASE3I F4 (B2, brief phase): `[ai] mode = "session"` clusters the open gaps into regions, gives each region its own
session board and brief, and returns "packet". Real board and real session_begin (kicad-cli DRC); gap_packet is stubbed
because it renders the packet PNGs, which is not under test here."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
from fluxplace import gaps as GP  # noqa: E402
from test_planetie import make  # noqa: E402


def _gap(gid, net, at):
    return {"id": gid, "kind": "open-gap", "class": "plain", "nets": [net], "at": at}


GAPS = [_gap("a", "N1", [5, 5]), _gap("b", "N2", [7, 5]),          # one corner: the boxes touch, one region
        _gap("c", "N3", [35, 25]), _gap("d", "N4", [33, 25])]      # the other corner


def test_two_regions_two_briefs_two_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(GP, "gap_packet", lambda base, g, ctx, sid=None: {"png": []})
    board = make(str(tmp_path / "in.kicad_pcb"))
    ai = tmp_path / "ai"
    ctx = {"ai_dir": str(ai), "ai_used": {}, "cons": {"ai": {"packets": 4}}, "ai_mode": "session"}
    out = str(tmp_path / "out.kicad_pcb")
    r = GP._r_ai(board, out, GAPS, ctx)
    assert r["status"] == "packet" and r["n"] == 2 and len(r["briefs"]) == 2 and not os.path.exists(out)
    regs = json.load(open(ai / "sessions.json"))
    assert {frozenset(x["gaps"]) for x in regs} == {frozenset({"a", "b"}), frozenset({"c", "d"})}
    assert set(regs[0]["nets"]).isdisjoint(regs[1]["nets"])
    assert len({x["session"] for x in regs}) == 2 and all(os.path.exists(x["session"]) for x in regs)
    brief = json.load(open(regs[0]["brief"]))
    assert brief["v"] == 1 and brief["session"] == regs[0]["session"] and brief["ledger"].endswith("ledger.txt")
    assert {g["id"] for g in brief["gaps"]} == set(regs[0]["gaps"])
    # a second call: every gap is inside an open region, so no duplicate region
    assert GP._r_ai(board, out, GAPS, ctx) == {"status": "no packet", "n": 0}
    assert len(json.load(open(ai / "sessions.json"))) == 2


def test_open_regions_get_packet_rows_and_done_files_are_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(GP, "gap_packet", lambda base, g, ctx, sid=None: {"png": []})
    board = make(str(tmp_path / "in.kicad_pcb"))
    ai = tmp_path / "ai"
    ctx = {"ai_dir": str(ai), "ai_used": {}, "cons": {"ai": {"packets": 4}}, "ai_mode": "session"}
    GP._r_ai(board, str(tmp_path / "out.kicad_pcb"), GAPS, ctx)
    regs = json.load(open(ai / "sessions.json"))
    rows = [x for x in GP._packet_rows(str(tmp_path), GAPS, {}, 2) if x.get("kind") == "session"]
    assert sorted(x["region"] for x in rows) == sorted(x["rid"] for x in regs)
    assert GP._ai_pending_names(str(ai), GAPS) == []
    (ai / f"{regs[0]['rid']}.done.json").write_text("{}")
    assert GP._ai_pending_names(str(ai), [GAPS[0]]) == [f"{regs[0]['rid']}.done.json"]
    assert GP._ai_pending_names(str(ai), [GAPS[2]]) == []


# ---- PHASE3M review fixes: merge dir reuse (H2), refused merge charge (L2), unreadable done file (L1) ----
def _merge_ctx(tmp_path):
    ai = tmp_path / "ai"
    ai.mkdir()
    gap = {"id": "g1", "kind": "open-gap", "nets": ["A"], "at": [1, 1]}
    ctx = {"ai_dir": str(ai), "ai_used": {}, "cons": {}}
    return ai, gap, ctx


def _empty_ledger(tmp_path):
    p = tmp_path / "empty-ledger.txt"
    p.write_text("")
    return str(p)


def test_h2_reused_merge_dir_drops_stale_m_files(tmp_path):
    from fluxplace import gaps as GP
    ai, gap, ctx = _merge_ctx(tmp_path)
    reg = {"rid": "r1", "gaps": ["g1"], "status": "open"}
    (ai / "r1.done.json").write_text(json.dumps({"ledger": _empty_ledger(tmp_path)}))
    mdir = ai / "merge" / "r1"
    mdir.mkdir(parents=True)
    (mdir / "m5.kicad_pcb").write_text("stale")
    base = tmp_path / "base.kicad_pcb"
    base.write_text("base")
    GP._r_ai_merge(str(base), str(tmp_path / "out.kicad_pcb"), [gap], ctx, [reg], reg)
    assert not (mdir / "m5.kicad_pcb").exists()
    assert (mdir / "m0.kicad_pcb").exists()


def test_l1_unreadable_done_file_refuses_the_region_and_does_not_raise(tmp_path):
    from fluxplace import gaps as GP
    ai, gap, ctx = _merge_ctx(tmp_path)
    reg = {"rid": "r2", "gaps": ["g1"], "status": "open"}
    done = ai / "r2.done.json"
    done.write_text(json.dumps({"ledger": str(tmp_path / "missing.txt")}))
    base = tmp_path / "base.kicad_pcb"
    base.write_text("base")
    r = GP._r_ai_merge(str(base), str(tmp_path / "out.kicad_pcb"), [gap], ctx, [reg], reg)
    assert r["status"] == "refused" and reg["status"] == "refused"
    assert not done.exists()                                  # consumed, never re-read


def test_l2_refused_merge_charges_one_ai_try_per_gap(tmp_path):
    from fluxplace import gaps as GP
    ai, gap, ctx = _merge_ctx(tmp_path)
    reg = {"rid": "r3", "gaps": ["g1"], "status": "open"}
    (ai / "r3.done.json").write_text(json.dumps({"ledger": _empty_ledger(tmp_path)}))
    base = tmp_path / "base.kicad_pcb"
    base.write_text("base")
    r = GP._r_ai_merge(str(base), str(tmp_path / "out.kicad_pcb"), [gap], ctx, [reg], reg)
    assert r["status"] == "refused"
    assert ctx["ai_used"][GP.ai_key(gap)] == 1
