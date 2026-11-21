import os
"""gaps.py phase 2b: AI packet, action file load and apply, budgets, KRT json-out, packets stop. Fakes only."""
import hashlib, json, os, sys
from collections import Counter
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import types
import functools
from fluxplace import gaps as GP, critroute, handroute as HR, routediag as RD, routable as RB, legality as LGL
from fluxplace import boardwrite as BW, routerloc as RL, routerules as RR, adaptive, batchroute as BR
_close = functools.partial(GP.close, inproc=True)

PKT_KEYS = {"v", "gap", "board", "conn", "window", "corridors", "png", "sealed_by", "krt", "tried", "feedback", "actions"}
GAP = {"id": "open-free:SIG2:1:2", "kind": "open-free", "class": "plain", "nets": ["SIG2"]}
FREE = {"description": "Missing connection", "items": [
    {"description": "Pad 2 [SIG2]", "pos": {"x": 20, "y": 20}}, {"description": "Pad 3 [SIG2]", "pos": {"x": 25, "y": 20}}]}


def _measure(board, kicad_cli="kicad-cli"):
    n = 0 if "fixed" in open(board).read() else 1
    d = {"violations": [], "unconnected_items": [FREE] * n}
    return {"drc": d, "opens": n, "shorts": 0, "errors": 0, "err_keys": Counter()}


def _fp(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


def _same(base, out, gaps, ctx):
    import shutil
    shutil.copy(base, out)
    return {"mode": "same"}


def _ctx(tmp_path, **kw):
    c = {"cons": {}, "cons_path": None, "work": str(tmp_path), "ai_dir": str(tmp_path / "ai"), "ai_used": {},
         "ai_mode": "packet", "budget_s": 60, "attempts": str(tmp_path / "att.jsonl")}
    c.update(kw)
    return c


def _conn(net="SIG2", x0=20.0, x1=25.0):
    end = lambda x, pid: {"kind": "pad", "id": pid, "uuid": pid, "x": x, "y": 20.0, "layer": "F.Cu", "pin": "R1.1",
                          "text": "Pad"}
    return {"net": net, "a": end(x0, "aaaa1111"), "b": end(x1, "bbbb2222"), "straight_mm": 5.0,
            "rules": {"class": "plain", "width": 0.2}}


# ---- packet ----

def test_packet_no_connection_keys_and_size(monkeypatch, tmp_path):
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    ctx = _ctx(tmp_path)
    os.makedirs(ctx["ai_dir"])
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"), [GAP], ctx)
    assert res == {"status": "packet", "n": 1} and "rip_locked" in json.loads(open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP)) + ".packet.json")).read())["actions"]
    pk = os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP)) + ".packet.json")
    raw = open(pk, "rb").read()
    p = json.loads(raw)
    assert set(p) == PKT_KEYS and p["conn"] is None and p["window"] is None and p["v"] == 2 and p["png"] == []
    assert len(raw) <= 4096 and p["gap"]["net"] == "SIG2"


def test_packet_clips_to_4096(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": [_conn()]})
    tracks = [{"id": f"{i:064d}", "net": "SIG2", "layer": "F.Cu", "locked": False,
               "a": [20.0 + i * 0.01, 20.0], "b": [21.0, 20.0]} for i in range(60)]
    vias = [{"id": f"{i:064d}", "net": "SIG2", "pos": [22.0, 20.0 + i * 0.01], "locked": True} for i in range(60)]
    win = {"window": [0, 0, 30, 30], "layers": ["F.Cu"], "tracks": tracks, "vias": vias,
           "via_sites": {"sites": [[1.2345, 2.3456]] * 20}}
    monkeypatch.setattr(HR, "window", lambda *a, **k: win)
    lim = [{"id": "x" * 40, "net": "GND", "layer": "F.Cu", "locked": True, "border_cells": 3, "item": "P"}] * 12
    monkeypatch.setattr(HR, "suggest", lambda *a, **k: {"found": False, "sealed_by": {"limiting_obstacles": lim}})
    krt = tmp_path / "k.krt.json"
    krt.write_text(json.dumps({"failed_single": [{"net": "SIG2"}, {"net": "GND"}], "open_single": [{"net": "SIG2"}]}))
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, _ctx(tmp_path, krt_last=str(krt)))
    assert set(p) == PKT_KEYS and p["conn"]["i"] == 0 and p["conn"]["a"]["pin"] == "R1.1"
    assert len(json.dumps(p).encode()) <= 4096
    assert len(p["window"]["tracks"]) < 60 and len(p["window"]["via_sites"]) == 10
    assert p["krt"] == {"failed": [], "open": []}          # oversized: krt is dropped first
    assert len(p["sealed_by"]) == 8 and p["tried"] == []


def test_packet_krt_filtered_to_net(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": [_conn()]})
    monkeypatch.setattr(HR, "window", lambda *a, **k: {"window": [0, 0, 1, 1], "layers": ["F.Cu"], "tracks": [],
                                                       "vias": [], "via_sites": {"sites": []}})
    monkeypatch.setattr(HR, "suggest", lambda *a, **k: {"found": False, "sealed_by": {"limiting_obstacles": []}})
    krt = tmp_path / "k.krt.json"
    krt.write_text(json.dumps({"failed_single": [{"net": "SIG2"}, {"net": "GND"}], "open_single": [{"net": "SIG2"}]}))
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, _ctx(tmp_path, krt_last=str(krt)))
    assert p["krt"]["failed"] == [{"net": "SIG2"}] and p["krt"]["open"] == [{"net": "SIG2"}]


def test_packet_tried_lines_for_this_gap(monkeypatch, tmp_path):
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    att = tmp_path / "att.jsonl"
    rows = [{"gap": {"id": GAP["id"]}, "method": "patch", "result": "refused", "why": "no change"},
            {"gap": {"id": "other"}, "method": "krt", "result": "refused", "why": "x"}]
    att.write_text("".join(json.dumps(r) + "\n" for r in rows))
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, _ctx(tmp_path, attempts=str(att)))
    assert p["tried"] == [{"method": "patch", "result": "refused", "why": "no change"}]


