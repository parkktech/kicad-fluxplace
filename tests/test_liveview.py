"""liveview.py (F16): progress lines, placement snapshots, the GLB exporter with a fake kicad-cli, feedback items, gates."""
import json
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import events, geom as GM, liveview as LV, modules as MD, routediag as RD


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (events.ENV_FILE, events.ENV_RUN, events.ENV_STAGE, LV.ENV_OPS):
        monkeypatch.delenv(k, raising=False)
    LV._last.clear()
    LV._snap.update(seq=0, lanes={})
    yield


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def _on(tmp_path, monkeypatch, ops=False):
    W = tmp_path / "W"
    events.init(W, run="t")
    if ops:
        monkeypatch.setenv(LV.ENV_OPS, str(tmp_path / "ops.jsonl"))
    return W


def _board(tmp):
    sig = [f"S{i}" for i in range(8)]
    body = PB.column_ic("U1", 40, 40, ["VX"] + sig[1:])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    body += PB.fp("J1", 80, 20, [PB.pad(str(i + 1), 0, i * 2.0, n, 1.2, 1.2) for i, n in enumerate(sig)])
    body += PB.fp("U2", 60, 40, [PB.pad(str(i + 1), 0, i * 1.0, n, 0.6, 0.6) for i, n in enumerate(["Q0", "GND", "S0", "S1", "S2", "S3"])])
    path = PB.write(tmp, body)
    B = RD.load(path)
    return path, B, MD.build(B, {}, None, arrange=False)


# ---------------------------------------------------------------- L1
def test_stream_off_writes_nothing_and_never_raises(tmp_path):
    assert LV.progress("floorplan", "anneal", trial=1) is None
    assert LV.progress(None, None, key=object()) is None
    assert list(tmp_path.iterdir()) == []


def test_progress_line_has_every_field_and_is_an_ops_line(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch, ops=True)
    f = LV.progress("floorplan", "anneal", generation=2, generations=None, trial=3, trials=8, arm="unguided", cand="g2-t3", seed=7,
                    key=[["hard rows", 0], ["sum over max", 12.5], ["ghost opens", None]], leader="g1-t5", is_leader=False, rows_rev=0,
                    criteria=[{"name": "C1 -> U1.1", "kind": "support", "verdict": "fail", "value": 30.0, "target": 3.0, "max": 5.0, "unit": "mm"},
                              {"id": "sealed", "ok": None, "value": None, "limit": 0, "text": "sealed pins"}], elapsed_s=4.2, bogus=1)
    ev, ops = _lines(W / "events.jsonl"), _lines(tmp_path / "ops.jsonl")
    assert len(ev) == 1 and len(ops) == 1
    line = ev[0]
    assert line["kind"] == "placement_progress" and line["stage"] == "floorplan" and "bogus" not in line
    assert all(k in line for k in LV.PROGRESS_KEYS)
    assert all(k in ops[0] for k in ("v", "ts", "kind", "text"))
    assert line["text"] == "floorplan anneal gen 2 trial 3/8 unguided g2-t3 hard rows 0 sum over max 12.5 running"
    assert line["key"][2] == ["ghost opens", None] and line["verdict"] == "running"
    assert line["criteria"] == [{"id": "C1 -> U1.1", "ok": False, "value": 30.0, "limit": 5.0, "text": "support C1 -> U1.1: 30.0 / 5.0 mm"},
                                {"id": "sealed", "ok": None, "value": None, "limit": 0, "text": "sealed pins"}]
    assert f["trial"] == 3


