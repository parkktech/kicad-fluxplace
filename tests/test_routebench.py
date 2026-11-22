"""routebench part 1: stage (strip), metrics (helpers monkeypatched; no router, no kicad-cli)."""
import pytest

import pcbgen
import routebench as RB
from fluxplace import corpus as C, gaps

BODY = ('(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") (net "A") (uuid "11111111-1111-4111-8111-111111111111"))\n'
        '(segment (start 10 12) (end 20 12) (width 0.2) (layer "F.Cu") (net "B") (uuid "22222222-2222-4222-8222-222222222222"))\n'
        '(via (at 15 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "A") (uuid "33333333-3333-4333-8333-333333333333"))\n'
        '(zone (net "A") (net_name "A") (layer "F.Cu") (uuid "44444444-4444-4444-8444-444444444444") (hatch edge 0.5)\n'
        '  (polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))\n')


def _ent(path, strip):
    return {"name": "t", "board": path, "bench": {"strip": strip, "task": "whole", "budget_s": 60}}


def test_strip_removes_net_copper_and_keeps_zones(tmp_path):
    src = pcbgen.board(tmp_path, BODY)
    before = open(src).read()
    out = RB.stage(_ent(src, True), str(tmp_path / "work"))
    text = open(out).read()
    assert "(segment" not in text and "(via" not in text
    assert text.count("(zone") == before.count("(zone") == 1
    assert open(src).read() == before        # source board untouched


def test_stage_without_strip_keeps_copper(tmp_path):
    src = pcbgen.board(tmp_path, BODY)
    out = RB.stage(_ent(src, False), str(tmp_path / "work"))
    assert open(out).read().count("(segment") == 2


def test_metrics_fills_every_key_with_constraints(tmp_path, monkeypatch):
    src = pcbgen.board(tmp_path, BODY)
    drc = {"violations": [{"type": "clearance", "severity": "error"}], "unconnected_items": []}
    m = {"opens": 2, "shorts": 0, "errors": 1, "err_keys": {("clearance", "x"): 1}, "drc": drc}
    monkeypatch.setattr(gaps, "measure", lambda b, *a, **k: m)
    monkeypatch.setattr(gaps, "_pair_audit", lambda b, c, o=None: (None, None, [
        {"status": "complete"}, {"status": "half"}, {"status": "open"}]))
    gl = [{"kind": "power-width", "class": "plain", "mag": 0.05}, {"kind": "power-vias", "class": "plain", "mag": 1}, {"kind": "pair-open", "class": "pair", "sev": 3}]
    monkeypatch.setattr(gaps, "classify", lambda d, c, board=None, **k: gl)
    monkeypatch.setattr(C, "_pcb_metrics", lambda p: {"board.vias": 4, "board.tracks": 9, "board.track_mm": 12.5})
    r = RB.metrics(src, {"rf": {}})
    for k in ("opens", "shorts", "errors", "err_types", "pairs", "rails", "score", "vias", "tracks", "track_mm"):
        assert r[k] is not None, k
    assert r["pairs"] == {"complete": 1, "mismatch": 0, "half": 1, "open": 1}
    assert r["rails"] == {"narrow": 1, "vias": 1, "mag": 1.05}
    assert r["score"] == gaps.score(m, gl)
    assert r["err_types"] == [["clearance", 1]]


def test_metrics_without_constraints_nulls_pairs_rails_score(tmp_path, monkeypatch):
    src = pcbgen.board(tmp_path, BODY)
    m = {"opens": 0, "shorts": 0, "errors": 0, "err_keys": {}, "drc": {"violations": [], "unconnected_items": []}}
    monkeypatch.setattr(gaps, "measure", lambda b, *a, **k: m)
    monkeypatch.setattr(C, "_pcb_metrics", lambda p: {})
    r = RB.metrics(src, None)
    assert r["pairs"] is None and r["rails"] is None and r["score"] is None
    assert r["opens"] == 0 and r["vias"] is None