# ---- action file ----

def test_load_ai_action_drops_invalid(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu", "B.Cu"]); monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    acts = [{"do": "zap"}, {"do": "rip", "net": "N", "ids": []}, {"do": "rip", "net": "N", "ids": ["zz"]},
            {"do": "nudge", "ref": "R1", "dx": 2.0, "dy": 0, "rot": 0},
            {"do": "nudge", "ref": "R1", "dx": 0.5, "dy": 0, "rot": 0},
            {"do": "route", "nets": ["N"], "layers": ["In1.Cu"]},
            {"do": "route", "nets": ["N"], "layers": ["F.Cu"]},
            {"do": "rip", "net": "N", "ids": ["abcdef12"]}, {"do": "give_up", "why": "no room"}]
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"actions": acts, "tokens": 812, "why": "try"}))
    a = GP.load_ai_action(str(f), str(tmp_path / "b.kicad_pcb"))
    assert [x["do"] for x in a["actions"]] == ["nudge", "route", "rip", "give_up"]
    assert a["tokens"] == 812 and a["why"] == "try"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert GP.load_ai_action(str(bad), "b") == {"actions": [], "tokens": 0, "why": "", "dropped": []}


# ---- apply ----

@pytest.fixture
def apply_env(monkeypatch, tmp_path):
    calls = []
    state = {"laid": False}
    parts = {"R1": {"pos": (1.0, 2.0), "pads": [1, 2], "angle": 90.0, "locked": False},
             "U1": {"pos": (5.0, 5.0), "pads": [1, 2, 3, 4], "angle": 0.0, "locked": False}}
    monkeypatch.setattr(RD, "load", lambda p, **k: {"parts": parts})
    monkeypatch.setattr(RB, "_fixed_set", lambda B, cons: set(state.get("fixed", [])))

    def move(src, dst, moves, post=True):
        calls.append(("move", sorted(moves)))
        open(dst, "w").write("moved")
        return list(moves)
    monkeypatch.setattr(BW, "move_file", move)

    class Legal:
        def __init__(self, B, cons=None, fixed=()):
            pass

        def part_issues(self, ref, **k):
            calls.append(("legal", ref))
            return []
    monkeypatch.setattr(LGL, "Legal", Legal)
    monkeypatch.setattr(HR, "session_begin", lambda b, w, *a, **k: calls.append(("begin", os.path.basename(w))))
    monkeypatch.setattr(HR, "shove", lambda b, net, **k: calls.append(("shove", net, k.get("op"), k.get("ids"),
                                                                       k.get("allow_locked", False))))
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])

    def krt_cmd(b, out, nets, ctx, layers=None):
        calls.append(("krtcmd", list(nets)))
        return ["krt", "--nets", "SIG2"]
    monkeypatch.setattr(GP, "_krt_cmd", krt_cmd)

    def run(cmd, **k):
        calls.append(("krt", cmd[0]))
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(GP.subprocess, "run", run)

    def open_nets(b, **k):
        conns = [] if state["laid"] else [_conn()]
        return {"connections": conns}
    monkeypatch.setattr(HR, "open_nets", open_nets)
    monkeypatch.setattr(HR, "suggest", lambda b, net, **k: (calls.append(("suggest", net, k.get("conn"))),
                                                           {"found": True, "path": "P 1 2"})[1])

    def lay(b, net, path, **k):
        calls.append(("lay", net, path))
        state["laid"] = True
        return {"accepted": True}                   # handroute.lay reports its verdict in the result
    monkeypatch.setattr(HR, "lay", lay)
    monkeypatch.setattr(HR, "session_commit",
                        lambda w, out, **k: (calls.append(("commit",)), {"accepted": True, "reasons": []})[1])
    return {"calls": calls, "state": state, "tmp": tmp_path}


def _write_action(ai_dir, acts):
    os.makedirs(ai_dir, exist_ok=True)
    p = os.path.join(ai_dir, critroute._safe(GP.ai_key(GAP)) + ".action.json")
    with open(p, "w") as fh:
        json.dump({"actions": acts, "tokens": 40, "why": "reroute"}, fh)
    return p


def test_apply_order_and_temps(apply_env, monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu", "B.Cu"]); monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [
        {"do": "nudge", "ref": "R1", "dx": 0.5, "dy": 0.0, "rot": 10.0},
        {"do": "rip", "net": "SIG9", "ids": ["abcdef12"]},
        {"do": "route", "nets": ["SIG2"], "layers": None}])
    out = str(tmp_path / "out.kicad_pcb")
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), out, [GAP], ctx)
    assert res == {"status": "applied", "commit": True, "why": "", "tokens": 40, "gap_id": GAP["id"]}
    c = apply_env["calls"]
    order = [e[0] for e in c]
    assert order.index("move") < order.index("begin") < order.index("shove") < order.index("krt") \
        < order.index("suggest") < order.index("lay") < order.index("commit")
    assert ("shove", "SIG9", "unlay", ["abcdef12"], False) in c
    assert ("move", [("R1")]) == c[0] or c[0][0] == "move"
    assert sorted(f for f in os.listdir(ctx["ai_dir"]) if not f.endswith(".feedback.json")) == [
        critroute._safe(GP.ai_key(GAP)) + ".action.used.1.json"]
    assert ctx["ai_used"] == {GP.ai_key(GAP): 1}


def test_nudge_refused_on_fixed_or_big_part(apply_env, monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu", "B.Cu"]); monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    apply_env["state"]["fixed"] = ["R1"]
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "nudge", "ref": "R1", "dx": 0.5, "dy": 0.0, "rot": 0.0},
                                  {"do": "nudge", "ref": "U1", "dx": 0.5, "dy": 0.0, "rot": 0.0}])
    GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    assert not [e for e in apply_env["calls"] if e[0] == "move"]