def test_progress_is_limited_to_one_per_two_seconds_but_always_lines_pass(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    for i in range(100):
        LV.progress("floorplan", "anneal", trial=i)
    LV.progress("floorplan", "anneal", always=True, verdict="kept", cand="a")
    LV.progress("floorplan", "anneal", always=True, verdict="dropped", cand="b", why="worse: power")
    LV.progress("placetour", "judge", trial=1)                 # another stage has its own limit
    ev = _lines(W / "events.jsonl")
    assert [(e["stage"], e["verdict"]) for e in ev] == [("floorplan", "running"), ("floorplan", "kept"), ("floorplan", "dropped"), ("placetour", "running")]


def test_progress_over_the_line_cap_falls_back_to_the_truncated_form(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    LV.progress("floorplan", "anneal", criteria=[{"id": f"row{i}", "ok": True, "value": i, "limit": i, "text": "x" * 300} for i in range(400)])
    line = _lines(W / "events.jsonl")[0]
    assert line["truncated"] is True and line["kind"] == "placement_progress"


# ---------------------------------------------------------------- L2
def test_grid_names_cells_from_the_origin():
    g = LV.grid_of((100.0, 50.0, 200.0, 95.0))
    assert g == {"cols": 8, "rows": 4, "cell": [12.5, 11.25], "origin": [100.0, 50.0]}
    assert LV.cell_rect(g, "A1") == (100.0, 50.0, 112.5, 61.25)
    assert LV.cell_rect(g, "C2:D3") == (125.0, 61.25, 150.0, 83.75)
    assert LV.cell_rect(g, "I1") is None and LV.cell_rect(g, "A5") is None and LV.cell_rect(g, "??") is None
    assert LV.cells_of(g, (126.0, 62.0, 149.0, 83.0)) == "C2:D3" and LV.cells_of(g, (101.0, 51.0, 102.0, 52.0)) == "A1"


def test_snapshot_delta_lists_exactly_the_moved_module(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    meta = {"cand": "c1", "arm": "unguided", "generation": 0, "key": [["hard rows", 1]], "rows_rev": 0}
    assert LV.place_snapshot(B, "leader", meta, M) == "live/place/leader-000001.json"
    full = json.load(open(W / "live/place/leader-000001.json"))
    assert full["tracks"] == [] and full["vias"] == [] and len(full["parts"]) == 4 and len(full["pads"]) > 0
    assert set(full) >= {"v", "seq", "ts", "board", "bbox", "edge", "layers", "parts", "pads", "zones", "tracks", "vias", "grid", "modules",
                         "regions", "keepouts", "key", "criteria", "cand", "arm", "generation", "rows_rev"}
    u1 = next(p for p in full["parts"] if p["ref"] == "U1")
    assert set(u1) >= {"ref", "x", "y", "rot", "side", "crt", "value", "mod", "fixed", "h"} and u1["mod"] == "U1" and u1["fixed"] is None
    assert {m["id"]: m["n"] for m in full["modules"]} == {"J1": 1, "U1": 2, "U2": 1}
    for r in ("U1", "C1"):                                     # module U1 moves 3 mm in x
        p = B["parts"][r]
        GM.place(p, p["pos"][0] + 3.0, p["pos"][1], p["angle"])
    assert LV.place_snapshot(B, "leader", dict(meta, cand="c2"), M) == "live/place/leader-000002.delta.json"
    d = json.load(open(W / "live/place/leader-000002.delta.json"))
    assert d["base"] == 1 and "pads" not in d and "regions" not in d
    assert sorted(d["moved"], key=lambda m: m["ref"]) == [{"ref": "C1", "dx": 3.0, "dy": 0.0, "drot": 0.0, "flip": False},
                                                          {"ref": "U1", "dx": 3.0, "dy": 0.0, "drot": 0.0, "flip": False}]
    assert sorted(p["ref"] for p in d["parts"]) == ["C1", "U1"] and [m["id"] for m in d["modules"]] == ["U1"]
    p = B["parts"]["U2"]
    GM.place(p, p["pos"][0] + 0.005, p["pos"][1], p["angle"])  # under 0.01 mm: not a move
    LV.place_snapshot(B, "leader", meta, M)
    d = json.load(open(W / "live/place/leader-000003.delta.json"))
    assert sorted(m["ref"] for m in d["moved"]) == ["C1", "U1"]
    ev = [e for e in _lines(W / "events.jsonl") if e["kind"] == "placement_snapshot"]
    assert [(e["lane"], e["seq"], e["full"], e["moved"], e["base"]) for e in ev] == [("leader", 1, True, 0, None), ("leader", 2, False, 2, 1), ("leader", 3, False, 2, 1)]
    assert not [n for n in os.listdir(W / "live/place") if n.endswith(".tmp")]


def test_snapshot_of_a_model_equals_the_snapshot_of_the_board_written_from_it(tmp_path, monkeypatch):
    from fluxplace import boardwrite as BW
    _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    moves = {}
    for r in ("U1", "C1"):
        p = B["parts"][r]
        rx, ry = GM.rot(p["pos"][0] - 40.0, p["pos"][1] - 40.0, 90.0)
        moves[r] = (50.0 + rx, 30.0 + ry, p["angle"] + 90.0)
        GM.place(p, *moves[r])
    out = str(tmp_path / "moved.kicad_pcb")
    BW.move_file(path, out, moves, post=False)
    a, b = LV.snapshot_dict(B, 1, M), LV.snapshot_dict(RD.load(out), 1, M)
    assert a["parts"] == b["parts"] and a["modules"] == b["modules"]


def test_full_every_20_lanes_are_separate_and_the_trial_lane_is_throttled(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    for _ in range(23):
        LV.place_snapshot(B, "leader", None, M)
    names = sorted(os.listdir(W / "live/place"))
    assert [n for n in names if not n.endswith(".delta.json")] == ["leader-000001.json", "leader-000022.json"]
    assert LV.place_snapshot(B, "trial", None, M) == "live/place/trial-000024.json"
    assert LV.place_snapshot(B, "trial", None, M) is None                       # inside 5 s
    assert LV.place_snapshot(B, "trial", None, M, full=True) == "live/place/trial-000025.json"   # the stage end is never throttled
    assert LV.place_snapshot(B, "nolane", None, M) is None


def test_a_snapshot_failure_is_one_logged_row(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    assert LV.place_snapshot({"broken": True}, "leader") is None
    rows = _lines(W / "live/errors.jsonl")
    assert len(rows) == 1 and rows[0]["where"] == "place_snapshot"


# ---------------------------------------------------------------- L3
def _fake_cli(tmp_path, body):
    """A stand-in for kicad-cli: `-o OUT` is argv[-2], the board argv[-1]."""
    p = tmp_path / "fake-kicad-cli"
    p.write_text("#!/bin/bash\nout=\"${@: -2:1}\"\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def _wait(ex, secs=20.0):
    t0 = time.time()
    while (ex.run or ex.wait) and time.time() - t0 < secs:
        ex.poll()
        time.sleep(0.05)


def _glb_events(W):
    return [e for e in _lines(W / "events.jsonl") if e["kind"].startswith("glb_")]


def test_export_writes_the_file_by_rename_counts_missing_models_and_points_latest_at_it(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    cli = _fake_cli(tmp_path, 'echo "File not found: a.step"; echo "File not found: b.step"; echo "$@" > "$out.args"; printf glTF > "$out"')
    cons_path = tmp_path / "in" / "constraints.toml"
    ex = LV.Exporter({"live": {"model_base": "../models", "glb_min_s": 0}}, cons_path, cli=cli, stage="floorplan")
    t0 = time.time()
    assert ex.request("cand/1", "leader", path, {"C1": (10.0, 10.0, 0.0)}, snapshot=4) is True
    assert time.time() - t0 < 1.0
    _wait(ex)
    ev = _glb_events(W)
    assert len(ev) == 1 and ev[0]["kind"] == "glb_ready"
    assert (ev[0]["path"], ev[0]["cand"], ev[0]["why"], ev[0]["snapshot"], ev[0]["bytes"], ev[0]["missing"], ev[0]["models"]) == \
        ("live/glb/cand_1.glb", "cand/1", "leader", 4, 4, 2, 0)
    assert sorted(os.listdir(W / "live/glb")) == ["cand_1.glb", "cand_1.glb.tmp.args", "cand_1.log", "latest.json"]
    assert json.load(open(W / "live/glb/latest.json"))["path"] == "live/glb/cand_1.glb"
    args = open(W / "live/glb/cand_1.glb.tmp.args").read().split()
    assert args[:6] == ["pcb", "export", "glb", "-f", "--subst-models", "--no-dnp"] and args[6:8] == ["-D", "KIPRJMOD=" + str(tmp_path / "models")]


def test_three_leaders_give_one_running_and_the_newest_waiting(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    ex = LV.Exporter({"live": {"glb_min_s": 0}}, cli=_fake_cli(tmp_path, 'sleep 0.5; printf glTF > "$out"'))
    for c in ("a", "b", "c"):
        ex.request(c, "leader", path)
    assert ex.run["cand"] == "a" and [j["cand"] for j in ex.wait] == ["c"]
    ex.request("keep", "accepted", path)                       # an accepted candidate waits its turn and is never replaced
    ex.request("d", "leader", path)
    assert [j["cand"] for j in ex.wait] == ["keep", "d"]
    _wait(ex)
    assert [e["cand"] for e in _glb_events(W) if e["kind"] == "glb_ready"] == ["a", "keep", "d"]


def test_leader_interval_is_five_times_the_measured_time(tmp_path, monkeypatch):
    _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    now = [1000.0]
    ex = LV.Exporter({"live": {"glb_min_s": 60}}, cli=_fake_cli(tmp_path, 'printf glTF > "$out"'), clock=lambda: now[0])
    ex.request("a", "leader", path)
    while ex.run["p"].poll() is None:
        time.sleep(0.05)
    ex.poll()
    ex.last_secs = 20.0
    ex.request("b", "leader", path)
    now[0] += 99.0
    ex.poll()
    assert ex.run is None and [j["cand"] for j in ex.wait] == ["b"]            # 99 s < max(60, 5 x 20)
    now[0] += 2.0
    ex.poll()
    assert ex.run["cand"] == "b"
    ex.close()


def test_a_hung_export_is_killed_at_the_timeout_and_leaves_no_process(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    ex = LV.Exporter({"live": {"glb_min_s": 0}}, cli=_fake_cli(tmp_path, "sleep 300"), timeout=0.5)
    ex.request("hang", "leader", path)
    pgid = ex.run["p"].pid
    time.sleep(0.8)
    ex.poll()
    assert ex.run is None
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    ev = _glb_events(W)
    assert [(e["kind"], e["cand"], e["why"]) for e in ev] == [("glb_failed", "hang", "timeout after 0.5 s")]
    assert sorted(os.listdir(W / "live/glb")) == []


def test_a_failed_export_and_an_absent_tool_are_one_row_each(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    ex = LV.Exporter({"live": {"glb_min_s": 0}}, cli=_fake_cli(tmp_path, "exit 1"))
    ex.request("bad", "leader", path)
    _wait(ex)
    ex2 = LV.Exporter({"live": {}}, cli=str(tmp_path / "no-such-tool"))
    assert ex2.request("x", "leader", path) is False and ex2.request("y", "leader", path) is False
    ev = _glb_events(W)
    assert [(e["kind"], e["cand"]) for e in ev] == [("glb_failed", "bad"), ("glb_failed", "x")]
    assert ev[0]["why"] == "fake-kicad-cli exit 1" and "not found" in ev[1]["why"]


def test_close_drops_leaders_waits_for_an_accepted_export_and_old_leader_files_are_pruned(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    ex = LV.Exporter({"live": {"glb_min_s": 0, "glb_keep": 2}}, cli=_fake_cli(tmp_path, 'printf glTF > "$out"'))
    for c in ("l1", "l2", "l3"):
        ex.request(c, "leader", path)
        _wait(ex)
    assert sorted(n for n in os.listdir(W / "live/glb") if n.endswith(".glb")) == ["l2.glb", "l3.glb"]
    ex = LV.Exporter({"live": {"glb_min_s": 0}}, cli=_fake_cli(tmp_path, 'sleep 0.4; printf glTF > "$out"'))
    ex.request("lead", "leader", path)
    ex.request("final", "accepted", path)
    ex.close()
    assert ex.run is None and ex.wait == []
    ev = _glb_events(W)[3:]
    assert [(e["kind"], e["cand"], e["why"]) for e in ev] == [("glb_failed", "lead", "stage ended"), ("glb_ready", "final", "accepted")]


def test_stream_off_no_live_table_or_a_nested_run_the_view_does_nothing(tmp_path, monkeypatch):
    ex = LV.Exporter({"live": {}}, cli="/bin/true")
    assert ex.request("a", "leader", "/nonexistent.kicad_pcb") is False
    ex.poll()
    ex.close()
    assert list(tmp_path.iterdir()) == []
    W = _on(tmp_path, monkeypatch)
    assert LV.Exporter({}, cli="/bin/true").request("a", "leader", "/nonexistent.kicad_pcb") is False      # no [live] table: no 3D export
    monkeypatch.setenv("FLUXPLACE_NESTED", "1")
    assert LV.work() == "" and LV.progress("floorplan", "anneal", always=True) is None
    assert LV.Exporter({"live": {}}, cli="/bin/true").request("a", "leader", "/nonexistent.kicad_pcb") is False
    assert not (W / "events.jsonl").exists()


# ---------------------------------------------------------------- L4
def _ctx():
    return {"parts": {"U1", "C1", "J1"}, "modules": {"U1"}, "groups": {"cm5"}, "grid": LV.grid_of((0.0, 0.0, 100.0, 80.0)),
            "bbox": (0.0, 0.0, 100.0, 80.0), "gates": {"PLACE_OK": ["c1"], "arch-choice": ["a", "b"]}, "seen": {"seen0001"}}


def _item(**kw):
    return dict({"v": 1, "id": "ab12cd34", "ts": 1.0, "author": "owner"}, **kw)


GOOD = [
    _item(kind="note", text="too close to the antenna", target={"part": "U1"}, cand="c1", snapshot=3),
    _item(kind="pin", target={"part": "U1"}, pin={"at": [50.0, 40.0], "rot": 90, "side": "top"}),
    _item(kind="pin", target={"module": "U1"}, cand="c1", snapshot=3),
    _item(kind="region", region={"cells": "C2:D3", "keep": "in"}, target={"group": "cm5"}),
    _item(kind="region", region={"rect": [10, 10, 30, 30], "keep": "out"}),
    _item(kind="near", target={"part": "C1"}, other={"part": "U1"}, dist_mm=10),
    _item(kind="near", target={"part": "C1"}, other={"edge": "N"}),
    _item(kind="away", target={"module": "U1"}, other={"cell": "A1"}, dist_mm=5, scope="board"),
    _item(kind="gate", gate={"id": "PLACE_OK", "action": "approve"}),
    _item(kind="gate", gate={"id": "arch-choice", "action": "approve", "choice": "b"}),
    _item(kind="gate", gate={"id": "PLACE_OK", "action": "reject"}, text="the relay is in the way"),
]
BAD = [
    ([1, 2], "not an object"),
    (_item(kind="note", text="x", v=2), "v is not 1"),
    (_item(kind="note", text="x", colour="red"), "unknown key colour"),
    ({"v": 1, "ts": 1.0, "author": "owner", "kind": "note", "text": "x"}, "missing key id"),
    (_item(kind="shout", text="x"), "unknown kind shout"),
    (_item(kind="note", text="x", id="seen0001"), "duplicate id seen0001"),
    (_item(kind="note", text="x" * 401), "text over 400 characters"),
    (_item(kind="note"), "missing key text"),
    (_item(kind="note", text="x", scope="forever"), "scope must be run or board"),
    (_item(kind="pin", target={"part": "U99"}, pin={"at": [5, 5]}), "unknown part U99"),
    (_item(kind="pin", target={"part": "U1"}, pin={"at": [500, 5]}), "pin.at is off the board"),
    (_item(kind="pin", target={"part": "U1"}), "pin needs pin.at, or cand and snapshot"),
    (_item(kind="pin", target={"part": "U1"}, pin={"at": [5, 5], "depth": 1}), "unknown key in pin"),
    (_item(kind="pin", target={"group": "cm5"}, pin={"at": [5, 5]}), "pin needs target.part or target.module"),
    (_item(kind="region", region={"cells": "Z9", "keep": "out"}), "unknown cell Z9"),
    (_item(kind="region", region={"cells": "A1", "keep": "in"}), "region keep in needs a target"),
    (_item(kind="region", region={"rect": [10, 10, 300, 30], "keep": "out"}), "region.rect is off the board"),
    (_item(kind="region", region={"cells": "A1", "rect": [1, 1, 2, 2], "keep": "out"}), "region needs cells or rect"),
    (_item(kind="near", target={"part": "C1"}), "near needs target and other"),
    (_item(kind="near", target={"part": "C1"}, other={"module": "U7"}), "unknown module U7"),
    (_item(kind="away", target={"part": "C1"}, other={"edge": "Q"}), "other.edge must be one of N, E, S, W"),
    (_item(kind="away", target={"part": "C1"}, other={"part": "U1"}, dist_mm=0), "dist_mm must be over 0"),
    (_item(kind="gate", gate={"id": "NOPE", "action": "approve"}), "NOPE is not an open gate"),
    (_item(kind="gate", gate={"id": "arch-choice", "action": "approve", "choice": "z"}), "choice z is not a candidate of the gate"),
    (_item(kind="gate", gate={"id": "arch-choice", "action": "approve"}), "gate.choice is needed: the gate has more than one candidate"),
    (_item(kind="gate", gate={"id": "PLACE_OK", "action": "reject"}), "a reject needs text"),
]


def test_every_valid_kind_loads_and_reads_the_same_twice(tmp_path):
    for i, a in enumerate(GOOD):
        p = tmp_path / f"{i}.json"
        p.write_text(json.dumps(a))
        assert LV.load_feedback(str(p), _ctx()) == (a, None), a
        assert LV.load_feedback(str(p), _ctx()) == (a, None)


@pytest.mark.parametrize("item,reason", BAD, ids=[r for _a, r in BAD])
def test_an_item_is_refused_whole_with_its_reason(tmp_path, item, reason):
    p = tmp_path / "x.json"
    p.write_text(json.dumps(item))
    assert LV.load_feedback(str(p), _ctx()) == (None, reason)


def test_unreadable_oversize_and_tmp_files(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json")
    assert LV.load_feedback(str(p), _ctx()) == (None, "unreadable or not JSON (JSONDecodeError)")
    p.write_text(json.dumps(_item(kind="note", text="x", cand="c" * 17000)))
    assert LV.load_feedback(str(p), _ctx()) == (None, "file over 16000 bytes")
    t = tmp_path / "y.json.tmp"
    t.write_text("{")
    assert LV.load_feedback(str(t), _ctx()) == (None, None)
    assert LV.load_feedback(str(tmp_path / "missing.json"), _ctx())[0] is None


# ---------------------------------------------------------------- L5
def _fb(tmp_path, monkeypatch, items=(), cons=None, inbox=True):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    if inbox:
        os.makedirs(W / "feedback/inbox")
    for i, a in enumerate(items):
        (W / "feedback/inbox" / f"2026-{i:02d}-{a['id']}.json").write_text(json.dumps(a))
    return W, path, B, M, LV.Feedback(B, M, cons or {}, stage="floorplan", clock=lambda: 100.0)


def test_no_inbox_folder_means_no_feedback_and_no_file(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, inbox=False)
    cons = {"placement": {"fixed": ["J1"]}}
    assert fb.on() is False and fb.boundary(0) == [] and fb.merged(cons) is cons and fb.finish(B) == []
    assert not (W / "feedback").exists()


def test_items_become_rows_with_one_log_line_per_action(tmp_path, monkeypatch):
    items = [_item(id="00000001", kind="pin", target={"part": "U2"}, pin={"at": [70.0, 50.0]}, text="here"),
             _item(id="00000002", kind="region", region={"rect": [30, 30, 55, 50], "keep": "in"}, target={"module": "U1"}),
             _item(id="00000003", kind="near", target={"part": "C1"}, other={"part": "U1"}, dist_mm=10, scope="board"),
             _item(id="00000004", kind="away", target={"part": "U2"}, other={"part": "J1"}),
             _item(id="00000005", kind="note", text="looks crowded on the left"),
             _item(id="00000006", kind="pin", target={"part": "U9"}, pin={"at": [1, 1]})]
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, items)
    new = fb.boundary(generation=0)
    assert [(r["id"], r["kind"], r["hard"]) for r in new] == [("00000001", "fixed", True), ("00000002", "region", True), ("00000003", "near", True)]
    assert new[1]["refs"] == ["C1", "U1"] and new[2]["b"] == {"refs": ["U1"]}
    assert fb.rev == 3 and json.load(open(W / "feedback/rows.json"))["rows_rev"] == 3
    assert sorted(os.listdir(W / "feedback/inbox")) == [] and len(os.listdir(W / "feedback/applied")) == 5
    assert open(W / "feedback/refused/2026-05-00000006.json.reason.txt").read() == "unknown part U9\n"
    log = _lines(W / "feedback/log.jsonl")
    assert [(x["id"], x["action"]) for x in log] == [
        ("00000001", "received"), ("00000001", "applied"), ("00000002", "received"), ("00000002", "applied"), ("00000003", "received"),
        ("00000003", "applied"), ("00000004", "received"), ("00000004", "context"), ("00000005", "received"), ("00000005", "context"),
        ("2026-05-00000006.json", "refused")]
    assert log[1]["generation"] == 0 and log[1]["stage"] == "floorplan" and log[1]["rows_rev"] == 1
    assert len([e for e in _lines(W / "events.jsonl") if e["kind"] == "feedback_result"]) == 11
    assert fb.boundary(generation=1) == []                    # nothing new: rows_rev stays
    cons = {"placement": {"fixed": ["J1"]}}
    c = fb.merged(cons)
    assert cons == {"placement": {"fixed": ["J1"]}}             # the caller's constraints are never edited
    assert c["lock"]["at"] == {"U2": [70.0, 50.0, "F.Cu"]} and c["placement"]["fixed"] == ["J1", "U2"]
    assert c["placement"]["fixed_reason"]["U2"] == {"reason": "owner", "decision": "live feedback 00000001", "note": "here"}
    assert c["intent"]["region"] == [{"name": "owner-00000002", "rect": [30, 30, 55, 50], "keep": "in", "side": "both", "refs": ["C1", "U1"], "hard": True}]
    assert c["intent"]["near"] == [{"refs": ["C1"], "to": "U1", "max_mm": 10, "hard": True}]
    assert fb.pin_moves() == {"U2": (70.0, 50.0, 0.0)}
    table = fb.finish(B)                                       # U2 is still at 60, 40: the pin is not honoured; the others are
    acts = {(x["id"], x["action"]) for x in _lines(W / "feedback/log.jsonl")[11:]}
    assert acts == {("00000001", "not_honoured"), ("00000002", "honoured"), ("00000003", "honoured")}
    assert len(table) == 2 + 6 + 3 and "scope board" in table[-2] and "| 00000005 | note | context |" in table[6]


def test_conflicts_name_the_other_row(tmp_path, monkeypatch):
    items = [_item(id="00000001", kind="region", region={"rect": [0, 0, 20, 20], "keep": "out"}),
             _item(id="00000002", kind="pin", target={"part": "U2"}, pin={"at": [10.0, 10.0]}),
             _item(id="00000003", kind="pin", target={"part": "J1"}, pin={"at": [50.0, 50.0]}),
             _item(id="00000004", kind="away", target={"part": "C1"}, other={"part": "U2"}, dist_mm=20),
             _item(id="00000005", kind="near", target={"part": "C1"}, other={"part": "U2"}, dist_mm=5),
             _item(id="00000006", kind="region", region={"rect": [40, 40, 41, 41], "keep": "in"}, target={"part": "U1"})]
    cons = {"placement": {"fixed": ["J1"], "fixed_reason": {"J1": {"reason": "mechanical", "decision": "D1"}}}}
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, items, cons)
    assert [r["id"] for r in fb.boundary(0)] == ["00000001", "00000004"]
    why = {x["id"]: x["why"] for x in _lines(W / "feedback/log.jsonl") if x["action"] == "conflict"}
    assert why == {"00000002": "the pin is inside keep-out 00000001",
                   "00000003": "J1 is fixed for a mechanical reason: change the constraints, not the live run",
                   "00000005": "near 5 mm is under away 20 mm of row 00000004",
                   "00000006": "the region is under 1.5 x the courtyard area of its target (%s mm2)" % why_area(B)}
    assert len(os.listdir(W / "feedback/refused")) == 8        # 4 items + 4 reason files


def why_area(B):
    c = B["parts"]["U1"]["court"]
    return f"{(c[2] - c[0]) * (c[3] - c[1]):.1f}"


def test_a_rerun_with_the_same_applied_folder_applies_each_item_at_its_recorded_boundary(tmp_path, monkeypatch):
    items = [_item(id="00000001", kind="pin", target={"part": "U2"}, pin={"at": [70.0, 50.0]})]
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, items)
    assert [r["id"] for r in fb.boundary(generation=0)] == ["00000001"]     # the first run applies it at generation 0
    late = _item(id="00000002", kind="pin", target={"part": "C1"}, pin={"at": [20.0, 20.0]})
    (W / "feedback/inbox/2026-09-00000002.json").write_text(json.dumps(late))
    fb1 = LV.Feedback(B, M, {}, stage="floorplan", clock=lambda: 100.0)
    assert [r["id"] for r in fb1.boundary(generation=3)] == ["00000002"]
    fb2 = LV.Feedback(B, M, {}, stage="floorplan", clock=lambda: 100.0)     # the re-run: same applied/ folder, empty inbox
    assert [[r["id"] for r in fb2.boundary(generation=g)] for g in (0, 1, 2, 3)] == [["00000001"], [], [], ["00000002"]]
    assert [r["id"] for r in fb2.rows] == ["00000001", "00000002"] and fb2.rev == 2
    assert sorted(os.listdir(W / "feedback/applied")) == ["2026-00-00000001.json", "2026-09-00000002.json"]


# ---------------------------------------------------------------- L6
class Clock:
    def __init__(self, t=1000.0):
        self.t, self.sleeps = t, 0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1


CANDS = [{"cand": "c1", "key": [["hard rows", 0]], "criteria": [], "snapshot": 7}, {"cand": "c2", "key": [["hard rows", 0]], "criteria": [], "snapshot": 8}]


def test_no_live_gate_section_means_no_file_and_no_wait(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch)
    ck = Clock()
    r = LV.gate("PLACE_OK", "PASS", CANDS[:1], fb, {"live": {"glb_min_s": 60}}, clock=ck, sleep=ck.sleep)
    assert (r["passed"], r["by"], r["exit"], ck.sleeps) == (True, None, 0, 0)
    assert LV.gate("PLACE_OK", "FAIL", CANDS[:1], fb, {}, clock=ck, sleep=ck.sleep)["passed"] is False
    assert not (W / "feedback/gates").exists() and not (W / "events.jsonl").exists()


def test_a_gate_with_no_answer_closes_by_timeout_with_its_default(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch)
    ck = Clock()
    cons = {"live": {"gate": {"PLACE_OK": {"wait_min": 1}, "arch-choice": {"wait_min": 1}}}}
    r = LV.gate("PLACE_OK", "PASS", CANDS[:1], fb, cons, clock=ck, sleep=ck.sleep)
    assert (r["passed"], r["by"], r["action"], r["choice"], r["exit"], r["wait_s"], ck.sleeps) == (True, "timeout", "proceed", "c1", 0, 60.0, 30)
    g = json.load(open(W / "feedback/gates/PLACE_OK.json"))
    assert (g["deadline"], g["default"], g["machine"], g["by"], g["action"], g["gate_wait_s"]) == (1060.0, "proceed", "PASS", "timeout", "proceed", 60.0)
    r = LV.gate("arch-choice", None, CANDS, fb, cons, clock=ck, sleep=ck.sleep)     # no machine verdict: the default is stop
    assert (r["passed"], r["by"], r["action"], r["exit"]) == (False, "timeout", "stop", 5)
    kinds = [e["kind"] for e in _lines(W / "events.jsonl")]
    assert kinds == ["gate_open", "gate_closed", "feedback_result", "gate_open", "gate_closed", "feedback_result"]


def _answer(W, fb_item):
    (W / "feedback/inbox" / f"2026-{fb_item['id']}.json").write_text(json.dumps(fb_item))


def test_an_approve_closes_the_gate_at_the_next_poll_and_never_overrides_a_machine_fail(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch)
    cons = {"live": {"gate": {"PLACE_OK": {"wait_min": 10}}}}

    class Answering(Clock):
        def sleep(self, s):
            Clock.sleep(self, s)
            if self.sleeps == 3:
                _answer(W, _item(id="000000a1", kind="gate", gate={"id": "PLACE_OK", "action": "approve"}))
    ck = Answering()
    r = LV.gate("PLACE_OK", "PASS", CANDS[:1], fb, cons, clock=ck, sleep=ck.sleep)
    assert (r["passed"], r["by"], r["action"], r["wait_s"], ck.sleeps) == (True, "owner", "approve", 6.0, 3)
    os.remove(W / "feedback/gates/PLACE_OK.json")
    _answer(W, _item(id="000000a2", kind="gate", gate={"id": "PLACE_OK", "action": "approve"}))
    assert not fb.boundary(1) and (W / "feedback/refused/2026-000000a2.json").exists()     # no gate is open: refused
    ck = Clock()
    fb2 = LV.Feedback(B, M, {}, stage="placetour", clock=ck)
    _answer(W, _item(id="000000a3", kind="gate", gate={"id": "PLACE_OK", "action": "approve"}))
    r = LV.gate("PLACE_OK", "FAIL", CANDS[:1], fb2, cons, clock=ck, sleep=ck.sleep)        # an answer already in the inbox applies at once
    assert (r["passed"], r["by"], r["action"], ck.sleeps) == (False, "owner", "approve", 0)
    last = _lines(W / "feedback/log.jsonl")[-1]
    assert (last["action"], last["why"]) == ("gate_closed", "recorded only: the machine side failed")


def test_a_reject_returns_the_text_and_a_restart_keeps_the_deadline(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch)
    cons = {"live": {"gate": {"PLACE_OK": {"wait_min": 10, "default": "stop"}}}}

    class Killed(Clock):
        def sleep(self, s):
            Clock.sleep(self, s)
            if self.sleeps == 5:
                raise KeyboardInterrupt
    ck = Killed()
    with pytest.raises(KeyboardInterrupt):
        LV.gate("PLACE_OK", "PASS", CANDS[:1], fb, cons, clock=ck, sleep=ck.sleep)
    assert json.load(open(W / "feedback/gates/PLACE_OK.json"))["deadline"] == 1600.0

    class Rejecting(Clock):
        def sleep(self, s):
            Clock.sleep(self, s)
            if self.sleeps == 2:
                _answer(W, _item(id="000000b1", kind="gate", gate={"id": "PLACE_OK", "action": "reject"}, text="the relay is in the way"))
    ck = Rejecting(1500.0)                                      # restarted 500 s after the gate opened
    fb2 = LV.Feedback(B, M, {}, stage="placetour", clock=ck)
    r = LV.gate("PLACE_OK", "PASS", CANDS[:1], fb2, cons, clock=ck, sleep=ck.sleep)
    assert (r["passed"], r["by"], r["action"], r["text"], r["exit"]) == (False, "owner", "reject", "the relay is in the way", 0)
    g = json.load(open(W / "feedback/gates/PLACE_OK.json"))
    assert g["deadline"] == 1600.0 and g["opened"] == 1000.0 and g["item"] == "000000b1"
    assert len([e for e in _lines(W / "events.jsonl") if e["kind"] == "gate_open"]) == 1


def test_a_gate_with_wait_zero_is_shown_but_does_not_wait(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch)
    ck = Clock()
    r = LV.gate("PLACE_OK", "PASS", CANDS[:1], fb, {"live": {"gate": {"PLACE_OK": {}}}}, clock=ck, sleep=ck.sleep)
    assert (r["passed"], r["by"], ck.sleeps) == (True, None, 0)
    g = json.load(open(W / "feedback/gates/PLACE_OK.json"))
    assert g["deadline"] is None and "closed" not in g


# ---------------------------------------------------------------- L7
def _plan(tmp):
    from fluxplace import floorplan2 as F2
    path, B, M = _board(tmp)
    P = F2.Plan(B, {}, None, M, log=lambda *a, **k: None)
    P.set_claims({})
    P.freeze_baseline()
    return path, P, F2


def test_anneal_is_the_same_with_and_without_the_watcher_and_calls_it_once_per_250_moves(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _p, P1, F2 = _plan(tmp_path / "a")
    _p, P2, F2 = _plan(tmp_path / "b")
    calls = []
    r1 = F2.anneal(P1, iters=1000, seed=3)
    r2 = F2.anneal(P2, iters=1000, seed=3, watch=lambda i, n, cs: calls.append((i, n)))
    assert r1 == r2 and (P1.X == P2.X).all() and (P1.Y == P2.Y).all() and (P1.T == P2.T).all()
    assert calls == [(0, 1000), (250, 1000), (500, 1000), (750, 1000)] and F2.WATCH_EVERY == 250
    assert {r: (p["pos"], p["angle"]) for r, p in P1.B["parts"].items()} == {r: (p["pos"], p["angle"]) for r, p in P2.B["parts"].items()}


def test_watcher_and_candidate_write_lines_and_snapshots_and_are_off_without_a_stream(tmp_path, monkeypatch):
    path, P, F2 = _plan(tmp_path)
    assert LV.watcher("floorplan", "t", P) is None and LV.candidate("floorplan", "t", P.B) is None
    W = _on(tmp_path, monkeypatch, ops=True)
    watch = LV.watcher("floorplan", "g_0", P, {}, names=("cost",))
    watch(0, 1000, 12.3456)
    watch(250, 1000, 11.0)                                     # inside 2 s: no line, no snapshot
    M = {"modules": P.mods, "of": P.of}
    assert LV.candidate("floorplan", "g_0", P.B, M, [["fan-out blocked", 0]], [{"id": "fan-out", "ok": True, "value": 9, "limit": 9, "text": ""}], {},
                        generation=1, is_leader=True, leader="g_0") == "live/place/leader-000002.json"
    assert LV.candidate("floorplan", "result", path, None, None, None, {}, lane="accepted", phase="gate", full=True) == "live/place/accepted-000003.json"
    ev = [e for e in _lines(W / "events.jsonl") if e["kind"] != "narration"]     # narration: tests/test_narrate.py
    assert [(e["kind"], e.get("phase") or e.get("lane"), e.get("cand")) for e in ev] == [
        ("placement_progress", "anneal", "g_0"), ("placement_snapshot", "trial", "g_0"), ("placement_progress", "judge", "g_0"),
        ("placement_snapshot", "leader", "g_0"), ("placement_progress", "gate", "result"), ("placement_snapshot", "accepted", "result")]
    assert ev[0]["key"] == [["cost", 12.346]] and (ev[0]["trial"], ev[0]["trials"]) == (0, 1000)
    ops = [o for o in _lines(tmp_path / "ops.jsonl") if o["kind"] != "narration"]   # the ops feed gets every line (narration: test_narrate)
    assert [(o["kind"], o.get("path")) for o in ops] == [(e["kind"], e.get("path")) for e in ev] and all("text" in o for o in ops)
    assert not (W / "live/errors.jsonl").exists()


def test_stage_start_is_a_no_op_without_an_inbox_and_moves_owner_pins_on_a_copy(tmp_path, monkeypatch):
    path, B, M = _board(tmp_path)
    cons = {"placement": {"fixed": ["J1"]}}
    assert LV.stage_start(path, cons, None, "floorplan", str(tmp_path / "wd")) == (path, cons, None)       # stream off
    W = _on(tmp_path, monkeypatch)
    b, c, fb = LV.stage_start(path, cons, None, "floorplan", str(tmp_path / "wd"))
    assert b == path and c is cons and fb is None and not (tmp_path / "wd").exists()                     # no inbox folder
    os.makedirs(W / "feedback/inbox")
    b, c, fb = LV.stage_start(path, cons, None, "floorplan", str(tmp_path / "wd"))
    assert b == path and c is cons and fb is not None and fb.rows == []                                  # empty inbox: nothing changes
    before = open(path, "rb").read()
    (W / "feedback/inbox/2026-a.json").write_text(json.dumps(_item(id="00000001", kind="pin", target={"part": "U2"}, pin={"at": [70.0, 50.0], "rot": 90})))
    b, c, fb = LV.stage_start(path, cons, None, "floorplan", str(tmp_path / "wd"))
    assert b == str(tmp_path / "wd" / "owner-pins.kicad_pcb") and open(path, "rb").read() == before
    p = RD.load(b)["parts"]["U2"]
    assert (round(p["pos"][0], 3), round(p["pos"][1], 3), p["angle"]) == (70.0, 50.0, 90.0)
    assert c["lock"]["at"]["U2"] == [70.0, 50.0, "F.Cu"] and c["placement"]["fixed"] == ["J1", "U2"] and cons == {"placement": {"fixed": ["J1"]}}
    assert fb.measure(fb.rows[0], RD.load(b)) == (True, 0.0)


# ---------------------------------------------------------------- EVAL-PLACER-STEP4a section 6 (M1-M5)
def test_m1_a_module_pin_with_at_is_refused_and_a_module_pin_with_a_snapshot_loads(tmp_path):
    for a, want in [(_item(kind="pin", target={"module": "U1"}, pin={"at": [5, 5]}), "pin.at needs target.part (a module is pinned where it stands)"),
                    (_item(kind="pin", target={"module": "U1"}, cand="c1", snapshot=3), None)]:
        p = tmp_path / f"{a['kind']}-{want is None}.json"
        p.write_text(json.dumps(a))
        assert LV.load_feedback(str(p), _ctx())[1] == want


def test_m2_a_pin_without_at_holds_its_part_and_reads_the_distance(tmp_path, monkeypatch):
    import copy
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, [_item(id="00000001", kind="pin", target={"part": "U2"}, cand="c1", snapshot=3)])
    assert len(fb.boundary(generation=0)) == 1
    assert fb.measure(fb.rows[0], B) == (True, 0.0)
    moved = copy.deepcopy(B)
    moved["parts"]["U2"]["pos"] = [moved["parts"]["U2"]["pos"][0] + 0.5, moved["parts"]["U2"]["pos"][1]]
    assert fb.measure(fb.rows[0], moved) == (False, 0.5)


def test_m3_a_stage_that_does_not_apply_shows_the_row_and_moves_nothing(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    os.makedirs(W / "feedback/inbox")
    (W / "feedback/inbox/2026-a.json").write_text(json.dumps(_item(id="00000001", kind="pin", target={"part": "U2"}, pin={"at": [70.0, 50.0]})))
    cons = {"placement": {"fixed": ["J1"]}}
    b, c, fb = LV.stage_start(path, cons, None, "placetour", str(tmp_path / "wd"), apply=False)
    assert b == path and c is cons and fb.rows == [] and fb.rev == 0 and len(fb.notes) == 1
    assert not (tmp_path / "wd" / "owner-pins.kicad_pcb").exists()
    fb.finish(B)
    log = _lines(W / "feedback/log.jsonl")
    assert [(x["id"], x["action"]) for x in log] == [("00000001", "received"), ("00000001", "context")]
    assert log[1]["why"] == "read after the search of placetour: not applied in this run"
    assert not [x for x in _lines(W / "feedback/log.jsonl") if x["action"] == "honoured"]


def test_m4_a_second_process_in_the_same_w_continues_the_numbers_and_starts_full(tmp_path, monkeypatch):
    W = _on(tmp_path, monkeypatch)
    path, B, M = _board(tmp_path)
    assert LV.place_snapshot(B, "leader", None, M) == "live/place/leader-000001.json"
    first = (W / "live/place/leader-000001.json").read_text()
    LV._snap.clear()                             # a second process: its own counters, same W
    LV._snap.update(seq=0, lanes={})
    assert LV.place_snapshot(B, "leader", None, M) == "live/place/leader-000002.json"
    assert (W / "live/place/leader-000001.json").read_text() == first


def test_m5_an_item_that_arrives_during_a_stage_is_logged_once_and_the_file_stays(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch)
    (W / "feedback/inbox/2026-late.json").write_text(json.dumps(_item(id="00000009", kind="note", text="late")))
    fb.finish(B)
    log = _lines(W / "feedback/log.jsonl")
    assert (log[-1]["id"], log[-1]["action"], log[-1]["why"]) == ("00000009", "received", "arrived during the search: not read by this stage")
    assert os.path.exists(W / "feedback/inbox/2026-late.json")


def test_budget_row_through_criteria_carries_hard():
    from fluxplace import liveview as LV
    r = {"kind": "support", "name": "U1.7 own bypass", "verdict": "fail", "value": 9, "target": 5, "max": 5, "unit": "mm", "hard": False}
    assert LV.criteria([r])[0]["hard"] is False


# ---------------------------------------------------------------- owner answer item (kind answer)
def _answer_item(qid, choice, iid="ans00001"):
    return _item(id=iid, kind="answer", answer={"id": qid, "choice": choice})


def _asked(W, qid="q1", choices=("x", "y")):
    (W / "b.questions.json").write_text(json.dumps({"questions": [{"id": qid, "options": [{"choice": c} for c in choices]}]}))


def test_answer_item_is_recorded_in_the_fluxmap_moves_to_applied_and_changes_no_row(tmp_path, monkeypatch, capsys):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, [_answer_item("q1", "y")])
    _asked(W)
    assert fb.boundary(generation=0) == [] and fb.rows == []
    assert json.loads((W / "b.fluxmap.json").read_text())["answers"] == {"q1": "y"}
    assert os.listdir(W / "feedback/applied") == ["2026-00-ans00001.json"] and os.listdir(W / "feedback/inbox") == []
    assert "Owner answered q1 = y (applies from the next stage start)" in capsys.readouterr().out
    assert not list((W).glob("*.tmp"))


def test_answer_item_keeps_existing_map_keys_and_is_refused_for_unknown_id_or_bad_choice(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, [_answer_item("nope", "x", "ans00001"), _answer_item("q1", "zz", "ans00002")])
    _asked(W)
    (W / "b.fluxmap.json").write_text(json.dumps({"fluxmap": 1, "answers": {"other": "a"}}))
    fb.boundary(generation=0)
    assert (W / "feedback/refused/2026-00-ans00001.json.reason.txt").read_text().strip() == "unknown question or choice"
    assert (W / "feedback/refused/2026-01-ans00002.json.reason.txt").read_text().strip() == "unknown question or choice"
    assert json.loads((W / "b.fluxmap.json").read_text())["answers"] == {"other": "a"}


def test_answer_item_shape_and_length_are_checked(tmp_path):
    ctx = _ctx()
    ctx["asked"] = {"q1": ("b.kicad_pcb", {"x"})}
    assert LV.check_item(_answer_item("q1", "x"), ctx) is None
    assert LV.check_item(_answer_item("", "x"), ctx) and LV.check_item(_answer_item("q1", ""), ctx)
    assert LV.check_item(_answer_item("q1", "x" * 81), ctx)
    assert LV.check_item(_item(kind="answer", answer="q1"), ctx)
    assert LV.check_item(_answer_item("q1", "x"), dict(ctx, asked={})) == "unknown question or choice"


def test_crowding_answer_keep_is_accepted_from_the_floorplan_report(tmp_path, monkeypatch):
    W, path, B, M, fb = _fb(tmp_path, monkeypatch, [_answer_item("crowding", "keep", "ans00001")])
    (W / "b.floorplan.json").write_text(json.dumps({"history": [{"crowding": {"options": [{"key": "a-fill"}]}}]}))
    fb.boundary(generation=0)
    assert json.loads((W / "b.fluxmap.json").read_text())["answers"] == {"crowding": "keep"}


def test_last_progress_ignores_heartbeat_narration(tmp_path):
    """route-10 pairs 2026-11-27: 42 min with only 'Still working' heartbeats looked alive; real progress age must skip them."""
    import json, time
    from fluxplace import liveview as LV
    f = tmp_path / "events.jsonl"
    now = time.time()
    rows = [{"ts": now - 1200, "kind": "progress", "step": "pairs: BT2 not-laid"}] + \
           [{"ts": now - k, "kind": "narration", "text": "Still working"} for k in (600, 300, 5)]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    age, last = LV.last_progress(str(f))
    assert 1190 < age < 1300 and last == "pairs: BT2 not-laid"
    assert LV.last_progress(str(tmp_path / "missing")) == (None, None)