def test_failing_helper_gives_null_not_lost_row(tmp_path, monkeypatch):
    src = pcbgen.board(tmp_path, BODY)
    m = {"opens": 1, "shorts": 0, "errors": 0, "err_keys": {}, "drc": {"violations": [], "unconnected_items": []}}
    monkeypatch.setattr(gaps, "measure", lambda b, *a, **k: m)
    monkeypatch.setattr(gaps, "_pair_audit", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(C, "_pcb_metrics", lambda p: {"board.vias": 0})
    r = RB.metrics(src, {"x": 1})
    assert r["pairs"] is None and r["opens"] == 1 and r["vias"] == 0


# ---- part 2: engines, rows, outputs (runners stubbed; metrics replaced by a lookup)
import json
import subprocess

START = {"opens": 10, "shorts": 0, "errors": 2, "vias": 0, "tracks": 5, "track_mm": 10.0, "score": [1],
         "_err_keys": {"a": 1}}
END = {"opens": 0, "shorts": 0, "errors": 0, "err_types": [["clearance", 1]], "pairs": {"complete": 2, "mismatch": 0,
       "half": 0, "open": 0}, "rails": {"narrow": 0, "vias": 0, "mag": 0.0}, "score": [0], "vias": 3, "tracks": 7,
       "track_mm": 12.5, "_err_keys": {"a": 1, "b": 1}}


def _fake_metrics(monkeypatch):
    def fake(board, cons=None):
        return dict(START) if "start" in board else dict(END)
    monkeypatch.setattr(RB, "metrics", fake)


def _prep(tmp_path):
    ent = {"name": "t", "board": str(tmp_path / "start.kicad_pcb"), "bench": {"task": "finish", "budget_s": 60}}
    (tmp_path / "start.kicad_pcb").write_text("")
    copy = str(tmp_path / "start.kicad_pcb")
    work = str(tmp_path / "work" / "krt")
    return ent, copy, work


def test_ok_row_has_every_key_and_deltas(tmp_path, monkeypatch):
    _fake_metrics(monkeypatch)
    ent, copy, work = _prep(tmp_path)
    res_board = tmp_path / "work" / "krt.kicad_pcb"
    res_board.parent.mkdir(parents=True, exist_ok=True)
    res_board.write_text("")
    monkeypatch.setitem(RB.ENGINES, "krt", lambda c, w, e: {"rc": 0, "wall_s": 2.0, "log": "l", "version": "v",
                                                             "boards": {"krt": str(res_board)}, "wall": {"krt": 2.0}})
    rows = RB.bench_engine(ent, copy, dict(START), "krt", work, None, "tool", "sha")
    r = rows[0]
    assert r["status"] == "ok" and r["error"] is None and r["rc"] == 0
    for k in ("board", "task", "engine", "start", "end", "completion", "wall_s", "budget_s", "rc", "engine_version",
              "board_sha", "tool", "out_board", "log"):
        assert k in r
    assert r["end"]["new_err_keys"] == 1 and r["end"]["d_vias"] == 3 and r["end"]["d_track_mm"] == 2.5
    assert r["completion"] == 1.0 and r["end"]["pairs"]["complete"] == 2 and "_err_keys" not in r["start"]


def test_timeout_gives_timeout_row_with_start(tmp_path, monkeypatch):
    _fake_metrics(monkeypatch)
    ent, copy, work = _prep(tmp_path)

    def boom(c, w, e):
        raise subprocess.TimeoutExpired("route", 60)
    monkeypatch.setitem(RB.ENGINES, "krt", boom)
    r = RB.bench_engine(ent, copy, dict(START), "krt", work, None, "tool", "sha")[0]
    assert r["status"] == "timeout" and r["end"] is None and r["start"]["opens"] == 10


def test_rc2_without_board_is_error(tmp_path, monkeypatch):
    _fake_metrics(monkeypatch)
    ent, copy, work = _prep(tmp_path)
    monkeypatch.setitem(RB.ENGINES, "krt", lambda c, w, e: {"rc": 2, "wall_s": 1.0, "log": None, "boards": {}})
    r = RB.bench_engine(ent, copy, dict(START), "krt", work, None, "tool", "sha")[0]
    assert r["status"] == "error" and r["end"] is None and r["error"]


def test_kernel_is_not_available_before_c2(tmp_path, monkeypatch):
    _fake_metrics(monkeypatch)
    ent, copy, work = _prep(tmp_path)
    rows = RB.bench_engine(ent, copy, dict(START), "kernel", work, None, "tool", "sha")
    assert [r["status"] for r in rows] == ["n/a"] and rows[0]["end"] is None


def test_outputs_one_jsonl_line_per_row(tmp_path, monkeypatch):
    _fake_metrics(monkeypatch)
    ent, copy, work = _prep(tmp_path)
    monkeypatch.setitem(RB.ENGINES, "freerouting", lambda c, w, e: {"rc": 1, "wall_s": 1.0, "log": None,
                                                                    "boards": {}})
    rows = RB.bench_engine(ent, copy, dict(START), "freerouting", work, None, "tool", "sha")
    jl = tmp_path / "routebench.jsonl"
    out = tmp_path / "out"
    RB.write_outputs(rows, str(out), {"ts": "t", "tool": "tool", "kicad": "10.0.5", "cpus": 1}, str(jl))
    RB.write_outputs(rows, str(out), {"ts": "t", "tool": "tool", "kicad": "10.0.5", "cpus": 1}, str(jl))
    assert len(jl.read_text().splitlines()) == 2 * len(rows) == 4
    assert json.loads(jl.read_text().splitlines()[0])["ts"] == "t"
    assert (out / "routebench.json").is_file() and "| board |" in (out / "routebench.md").read_text()


def test_pipeline_cmd_flags_and_constraints_only_when_present(tmp_path):
    ent = {"name": "t", "bench": {"budget_s": 900}, "spec": "/s.json"}
    cmd = RB._pipeline_cmd("/c.kicad_pcb", "/w", ent, "krt-whole")
    for flag in ("--flow", "--from", "--to", "--router", "--route-budget", "--assume-defaults", "--allow-incomplete",
                 "--allow-undocumented", "--no-api", "--report-dir"):
        assert flag in cmd
    assert cmd[cmd.index("--route-budget") + 1] == "900" and "--constraints" not in cmd
    assert cmd[cmd.index("--spec") + 1] == "/s.json"
    ent["constraints"] = "/k.toml"
    assert "--constraints" in RB._pipeline_cmd("/c.kicad_pcb", "/w", ent, "freerouting")