def test_give_up_skips_session(apply_env, monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu"]); monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "give_up", "why": "no room"}])
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    assert res["status"] == "give_up" and res["why"] == "no room" and apply_env["calls"] == []


def test_ai_budget_after_four_applied(apply_env, monkeypatch, tmp_path):
    ctx = _ctx(tmp_path, ai_used={GP.ai_key(GAP): 4})
    os.makedirs(ctx["ai_dir"])
    open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP)) + ".action.json"), "w").write("{}")
    assert GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx) == {"status": "ai budget"}
    assert apply_env["calls"] == []


def test_ai_off_writes_no_packet(tmp_path):
    ctx = _ctx(tmp_path, ai_mode="off")
    assert GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx) == {"status": "ai off"}
    assert not os.path.exists(ctx["ai_dir"])


# ---- KRT json-out ----

def test_krt_json_out_before_nets(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setattr(RL, "resolve", lambda *a, **k: {"py": "/usr/bin/python3", "dir": str(tmp_path / "krt")})
    monkeypatch.setattr(RR, "rules", lambda cons, board=None, default=None, **k: {"track": 0.2})
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    monkeypatch.setattr(RR, "flags", lambda R, d, w, script="route.py": [])
    monkeypatch.setattr(adaptive, "krt_extra", lambda d, s: [])
    monkeypatch.setattr(BR, "has_fanout", lambda src: False)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        js = cmd[cmd.index("--json-out") + 1]
        open(js, "w").write(json.dumps({"failed_single": [{"net": "N"}], "open_single": []}))
        open(cmd[cmd.index("--output") + 1], "w").write("routed")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    opens = iter([{"N"}, set()])
    monkeypatch.setattr(GP, "_krt_open", lambda b, cp: next(opens))
    monkeypatch.setattr(GP, "_krt_rip", lambda b, net, cp, skip: [])
    ctx = {"cons": {}, "work": str(tmp_path), "budget_s": 60}
    out = tmp_path / "out.kicad_pcb"
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(out), [dict(GAP, nets=["N"])], ctx)
    cmd = seen["cmd"]
    assert cmd.index("--json-out") < cmd.index("--nets")
    assert res == {"status": "ran", "rc": 0, "failed": 1, "open": 0, "closed": 1, "ripped": 0}
    assert ctx["krt_last"] == str(out) + ".krt0.json" and os.path.exists(ctx["krt_last"])


# ---- close loop ----

@pytest.fixture
def close_env(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "measure", _measure)
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    monkeypatch.setattr(GP.audit, "snapshot", lambda *a, **k: {})
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    for r in GP.DISPATCH["open-free"]:
        if r != "ai":
            monkeypatch.setitem(GP.RUNGS, r, _same)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("original")
    return {"board": str(board), "work": str(tmp_path / "work"), "attempts": str(tmp_path / "att.jsonl")}


def test_close_packets_stop_exit5_and_not_memoised(close_env):
    cons = {"ai": {"mode": "packet"}}
    res = _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons=cons,
                   log=lambda m: None)
    assert res["stop"] == "packets" and res["exit"] == 5
    assert json.load(open(os.path.join(close_env["work"], "stop.json")))["exit"] == 5
    rows = [json.loads(x) for x in open(close_env["attempts"])]
    assert rows[-1]["method"] == "ai" and rows[-1]["result"] == "packet"
    import glob
    pk = glob.glob(os.path.join(close_env["work"], "ai", "*.packet.json"))
    assert len(pk) == 1
    os.remove(pk[0])
    _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons=cons, log=lambda m: None)
    assert os.path.exists(pk[0])


def test_close_ai_off_flat_not_packets(close_env):
    res = _close(close_env["board"], close_env["work"], attempts=close_env["attempts"],
                   cons={"ai": {"mode": "off"}}, log=lambda m: None)
    assert res["stop"] == "flat" and res["exit"] == 2
    assert not os.path.exists(os.path.join(close_env["work"], "ai"))


# ---- rip_locked, multi-gap _r_ai (phase 2b.1) ----

def _gap(net):
    return {"id": f"open-free:{net}:1:2", "kind": "open-free", "class": "plain", "nets": [net]}


def test_rip_locked_protected_net_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"actions": [
        {"do": "rip_locked", "net": "CLK", "ids": ["abcdef12"]},
        {"do": "rip_locked", "net": "SIG9", "ids": ["abcdef12"]},
        {"do": "rip_locked", "net": "SIG9", "ids": []}]}))
    a = GP.load_ai_action(str(f), "b", protect={"CLK"})
    assert a["actions"] == [{"do": "rip_locked", "net": "SIG9", "ids": ["abcdef12"]}]


def test_rip_locked_plain_net_shoves_allow_locked_and_routes(apply_env, monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "rip_locked", "net": "SIG9", "ids": ["abcdef12"]}])
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    assert res["status"] == "applied" and res["commit"] is True
    c = apply_env["calls"]
    assert ("shove", "SIG9", "unlay", ["abcdef12"], True) in c
    assert ("krtcmd", ["SIG9"]) in c and c.index(("krtcmd", ["SIG9"])) > c.index(("shove", "SIG9", "unlay", ["abcdef12"], True))


def test_rip_locked_net_appended_to_last_route(apply_env, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "rip_locked", "net": "SIG9", "ids": ["abcdef12"]},
                                  {"do": "route", "nets": ["SIG2"], "layers": None}])
    GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    c = apply_env["calls"]
    assert ("krtcmd", ["SIG2"]) in c and ("krtcmd", ["SIG9"]) in c


def test_packets_for_three_gaps_one_call(monkeypatch, tmp_path):
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    ctx = _ctx(tmp_path)
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"),
                   [_gap("N1"), _gap("N2"), _gap("N3")], ctx)
    assert res == {"status": "packet", "n": 3}
    names = sorted(os.listdir(ctx["ai_dir"]))
    assert names == sorted(critroute._safe(GP.ai_key(_gap(n))) + ".packet.json" for n in ("N1", "N2", "N3"))


