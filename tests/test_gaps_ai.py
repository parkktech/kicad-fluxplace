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

PKT_KEYS = {"v", "gap", "conn", "window", "sealed_by", "krt", "tried", "actions"}
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
    assert set(p) == PKT_KEYS and p["conn"] is None and p["window"] is None and p["v"] == 1
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
    assert GP.load_ai_action(str(bad), "b") == {"actions": [], "tokens": 0, "why": ""}


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
    assert os.listdir(ctx["ai_dir"]) == [critroute._safe(GP.ai_key(GAP)) + ".action.used.json"]
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


def test_ai_budget_after_two_applied(apply_env, monkeypatch, tmp_path):
    ctx = _ctx(tmp_path, ai_used={GP.ai_key(GAP): 2})
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
    ctx = _ctx(tmp_path)
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
