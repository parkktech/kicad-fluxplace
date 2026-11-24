"""Sample and replay of an event stream for dashboard development (docs/EVENTS-CONTRACT.md).

  python3 -m fluxplace.events --sample OUT            synthetic W with every event kind (fixture-sized)
  python3 -m fluxplace.events --replay W --speed 10 OUT  re-emit an existing W with its timing / speed
"""
import json
import os
import shutil
import struct
import sys
import time
import zlib

from . import snapshot as SN

RUN = "sample-20261122-101500"
T0 = 1795360000.0


def _png(path, w=64, h=40):
    """Tiny valid RGB PNG (gradient) with no dependencies."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw += bytes((x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 140))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def _track(i, x0, y0, x1, y1, net):
    a, b = [x0, y0], [x1, y1]
    return {"id": f"{i:012x}", "l": "F.Cu", "n": str(net), "w": 0.15, "a": a, "b": b, "m": None, "lk": False}


def _full(seq, tracks, parts, opens):
    return {"v": 1, "seq": seq, "ts": round(T0 + seq, 3), "board": "sample.kicad_pcb", "bbox": [0, 0, 100, 80],
            "layers": ["F.Cu", "B.Cu"], "edge": [[0, 0], [100, 0], [100, 80], [0, 80]],
            "nets": {"0": "GND", "1": "VBUS", "2": "CAN_H", "3": "CAN_L"}, "parts": parts,
            "pads": [{"ref": "U1", "num": "1", "x": 10.0, "y": 10.0, "w": 0.5, "h": 0.5, "rot": 0,
                      "shape": "rect", "layers": ["F.Cu"], "net": "1"}],
            "tracks": tracks, "vias": [{"id": "000000000001", "x": 5.0, "y": 5.0, "d": 0.6, "n": "0", "lk": False}],
            "metrics": {"vias": 1, "track_mm": {"F.Cu": 24.0}, "net_mm": {"VBUS": 4.0, "CAN_H": 8.0},
                        "parts": 2, "pads": 1, "density": 0.0004},
            "zones": [{"l": "B.Cu", "n": "0", "pts": [[0, 0], [100, 0], [100, 80], [0, 80]]}], "opens": opens}


def _parts(dx):
    return [{"ref": "U1", "x": 10.0 + dx, "y": 10.0, "rot": 0, "side": "top", "crt": [8, 8, 12, 12], "value": "MCU"},
            {"ref": "C3", "x": 11.2, "y": 9.0, "rot": 90, "side": "bottom", "crt": [10.5, 8.2, 11.9, 9.8], "value": "100n"}]


def _placer(out, ev):
    """The placer kinds of liveview.py (F16): two leader snapshots (full, delta with one moved part), one 3D model line, one
    refused and one applied feedback item, a gate that closed by timeout. Files under live/ and feedback/."""
    from . import liveview as LV
    for d in ("live/place", "live/glb", "feedback/gates"):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    key = [["hard rows", 1], ["sum over max", 12.5], ["ghost opens", None]]
    crit = [{"id": "C3 -> U1.1", "ok": False, "value": 6.2, "limit": 5.0, "text": "support C3 -> U1.1: 6.2 / 5.0 mm"}]

    def snap(seq, dx, k):
        parts = [dict(p, mod="U1", fixed=None, h=None) for p in _parts(dx)]
        return dict(_full(seq, [], parts, 3), vias=[], grid=LV.grid_of((0, 0, 100, 80)), keepouts=[], key=k, criteria=crit, cand=f"c{seq}",
                    arm="unguided", generation=seq - 1, rows_rev=0,
                    modules=[{"id": "U1", "name": "U1", "role": None, "side": "top", "fixed": False, "bbox": [8 + dx, 8, 12 + dx, 12], "group": False, "n": 2}],
                    regions=[{"id": "owner-00000002", "src": "owner", "target": ["U1"], "rect": [0.0, 0.0, 25.0, 20.0], "cells": "A1:B2", "keep": "in", "exit": None}])
    s1, s2 = snap(1, 0.0, key), snap(2, 3.0, [["hard rows", 0], ["sum over max", 0.0], ["ghost opens", None]])
    dl = LV.snapshot_delta(s1, s2)
    for name, obj in (("live/place/leader-000001.json", s1), ("live/place/leader-000002.delta.json", dl)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
    ev("narration", "floorplan", 2, phase="start", text="Seating the modules on the board, then searching for a better arrangement. "
       "Done when the search ends and the best layout is kept.", eta_s=None)
    ev("narration", "floorplan", 3.5, phase="doing", text="Seating 14 support parts around 2 modules' anchor parts (candidate c1).", eta_s=None)
    ev("narration", "floorplan", 4, phase="doing", text="floorplan round 2 of 8: best so far hard rows 1, sum over max 12.5", eta_s=42.0)
    base = {"generations": None, "trial": None, "trials": 8, "arm": "unguided", "seed": 1, "pair": None, "leader_key": None, "why": None,
            "rows_rev": 0, "eta_s": None, "criteria": crit}
    ev("placement_progress", "floorplan", 3, phase="anneal", generation=0, cand="c1", key=key, leader="c1", is_leader=True, verdict="kept",
       elapsed_s=1.0, text="floorplan anneal gen 0 unguided c1 hard rows 1 sum over max 12.5 kept", **base)
    ev("placement_snapshot", "floorplan", 3, lane="leader", seq=1, path="live/place/leader-000001.json", full=True, delta=False, base=None, cand="c1",
       arm="unguided", generation=0, moved=0, parts=2, key=key, text="leader snapshot 1: 2 parts")
    ev("feedback_result", "floorplan", 4, id="00000001", item_kind="pin", action="refused", generation=0, rows_rev=0, row=None,
       why="unknown part U99", effect=None, file="2026-00000001.json", text="feedback 00000001 (pin): refused - unknown part U99")
    ev("feedback_result", "floorplan", 4, id="00000002", item_kind="region", action="applied", generation=0, rows_rev=1, why=None, effect=None,
       row={"id": "00000002", "kind": "region", "src": "owner", "hard": True, "rect": [0.0, 0.0, 25.0, 20.0], "keep": "in", "refs": ["U1"]},
       file="2026-00000002.json", text="feedback 00000002 (region): applied")
    ev("placement_progress", "floorplan", 5, phase="anneal", generation=1, cand="c2", key=s2["key"], leader="c2", is_leader=True, verdict="kept",
       elapsed_s=3.0, text="floorplan anneal gen 1 unguided c2 hard rows 0 sum over max 0.0 kept", **dict(base, rows_rev=1))
    ev("placement_snapshot", "floorplan", 5, lane="leader", seq=2, path="live/place/leader-000002.delta.json", full=False, delta=True, base=1, cand="c2",
       arm="unguided", generation=1, moved=len(dl["moved"]), parts=2, key=s2["key"], text="leader snapshot 2: 2 parts, 1 moved")
    with open(os.path.join(out, "live/glb/c2.glb"), "wb") as fh:
        fh.write(b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00")              # header only: a stand-in, not a model
    glb = {"path": "live/glb/c2.glb", "cand": "c2", "why": "leader", "snapshot": 2, "secs": 15.7, "bytes": 12, "models": 2, "missing": 0,
           "text": "3D model of c2 (leader): 0.0 MB in 15.7 s, 0 model(s) missing"}
    with open(os.path.join(out, "live/glb/latest.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(glb, kind="glb_ready", ts=T0 + 6), fh)
    ev("glb_ready", "floorplan", 6, **glb)
    ev("glb_failed", "floorplan", 6, cand="c3", why="timeout after 60 s", text="3D export of c3 failed: timeout after 60 s")
    cands = [{"cand": "c2", "key": s2["key"], "criteria": crit, "snapshot": 2, "glb": "live/glb/c2.glb"}]
    gate = {"id": "PLACE_OK", "stage": "placetour", "opened": T0 + 7, "deadline": T0 + 7.5, "default": "proceed", "machine": "PASS", "candidates": cands}
    ev("gate_open", "placetour", 7, id="PLACE_OK", deadline=T0 + 7.5, default="proceed", machine="PASS", candidates=cands,
       path="feedback/gates/PLACE_OK.json", text="gate PLACE_OK open: machine PASS, waits 0.00833333 min, default proceed")
    ev("gate_closed", "placetour", 7.5, id="PLACE_OK", by="timeout", action="proceed", choice="c2", item=None,
       text="gate PLACE_OK closed by timeout: proceed (no answer in 0.00833333 min: default proceed)")
    with open(os.path.join(out, "feedback/gates/PLACE_OK.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(gate, closed=T0 + 7.5, by="timeout", action="proceed", choice="c2", item=None, gate_wait_s=0.5), fh)


def sample(out):
    """Write a synthetic W to `out`: events.jsonl with every kind, 2 full + 3 delta snapshots, eta.json, run.json,
    TOURNAMENT.md and one PNG."""
    out = os.fspath(out)
    os.makedirs(os.path.join(out, "snap"), exist_ok=True)
    os.makedirs(os.path.join(out, "png"), exist_ok=True)
    base = [_track(i, i * 5.0, 2.0, i * 5.0 + 4.0, 2.0, i % 4) for i in range(6)]
    s1 = _full(1, base, _parts(0.0), 3)
    mod = [dict(t) for t in base]
    mod[0] = dict(mod[0], id="a1b2c3d4e5f6", b=[9.0, 9.0])               # one track moved (1 add, 1 del)
    s2 = _full(2, mod, _parts(0.2), 2)
    s3 = _full(3, mod + [_track(9, 1.0, 40.0, 30.0, 40.0, 2)], _parts(0.4), 2)
    s4 = _full(4, mod + [_track(9, 1.0, 40.0, 30.0, 40.0, 2)], _parts(0.4), 1)
    s5 = _full(5, mod, _parts(0.4), 1)
    snaps = [("snap/000001.json", s1, True, "stage_end"), ("snap/000002.delta.json", SN._delta_of(s1, s2), False, "progress"),
             ("snap/000003.delta.json", SN._delta_of(s1, s3), False, "progress"),
             ("snap/000004.json", s4, True, "stage_end"), ("snap/000005.delta.json", SN._delta_of(s4, s5), False, "iteration")]
    for rel, obj, _f, _r in snaps:
        with open(os.path.join(out, rel), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    with open(os.path.join(out, "snap", "latest.json"), "w", encoding="utf-8") as fh:
        json.dump(s4, fh)
    _png(os.path.join(out, "png", "lookahead-1.png"))
    with open(os.path.join(out, "TOURNAMENT.md"), "w", encoding="utf-8") as fh:
        fh.write("| round | id | variant | fitness | png | verdict |\n|---|---|---|---|---|---|\n"
                 "| 0 | ghost-krt | unlocked | - | - | reference |\n"
                 "| 1 | c1 | pairs | (-2, 0, 3, 0, 0, 0.0) | png/lookahead-1.png | keep |\n")

    rows = []

    def ev(kind, stage, dt, **kw):
        rows.append(dict({"v": 1, "ts": round(T0 + dt, 3), "run": RUN, "seq": len(rows) + 1, "pid": 4242,
                          "stage": stage, "kind": kind}, **kw))

    ev("run_start", None, 0, board="sample.kicad_pcb", flow="classic",
       stages=["ready", "place", "route", "close"],
       features={"components": 2, "nets": 4, "pads": 1, "layers": 2, "area_mm2": 8000.0, "density": 0.0001})
    ev("stage_start", "ready", 1, n=1, name="ready")
    ev("stage_end", "ready", 2, n=1, name="ready", verdict="pass", line="ready ok", secs=1.0, board="sample.kicad_pcb")
    _placer(out, ev)
    ev("eta", "ready", 2, secs_left=4300.0, done_frac=0.02, model="table", basis="place:table; route:table")
    ev("stage_start", "place", 3, n=2, name="place")
    ev("parts", "place", 4, frame=[["U1", 10.0, 10.0, 0, "top"], ["C3", 11.2, 9.0, 90, "bottom"]])
    ev("iteration", "place", 5, loop="place", i=1, n=5, metrics={"moved": True, "movables": 2}, note="")
    ev("progress", "place", 6, step="legalise pass 1", pct=20.0, state="running")
    ev("stage_end", "place", 8, n=2, name="place", verdict="pass", line="place ok", secs=5.0, board="sample.kicad_pcb")
    ev("snapshot", "place", 8, path="snap/000001.json", full=True, reason="stage_end", opens=3)
    ev("stage_start", "route", 9, n=3, name="route")
    ev("progress", "route", 10, step="krt pass 1", pct=10.0, state="running")
    ev("log", "route", 10.5, level="info", text="pass 1: 3 nets left", src="krt")
    ev("log", "route", 10.7, level="warn", text="via count high on CAN_H", src="krt")
    ev("iteration", "route", 11, loop="pairs", i=2, n=8, metrics={"complete": 1, "total": 2}, note="")
    ev("snapshot", "route", 11, path="snap/000002.delta.json", full=False, reason="progress", opens=2)
    ev("iteration", "route", 12, loop="ghost", i=0, n=None, metrics={"opens": 4}, note="reference")
    ev("artifact", "route", 12, type="png", path="png/lookahead-1.png", label="ghost-krt")
    ev("snapshot", "route", 13, path="snap/000003.delta.json", full=False, reason="progress", opens=2)
    ev("iteration", "route", 14, loop="lookahead", i=1, n=None, metrics={"pairs_complete": 2, "opens": 0}, note="keep")
    ev("artifact", "route", 14, type="table", path="TOURNAMENT.md", label="TOURNAMENT")
    ev("stage_end", "route", 15, n=3, name="route", verdict="pass", line="route ok", secs=6.0, board="sample.kicad_pcb")
    ev("snapshot", "route", 15, path="snap/000004.json", full=True, reason="stage_end", opens=1)
    ev("stage_start", "close", 16, n=4, name="close")
    ev("iteration", "close", 17, loop="close", i=1, n=40, metrics={"score": [1, 0, 0], "opens": 1, "shorts": 0}, note="")
    ev("snapshot", "close", 17, path="snap/000005.delta.json", full=False, reason="iteration", opens=1)
    ev("eta", "close", 18, secs_left=120.0, done_frac=0.9, model="table", basis="close:table")
    ev("run_end", None, 19, verdict="stopped", secs=19.0, board="sample.kicad_pcb")
    with open(os.path.join(out, "events.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    with open(os.path.join(out, "eta.json"), "w", encoding="utf-8") as fh:
        json.dump({"ts": T0 + 18, "secs_left": 120.0, "done_frac": 0.9, "model": "table", "basis": "close:table",
                   "stage": "close"}, fh)
    with open(os.path.join(out, "run.json"), "w", encoding="utf-8") as fh:
        json.dump({"run": RUN, "board": "sample.kicad_pcb", "flow": "classic", "stages": ["ready", "place", "route", "close"],
                   "features": rows[0]["features"], "started": T0, "ended": T0 + 19, "verdict": "stopped"}, fh)
    return out


def replay(work, out, speed=1.0):
    """Re-emit the events of an existing W into `out` with the original timing divided by `speed`; the non-event
    files (snap/, eta.json, run.json, png/, TOURNAMENT.md) are copied at the start. Returns the line count."""
    work, out = os.fspath(work), os.fspath(out)
    os.makedirs(out, exist_ok=True)
    for name in os.listdir(work):
        src = os.path.join(work, name)
        if name == "events.jsonl" or name.endswith(".tmp"):
            continue
        dst = os.path.join(out, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    speed = float(speed) if float(speed) > 0 else 1.0
    n, prev = 0, None
    with open(os.path.join(work, "events.jsonl"), encoding="utf-8") as src, \
            open(os.path.join(out, "events.jsonl"), "w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            obj = json.loads(line)
            if prev is not None:
                gap = (obj["ts"] - prev) / speed
                if gap > 0:
                    time.sleep(min(gap, 5.0))
            prev = obj["ts"]
            dst.write(json.dumps(obj, separators=(",", ":")) + "\n")
            dst.flush()
            n += 1
    return n


def main(argv):
    if len(argv) == 2 and argv[0] == "--sample":
        print(sample(argv[1]))
        return 0
    if len(argv) == 5 and argv[0] == "--replay" and argv[2] == "--speed":
        print(replay(argv[1], argv[4], argv[3]))
        return 0
    if len(argv) == 3 and argv[0] == "--replay":
        print(replay(argv[1], argv[2]))
        return 0
    print("usage: python3 -m fluxplace.events --sample OUT | --replay W [--speed N] OUT", file=sys.stderr)
    return 2