def test_packets_capped_at_six_per_call(monkeypatch, tmp_path):
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    ctx = _ctx(tmp_path, cons={"ai": {"packets": 6}})
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"),
                   [_gap(f"N{i}") for i in range(7)], ctx)
    assert res == {"status": "packet", "n": 6} and len(os.listdir(ctx["ai_dir"])) == 6


def test_existing_action_for_second_gap_applied(apply_env, tmp_path):
    ctx = _ctx(tmp_path)
    os.makedirs(ctx["ai_dir"])
    g2 = _gap("N2")
    with open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(g2)) + ".action.json"), "w") as fh:
        json.dump({"actions": [{"do": "route", "nets": ["N2"], "layers": None}], "tokens": 7, "why": "x"}, fh)
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"),
                   [_gap("N1"), g2, _gap("N3")], ctx)
    assert res == {"status": "applied", "commit": True, "why": "", "tokens": 7, "gap_id": g2["id"]}
    assert ("krtcmd", ["N2"]) in apply_env["calls"]
    assert ctx["ai_used"] == {GP.ai_key(g2): 1}
    assert not [f for f in os.listdir(ctx["ai_dir"]) if f.endswith(".packet.json")]


# ---- PHASE3F B: packet pictures, new action kinds, feedback, budget, packet handoff ----

def _win_fake(calls, fail_png=False):
    def win(board, **k):
        calls.append(k)
        if k.get("png") and fail_png:
            raise RuntimeError("render boom")
        W = {"window": [0, 0, 1, 1], "layers": ["F.Cu"], "tracks": [], "vias": [], "via_sites": {"sites": []},
             "connection_view": {"a": {"item": "R1.1", "free_component_by_layer": {"F.Cu": 1}},
                                 "b": {"item": "R1.2", "free_component_by_layer": {"F.Cu": 1, "B.Cu": 2}},
                                 "direct_corridor_layers": ["F.Cu"]},
             "free_corridors": {"F.Cu": [{"id": 1, "max_track_mm": 0.3, "area_mm2": 2.0}]}}
        if k.get("png"):
            open(k["png"], "wb").write(b"png")
            W["images"] = [k["png"]]
        return W
    return win


def test_packet_pictures_pocket_and_corridors(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": [_conn()]})
    calls = []
    monkeypatch.setattr(HR, "window", _win_fake(calls))
    monkeypatch.setattr(HR, "suggest", lambda *a, **k: {"found": False, "sealed_by": {"limiting_obstacles": []}})
    ctx = _ctx(tmp_path)
    os.makedirs(ctx["ai_dir"])
    sid = critroute._safe(GP.ai_key(GAP))[:80]
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, ctx, sid=sid)
    assert set(p) == PKT_KEYS and p["v"] == 2 and p["board"] == str(tmp_path / "in.kicad_pcb")
    assert p["png"] == [os.path.join(ctx["ai_dir"], sid + ".conn.png"), os.path.join(ctx["ai_dir"], sid + ".pocket.png")]
    assert all(os.path.exists(x) for x in p["png"])
    pocket = next(k for k in calls if k.get("size") == (4, 4))
    assert pocket["at"] == (20.0, 20.0)                  # end a has less free room (one layer, not two)
    assert p["corridors"] == {"direct": ["F.Cu"], "max_track_mm": {"F.Cu": 0.3}}
    assert len(json.dumps(p).encode()) <= 4096


def test_packet_render_failure_still_written(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": [_conn()]})
    monkeypatch.setattr(HR, "window", _win_fake([], fail_png=True))
    monkeypatch.setattr(HR, "suggest", lambda *a, **k: {"found": False, "sealed_by": {"limiting_obstacles": []}})
    ctx = _ctx(tmp_path)
    os.makedirs(ctx["ai_dir"])
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, ctx, sid="s1")
    assert p["png"] == [] and p["window"]["box"] == [0, 0, 1, 1]


def test_packet_pair_block_for_pair_gap(monkeypatch, tmp_path):
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    pg = {"id": "pair-open:USB_P:USB_N", "kind": "pair-open", "class": "hs-pair", "nets": ["USB_P", "USB_N"]}
    cons = {"pairs": {"USB": {"impedance_diff": 90, "skew_mm": 0.15}}}
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), pg, _ctx(tmp_path, cons=cons))
    assert p["pair"]["p"] == "USB_P" and p["pair"]["n"] == "USB_N" and p["pair"]["family"] == "USB"
    assert p["pair"]["skew_limit_mm"] == 0.15 and p["pair"]["skew_mm"] is None and p["pair"]["class"] == "hs-pair"
    assert "pair" not in GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, _ctx(tmp_path))


def test_ai_action_new_kinds_validated(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    acts = [
        {"do": "path", "net": "SIG2", "path": "F.Cu: 1,2 3,4", "lock": True},          # ok
        {"do": "path", "net": "SIG2", "path": "F.Cu: 1,2 3,4; via 5"},                  # bad via token
        {"do": "path", "net": "LEG", "path": "F.Cu: 1,2 3,4"},                           # hs-pair leg
        {"do": "path", "net": "SIG2", "path": "F.Cu: " + "1,2 3,4 " * 120},            # > 800 chars
        {"do": "pair_path", "pair": ["P", "N"], "layer": "F.Cu", "waypoints": [[1, 2], [3, 4]]},   # ok
        {"do": "pair_path", "pair": ["P", "N"], "layer": "F.Cu", "waypoints": [[1, 2]]},            # 1 point
        {"do": "pair_path", "pair": ["P", "N"], "layer": "In9.Cu", "waypoints": [[1, 2], [3, 4]]},  # not a signal layer
        {"do": "swap_pins", "ref": "U1", "pads": ["1", "2"], "why": "x"},               # ok
        {"do": "swap_pins", "ref": "U1", "pads": ["1", "2", "3"]},                      # 3 pads
    ]
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"actions": acts}))
    a = GP.load_ai_action(str(f), "b", hs_legs={"LEG"})
    assert [x["do"] for x in a["actions"]] == ["path", "pair_path", "swap_pins"]
    assert a["actions"][0] == {"do": "path", "net": "SIG2", "path": "F.Cu: 1,2 3,4", "lock": True}


def test_ai_action_path_hs_leg_stub_join():
    cu = ["F.Cu", "B.Cu"]
    stub = GP._ai_action({"do": "path", "net": "LEG", "path": "F.Cu: 0,0 0.8,0"}, cu, hs_legs={"LEG"})[0]
    assert stub == {"do": "path", "net": "LEG", "path": "F.Cu: 0,0 0.8,0", "lock": False}      # 0.8 mm: kept
    assert GP._ai_action({"do": "path", "net": "LEG", "path": "F.Cu: 0,0 5,0"}, cu, hs_legs={"LEG"})[0] is None  # 5 mm: dropped
    assert GP._ai_action({"do": "path", "net": "SIG2", "path": "F.Cu: 0,0 5,0"}, cu, hs_legs={"LEG"})[0]["net"] == "SIG2"


def test_ai_action_file_capped_at_eight(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu"])
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"actions": [{"do": "give_up", "why": str(i)} for i in range(10)]}))
    assert len(GP.load_ai_action(str(f), "b")["actions"]) == GP.AI_ACTIONS_MAX == 8


def test_path_and_pair_path_order_and_refusal_keeps_transaction(apply_env, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [
        {"do": "pair_path", "pair": ["P", "N"], "layer": "F.Cu", "waypoints": [[1, 2], [3, 4]]},
        {"do": "path", "net": "BAD", "path": "F.Cu: 1,2 3,4", "lock": True},
        {"do": "path", "net": "SIG2", "path": "F.Cu: 1,2 3,4"},
        {"do": "route", "nets": ["SIG9"], "layers": None}])
    real_lay = HR.lay

    def lay(b, net, path, **k):
        if net == "BAD":
            raise ValueError("probe failed: clearance")
        return real_lay(b, net, path, **k)
    HR.lay = lay
    try:
        res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    finally:
        HR.lay = real_lay
    assert res["status"] == "applied" and res["commit"] is True
    c = apply_env["calls"]
    assert ("lay", "SIG2", "F.Cu: 1,2 3,4") in c and ("commit",) in c
    assert c.index(("lay", "SIG2", "F.Cu: 1,2 3,4")) < c.index(("krtcmd", ["SIG9"])) < c.index(("commit",))
    fb = json.load(open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP))[:80] + ".feedback.json")))
    assert [(x["do"], x["ok"]) for x in fb["actions"]] == [("path", False), ("path", True), ("pair_path", False)]
    assert fb["actions"][2]["why"].startswith("no pair geometry") and fb["commit"] is True   # PHASE3G G1: real builder, no stackup here


def test_swap_pins_writes_proposal_only(apply_env, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "swap_pins", "ref": "U1", "pads": ["1", "2"], "why": "crossed"}])
    GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    sw = json.load(open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP))[:80] + ".swap.json")))
    assert sw["gap_id"] == GAP["id"] and sw["action"]["ref"] == "U1"
    assert not [e for e in apply_env["calls"] if e[0] in ("lay", "move", "shove")]


def test_refused_path_feedback_reaches_next_packet(apply_env, monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "path", "net": "SIG2", "path": "F.Cu: 1,2 3,4"}])
    monkeypatch.setattr(HR, "lay", lambda b, net, path, **k: (_ for _ in ()).throw(ValueError("probe failed")))
    monkeypatch.setattr(HR, "probe", lambda b, net, path, **k: {"ok": False, "violations": [
        {"kind": "clearance", "net": "GND", "with": "track", "margin": 0.04, "at": [2.0, 3.0], "x": 1},
        {"kind": "via_below_floor", "need": [0.6, 0.3], "have": [0.5, 0.2]}]})
    GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    sid = critroute._safe(GP.ai_key(GAP))[:80]
    fb = json.load(open(os.path.join(ctx["ai_dir"], sid + ".feedback.json")))
    assert fb["actions"][0]["ok"] is False and fb["actions"][0]["probe"][0] == {"kind": "clearance", "net": "GND",
                                                                              "with": "track", "margin": 0.04, "at": [2.0, 3.0]}
    assert len(json.dumps(fb).encode()) <= GP.FEEDBACK_BYTES
    monkeypatch.setattr(HR, "open_nets", lambda *a, **k: {"connections": []})
    p = GP.gap_packet(str(tmp_path / "in.kicad_pcb"), GAP, ctx, sid=sid)
    assert p["feedback"]["actions"][0]["why"] == "probe failed"


def test_ai_budget_override_from_constraints(monkeypatch, tmp_path):
    assert GP._ai_budget({}) == 4 and GP._ai_budget({"ai": {"budget": 2}}) == 2
    ctx = _ctx(tmp_path, ai_used={GP.ai_key(GAP): 2}, cons={"ai": {"budget": 2}})
    os.makedirs(ctx["ai_dir"])
    open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP)) + ".action.json"), "w").write("{}")
    assert GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx) == {"status": "ai budget"}


def test_close_stop_lists_packets_then_second_close_applies(close_env, apply_env, monkeypatch):
    cons = {"ai": {"mode": "packet"}}
    monkeypatch.setattr(HR, "window", _win_fake([]))
    res = _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons=cons, log=lambda m: None)
    assert res["exit"] == 5 and len(res["packets"]) == 1
    row = res["packets"][0]
    sid = critroute._safe(GP.ai_key(GAP))[:80]
    assert row["gap_id"] == res["left"][0]["id"] and row["budget_left"] == 4
    assert row["action"] == os.path.join(close_env["work"], "ai", sid + ".action.json") and len(row["png"]) == 2
    assert json.load(open(os.path.join(close_env["work"], "stop.json")))["packets"] == res["packets"]
    _write_action(os.path.join(close_env["work"], "ai"), [{"do": "route", "nets": ["SIG2"], "layers": None}])
    res2 = _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons=cons, log=lambda m: None)
    gs = json.load(open(os.path.join(close_env["work"], "gaps.json")))
    assert gs["ai_used"] == {GP.ai_key(GAP): 1}
    assert os.path.exists(os.path.join(close_env["work"], "ai", sid + ".action.used.1.json"))
    assert ("krtcmd", ["SIG2"]) in apply_env["calls"] and res2["stop"] in ("packets", "flat", "clean")


# ---- PHASE3G G1: pair gaps reach the ai rung; pair actions are filtered; one packet key per pair -----------------
def test_pair_gaps_dispatch_to_ai_after_pairgroup():
    for k in ("pair-open", "pair-asym", "pair-skew"):
        assert GP.DISPATCH[k] == ["pairgroup", "ai"]


def test_ai_key_of_pair_gap_is_the_pair():
    g = {"id": "pair-skew:P/N", "kind": "pair-skew", "nets": ["P", "N"]}
    assert GP.ai_key(g) == "pair:P/N"
    assert GP.ai_key({"kind": "open-free", "nets": ["SIG2"]}) == "open-free:SIG2"


def test_hs_pair_gap_drops_route_keeps_path_and_pair_path(tmp_path, monkeypatch):
    from fluxplace import critroute
    gap = {"id": "pair-skew:P/N", "kind": "pair-skew", "class": "hs-pair", "nets": ["P", "N"]}
    ctx = _ctx(tmp_path)
    sid = critroute._safe(GP.ai_key(gap))[:80]
    act = tmp_path / "ai" / (sid + ".action.json")
    act.parent.mkdir(parents=True, exist_ok=True)
    act.write_text("{}")                                  # the file is read through load_ai_action (patched below)
    seen = {}
    monkeypatch.setattr(GP, "load_ai_action", lambda *a, **k: {"actions": [
        {"do": "path", "net": "P", "path": "F.Cu: 1,1 2,1", "lock": False},
        {"do": "route", "nets": ["N"], "layers": None},
        {"do": "pair_path", "pair": ["P", "N"], "layer": "F.Cu", "waypoints": [[1, 1], [1, 5]]},
        {"do": "give_up", "why": "x"}], "tokens": 0, "why": ""})
    def fake_apply(base, out, a, c, sid):
        seen["acts"] = [x["do"] for x in a["actions"]]
        return "applied", True, ""
    monkeypatch.setattr(GP, "_apply_ai", fake_apply)
    GP._r_ai(str(tmp_path / "b.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [gap], ctx)
    assert seen["acts"] == ["path", "pair_path", "give_up"]


# ---- PHASE3G G1b a/b: layer-change waypoints validated, refusal detail in the feedback row ----------------------------
def test_pair_path_layer_changes_validated(monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    ok3 = [[1, 2, "F.Cu"], [1, 6, "B.Cu"], [1, 9, "F.Cu"], [1, 12]]                # two changes, runs 4 / 3 / 3 mm
    alt = [[0, i, "F.Cu" if i % 2 == 0 else "B.Cu"] for i in range(7)]           # a change at every point: 6 > 4
    short = [[0, 0, "F.Cu"], [0, 0.5, "B.Cu"], [0, 1.0, "F.Cu"], [0, 3.0]]           # a 0.5 mm run
    last_change = [[0, 0], [0, 3], [0, 6, "B.Cu"]]                                   # a change on the last point
    bad_layer = [[0, 0], [0, 3, "In9.Cu"], [0, 6]]
    acts = [{"do": "pair_path", "pair": ["P", "N"], "layer": "F.Cu", "waypoints": w}
            for w in (ok3, alt, short, last_change, bad_layer)]
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"actions": acts}))
    got = GP.load_ai_action(str(f), "b")["actions"]
    assert [x["waypoints"] for x in got] == [ok3]


def test_pair_path_refusal_detail_reaches_feedback_row(apply_env, tmp_path, monkeypatch):
    from fluxplace import pairsonly as PS
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "pair_path", "pair": ["P", "N"], "layer": "F.Cu", "waypoints": [[1, 2], [3, 4]]}])
    detail = {"laid": False, "why": "P centre line 0.05 mm from via X (exact check)", "p_mm": None, "n_mm": None,
              "at": [1.2, 3.4], "blocker": {"label": "via X", "net": "X", "layer": "F.Cu"}, "stripped": None}
    monkeypatch.setattr(PS, "lay_centreline", lambda *a, **k: detail)
    GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    fb = json.load(open(os.path.join(ctx["ai_dir"], critroute._safe(GP.ai_key(GAP))[:80] + ".feedback.json")))
    row = fb["actions"][-1]
    assert row["do"] == "pair_path" and row["ok"] is False and row["at"] == [1.2, 3.4]
    assert row["blocker"] == {"label": "via X", "net": "X", "layer": "F.Cu"} and "stripped" in row


def test_ai_pending_names_sorted_existing_only(tmp_path):
    g1 = {"id": "open-free:SIG2:1:2", "kind": "open-free", "class": "plain", "nets": ["SIG2"]}
    g2 = {"id": "open-free:AAA:1:2", "kind": "open-free", "class": "plain", "nets": ["AAA"]}
    g3 = {"id": "open-free:ZZZ:1:2", "kind": "open-free", "class": "plain", "nets": ["ZZZ"]}
    for g in (g1, g2):
        open(os.path.join(tmp_path, GP.critroute._safe(GP.ai_key(g))[:80] + ".action.json"), "w").write("{}")
    assert GP._ai_pending_names(str(tmp_path), [g1, g3, g2]) == sorted(
        [GP.critroute._safe(GP.ai_key(g1))[:80] + ".action.json", GP.critroute._safe(GP.ai_key(g2))[:80] + ".action.json"])
    assert GP._ai_pending_names(str(tmp_path), [g3]) == []
    assert GP._ai_pending(str(tmp_path), [g3]) is False
    assert GP._ai_pending(str(tmp_path), [g1]) is True


def test_ai_filter_pair_hs_and_low_speed():
    hs = {"id": "pair-open:USB_P:USB_N", "kind": "pair-open", "class": "hs-pair", "nets": ["USB_P", "USB_N"]}
    lo = {"id": "pair-open:I2C_P:I2C_N", "kind": "pair-open", "class": "pair", "nets": ["I2C_P", "I2C_N"]}
    acts = [{"do": "route"}, {"do": "path"}, {"do": "pair_path"}, {"do": "rip"}, {"do": "nudge"}, {"do": "give_up"}]
    assert [x["do"] for x in GP._ai_filter_pair(hs, acts)] == ["path", "pair_path", "rip", "give_up"]
    assert [x["do"] for x in GP._ai_filter_pair(lo, acts)] == [x["do"] for x in acts]


# ---- PHASE3H Part A: loop, loader, budget (audit 1-14) ----

def _free(n):
    return {"description": "Missing connection", "items": [
        {"description": f"Pad 2 [{n}]", "pos": {"x": 20, "y": 20}}, {"description": f"Pad 3 [{n}]", "pos": {"x": 25, "y": 20}}]}


def _measure3(board, kicad_cli="kicad-cli"):
    text = open(board).read()
    items = [_free(n) for n in ("SIG2", "SIG3", "SIG4") if f"closed:{n}" not in text]
    return {"drc": {"violations": [], "unconnected_items": items}, "opens": len(items), "shorts": 0, "errors": 0,
            "err_keys": Counter()}


def _sid(key):
    return critroute._safe(key)[:80]


def test_a1_pair_leg_dropped_and_least_used_first(apply_env, monkeypatch, tmp_path):
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu", "B.Cu"]); monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(HR, "session_commit", lambda w, out, **k: {"accepted": False, "reasons": ["R2 new DRC key"]})
    monkeypatch.setattr(GP, "gap_packet", lambda base, gap, ctx, sid=None: {"gap": gap["id"]})
    nets = [f"N{i}" for i in range(30)]
    pair = {"id": "pair-skew:N3/N31", "kind": "pair-skew", "class": "pair", "nets": ["N3", "N31"]}
    gaps = [_gap(n) for n in nets] + [pair]
    ctx = _ctx(tmp_path, pair_gap_legs=["N3", "N31"])
    ai = os.path.join(ctx["ai_dir"])
    os.makedirs(ai)
    for n in ("N0", "N1", "N2", "N4", "N5", "N6"):           # six answers; each call applies one, refused by the guard
        json.dump({"actions": [{"do": "route", "nets": [n], "layers": None}]}, open(os.path.join(ai, _sid(f"open-free:{n}") + ".action.json"), "w"))
    for _ in range(6):
        res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), gaps, ctx)
        assert res["status"] == "applied" and res["commit"] is False
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), gaps, ctx)
    assert res == {"status": "packet", "n": 24}
    packets = {f for f in os.listdir(ai) if f.endswith(".packet.json")}
    # least used first: N7..N29 (unused) and the pair N3/N31 fill the 24; the answered N0..N6 wait; N3 (a pair leg) has no open-free packet
    want = {_sid(f"open-free:N{i}") + ".packet.json" for i in range(7, 30)} | {_sid("pair:N3/N31") + ".packet.json"}
    assert packets == want
    assert _sid("open-free:N3") + ".packet.json" not in packets


def test_a2_accept_passes_no_targets_when_one_lost_sev(monkeypatch):
    def gap(i, kind, sev, nets):
        return {"id": i, "kind": kind, "class": "pair", "sev": sev, "nets": nets}
    base_m = {"opens": 2, "shorts": 0, "errors": 0, "err_keys": Counter(), "keys": Counter(),
              "opens_by_net": {"P1": 1, "Q1": 1}, "gaps": [gap("A", "pair-open", 5, ["P1", "P2"]), gap("B", "pair-open", 5, ["Q1", "Q2"])]}
    cand_gaps = [gap("A", "pair-open", 5, ["P1", "P2"]), gap("B", "pair-asym", 3, ["Q1", "Q2"])]
    cand_m = {"opens": 1, "shorts": 0, "errors": 0, "err_keys": Counter(), "keys": Counter(),
              "opens_by_net": {"P1": 1, "Q1": 0}, "gaps": cand_gaps}
    assert GP._accept(base_m, None, cand_m, None, ["A", "B"], cand_gaps, "pair-open") == (True, "")


def test_a3_pending_answers_drained_before_any_rung(close_env, monkeypatch):
    from fluxplace import opengate as OG
    import glob
    monkeypatch.setattr(GP, "measure", _measure3)
    monkeypatch.setattr(OG, "refill", lambda p: None)
    calls = []

    def fake_call(name, base, out, gaps, ctx):
        calls.append(name)
        if name != "ai":
            return {}
        ai_dir = ctx["ai_dir"]
        pend = sorted(f for f in os.listdir(ai_dir) if f.endswith(".action.json")) if os.path.isdir(ai_dir) else []
        if not pend:
            return {"status": "packet", "n": 0}
        p = os.path.join(ai_dir, pend[0])
        close = json.load(open(p))["close"]
        os.replace(p, p[:-len(".json")] + ".used.1.json")
        open(out, "w").write(open(base).read() + "".join(f"closed:{n}" for n in close))
        return {"status": "applied"}
    monkeypatch.setattr(GP, "_call_rung", fake_call)
    ai_dir = os.path.join(close_env["work"], "ai")
    os.makedirs(ai_dir)
    for i, n in enumerate(["SIG2", "SIG3", "SIG4"]):      # the first (SIG2) closes its gap; the other two are refused
        json.dump({"close": ["SIG2"] if i == 0 else ["SIG9"]},
                  open(os.path.join(ai_dir, _sid(f"open-free:{n}") + ".action.json"), "w"))
    _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons={"ai": {"mode": "packet"}},
           log=lambda m: None)
    assert calls[:3] == ["ai", "ai", "ai"]                  # all three answers before any non-ai rung
    assert "prune" in calls and calls.index("prune") > 2
    assert glob.glob(os.path.join(ai_dir, "*.action.json")) == []


def test_a4_invalid_answer_applies_nothing(apply_env, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "rip", "net": "SIG9", "ids": ["abcdef12"]},
                                  {"do": "route", "nets": [f"N{i}" for i in range(7)], "layers": None}])
    out = str(tmp_path / "o.kicad_pcb")
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), out, [GAP], ctx)
    assert res["status"] == "invalid" and res["commit"] is False
    assert apply_env["calls"] == [] and not os.path.exists(out)
    fb = json.load(open(os.path.join(ctx["ai_dir"], _sid(GP.ai_key(GAP)) + ".feedback.json")))
    assert "route: nets > 6" in json.dumps(fb["dropped"]) and fb["refused"] == "action invalid"
    assert ctx["ai_used"] == {}


def test_a5_packet_text_states_every_limit():
    t = GP._PACKET_ACTIONS
    for v in (GP.AI_ROUTE_NETS, GP.AI_RIP_IDS, GP.AI_NUDGE_MM, GP.AI_MAX_ACTIONS, GP.AI_PAIR_WPTS):
        assert str(v) in t
    for phrase in ('{"actions":[...],"why","tokens"}', "signal layers", "one net per rip", "needs rot", "<= 3 pads",
                   "not fixed", "nudge -> rip -> path -> pair_path -> route", 'path "via x,y"'):
        assert phrase in t
    assert len(json.dumps(t)) < 1500


def test_a6_interrupt_leaves_answer_pending(apply_env, monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    p = _write_action(ctx["ai_dir"], [{"do": "route", "nets": ["SIG2"], "layers": None}])

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(GP, "_apply_ai", boom)
    with pytest.raises(KeyboardInterrupt):
        GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    assert os.path.exists(p) and ctx["ai_used"] == {}


def test_a6_error_consumes_answer_uncharged(apply_env, monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    p = _write_action(ctx["ai_dir"], [{"do": "route", "nets": ["SIG2"], "layers": None}])

    def boom(*a, **k):
        raise RuntimeError("router died")
    monkeypatch.setattr(GP, "_apply_ai", boom)
    res = GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)
    assert res["status"] == "error" and "router died" in res["why"]
    assert not os.path.exists(p) and os.path.exists(p[:-len(".json")] + ".used.1.json")
    assert ctx["ai_used"] == {}


def test_a7_refused_answer_gives_packets_stop_with_feedback(close_env, apply_env, monkeypatch):
    monkeypatch.setattr(HR, "window", _win_fake([]))
    monkeypatch.setattr(HR, "session_commit", lambda w, out, **k: {"accepted": False, "reasons": ["R2 new DRC key"]})
    ai_dir = os.path.join(close_env["work"], "ai")
    _write_action(ai_dir, [{"do": "route", "nets": ["SIG2"], "layers": None}])
    open(os.path.join(ai_dir, "zz.action.json"), "w").write("{}")
    res = _close(close_env["board"], close_env["work"], attempts=close_env["attempts"],
                 cons={"ai": {"mode": "packet"}}, log=lambda m: None)
    assert res["stop"] == "packets" and res["exit"] == 5
    assert res["ignored"] == [{"file": os.path.join(ai_dir, "zz.action.json"), "reason": "no such gap"}]
    sid = _sid(GP.ai_key(GAP))
    assert json.load(open(os.path.join(ai_dir, sid + ".feedback.json")))["commit"] is False
    assert json.load(open(os.path.join(ai_dir, sid + ".packet.json")))["feedback"]["commit"] is False


def test_a8_answer_rewritten_on_same_board_is_applied(close_env, apply_env, monkeypatch):
    calls = []

    def commit(w, out, **k):
        calls.append(1)
        return {"accepted": False, "reasons": ["R2 new DRC key"]}
    monkeypatch.setattr(HR, "session_commit", commit)
    monkeypatch.setattr(HR, "window", _win_fake([]))
    ai_dir = os.path.join(close_env["work"], "ai")
    cons = {"ai": {"mode": "packet"}}
    _write_action(ai_dir, [{"do": "route", "nets": ["SIG2"], "layers": None}])
    _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons=cons, log=lambda m: None)
    assert len(calls) == 1
    _write_action(ai_dir, [{"do": "route", "nets": ["SIG2"], "layers": None}])
    _close(close_env["board"], close_env["work"], attempts=close_env["attempts"], cons=cons, log=lambda m: None)
    assert len(calls) == 2 and os.path.exists(os.path.join(ai_dir, _sid(GP.ai_key(GAP)) + ".action.used.2.json"))


def test_a9_give_up_not_charged_and_over_budget_feedback(apply_env, tmp_path):
    ctx = _ctx(tmp_path)
    _write_action(ctx["ai_dir"], [{"do": "give_up", "why": "no room"}])
    assert GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [GAP], ctx)["status"] == "give_up"
    assert ctx["ai_used"] == {}
    ctx2 = _ctx(tmp_path / "b", ai_used={GP.ai_key(GAP): 4})
    _write_action(ctx2["ai_dir"], [{"do": "route", "nets": ["SIG2"], "layers": None}])
    assert GP._r_ai(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o2.kicad_pcb"), [GAP], ctx2) == {"status": "ai budget"}
    fb = json.load(open(os.path.join(ctx2["ai_dir"], _sid(GP.ai_key(GAP)) + ".feedback.json")))
    assert fb["refused"] == "ai budget 4/4"
    assert apply_env["calls"] == []
