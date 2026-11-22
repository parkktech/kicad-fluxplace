import argparse
import json
import os

from fluxplace import eta as ETA, events, lookahead, pipeline, routestatus, snapshot as SN

HEAD = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'


def _board(path):
    path.write_text(HEAD + '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))\n'
                    '(segment (start 1 1) (end 5 1) (width 0.15) (layer "F.Cu") (net "N1"))\n)\n')
    return str(path)


def _rows(work):
    return [json.loads(x) for x in open(os.path.join(str(work), "events.jsonl"), encoding="utf-8") if x.strip()]


def _clean(monkeypatch, tmp_path=None):
    for k in ("FLUXPLACE_EVENTS", "FLUXPLACE_RUN", "FLUXPLACE_STAGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ETA, "TIMINGS", str(tmp_path / "timings.jsonl") if tmp_path else "/nonexistent/t.jsonl")
    events._buckets.clear()
    SN.set_active(None)


def test_status_write_emits_progress(tmp_path, monkeypatch):
    _clean(monkeypatch)
    events.init(tmp_path, run="H1")
    routestatus._atomic(str(tmp_path / "x.status.json"), {"current_step": "pass 1", "state": "running", "pct": 10.0})
    routestatus._atomic(str(tmp_path / "other.json"), {"a": 1})
    rows = _rows(tmp_path)
    assert [r["kind"] for r in rows] == ["progress"]
    assert rows[0]["step"] == "pass 1" and rows[0]["pct"] == 10.0


def test_iteration_and_artifact_paths_are_relative(tmp_path, monkeypatch):
    _clean(monkeypatch)
    events.init(tmp_path, run="H2")
    events.iteration("pairs", 2, 8, {"complete": 3, "total": 5, "obj": object()}, note="ok")
    events.artifact("png", os.path.join(str(tmp_path), "lookahead", "c1.png"), "cand 1")
    events.artifact("table", "relative/T.md", "T")
    rows = _rows(tmp_path)
    assert rows[0]["kind"] == "iteration" and rows[0]["metrics"] == {"complete": 3, "total": 5}
    assert rows[1]["path"] == "lookahead/c1.png"
    assert rows[2]["path"] == "relative/T.md"


def test_table_writes_artifact(tmp_path, monkeypatch):
    _clean(monkeypatch)
    events.init(tmp_path, run="H3")
    rows = [{"round": 0, "id": "ghost-krt", "variant": "u", "fit": None, "png": "-", "verdict": "ref"}]
    lookahead._table(str(tmp_path / "TOURNAMENT.md"), rows)
    got = _rows(tmp_path)
    assert got[-1]["kind"] == "artifact" and got[-1]["type"] == "table"


def test_stage_end_writes_stage_end_and_forced_full_snapshot(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path / "cfg")
    events.init(tmp_path, run="H4")
    SN.set_active(SN.Snapper(str(tmp_path)))
    b = _board(tmp_path / "route.kicad_pcb")
    pipeline._ev_stage_end(9, "route", {"verdict": "pass", "line": "ok"}, b, 2.46)
    kinds = [r["kind"] for r in _rows(tmp_path)]
    assert kinds[:3] == ["snapshot", "stage_end", "eta"]
    stage_end = [r for r in _rows(tmp_path) if r["kind"] == "stage_end"][0]
    assert stage_end["metrics"]["track_mm"]["F.Cu"] == 4.0 and stage_end["metrics"]["vias"] == 0
    snap = [r for r in _rows(tmp_path) if r["kind"] == "snapshot"][0]
    assert snap["full"] is True and snap["reason"] == "stage_end"
    assert os.path.exists(os.path.join(str(tmp_path), snap["path"]))
    assert os.path.exists(os.path.join(str(tmp_path), "snap", "latest.json"))


def test_run_wrapper_writes_header_and_end_and_restores_env(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    work = tmp_path / "W"
    seen = {}

    def fake_run(a):
        seen["env"] = os.environ.get("FLUXPLACE_EVENTS")
        events.emit("log", level="info", text="inside", src="t")
        return 0

    monkeypatch.setattr(pipeline, "_run", fake_run)
    rc = pipeline.run(argparse.Namespace(workdir=str(work), board=None, flow=None))
    assert rc == 0
    assert seen["env"] == os.path.join(str(work), "events.jsonl")
    assert "FLUXPLACE_EVENTS" not in os.environ and "FLUXPLACE_RUN" not in os.environ
    kinds = [r["kind"] for r in _rows(work)]
    assert kinds[-1] == "run_end" and "log" in kinds
    hdr = json.load(open(work / "run.json"))
    assert hdr["verdict"] == "pass" and hdr["ended"] is not None


def test_hooks_never_raise_without_workdir(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    pipeline._ev_stage_end(1, "x", {"verdict": "pass"}, str(tmp_path / "nope.kicad_pcb"), 0.1)
    SN.active_take(str(tmp_path / "nope.kicad_pcb"), "x", force=True)
    routestatus._atomic(str(tmp_path / "y.progress.json"), {"current_step": 3})


def test_nested_run_logs_to_parent_flags_child_and_run_start(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    parent = tmp_path / "P"
    events.init(parent, run="PR")
    child = tmp_path / "ghost" / "W"
    seen = {}

    def fake_run(a):
        seen["nested"] = os.environ.get("FLUXPLACE_NESTED")
        seen["events"] = os.environ.get("FLUXPLACE_EVENTS")
        return 0

    monkeypatch.setattr(pipeline, "_run", fake_run)
    assert pipeline.run(argparse.Namespace(workdir=str(child), board=None, flow=None)) == 0
    assert seen["nested"] == "1"
    assert seen["events"] == os.path.join(str(child), "events.jsonl")
    # parent stream: exactly one nested log line naming the child W
    plog = [r for r in _rows(parent) if r["kind"] == "log"]
    assert len(plog) == 1 and plog[0]["src"] == "nested" and plog[0]["text"] == f"nested run {child}"
    # child stream: its own run_start with nested true, and its own run_end
    crows = _rows(child)
    start = [r for r in crows if r["kind"] == "run_start"]
    assert start and start[0]["nested"] is True
    assert crows[-1]["kind"] == "run_end"
    assert json.load(open(child / "run.json"))["nested"] is True
    # env restored to the parent's stream afterwards
    assert os.environ.get("FLUXPLACE_EVENTS") == os.path.join(str(parent), "events.jsonl")
    assert "FLUXPLACE_NESTED" not in os.environ


def test_top_level_run_start_nested_false(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    work = tmp_path / "W"
    monkeypatch.setattr(pipeline, "_run", lambda a: 0)
    pipeline.run(argparse.Namespace(workdir=str(work), board=None, flow=None))
    start = [r for r in _rows(work) if r["kind"] == "run_start"][0]
    assert start["nested"] is False
    assert json.load(open(work / "run.json"))["nested"] is False


def test_lookahead_pct_formula_and_progress_file(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    P = {"max_rounds": 3}
    assert lookahead._pct(P, 1, 0, 4) == 0.0
    assert lookahead._pct(P, 2, 1, 2) == 50.0            # round 2 half landed: (1 + 0.5) / 3
    assert lookahead._pct(P, 3, 0, 0) == 66.7            # round 3 begins: 2 / 3
    assert lookahead._pct(P, 4, 0, 0) == 100.0           # capped
    spath = str(tmp_path / "progress.json")
    lookahead._progress(spath, "running", "round 2", pct=50.0)
    assert json.load(open(spath))["pct"] == 50.0


def test_candidate_iteration_events_index_and_metrics(tmp_path, monkeypatch):
    _clean(monkeypatch, tmp_path)
    events.init(tmp_path, run="G3")
    lookahead._CAND["i"] = 0
    fit = {"pairs_complete": 2, "pairs_total": 3, "opens": 1, "key": (-2, 0, 1, 0, 0, 0.0),
           "critical": ["CAN_H", "CAN_L"], "blockers": {"P|N": {"count": 1}}, "unconnected": []}
    lookahead._row(1, "r1-0", "pairs", fit, None, "kept")
    lookahead._row(1, "ghost-krt", "unlocked", None, None, "reference")
    lookahead._row(1, "r1-1", "pairs", fit, None, "winner")
    rows = [r for r in _rows(tmp_path) if r["kind"] == "iteration"]
    assert [(r["loop"], r["i"]) for r in rows] == [("lookahead", 1), ("ghost", 1), ("lookahead", 2)]
    assert rows[0]["note"] == "r1-0" and rows[0]["metrics"]["opens"] == 1
    assert "critical" not in rows[0]["metrics"] and "blockers" not in rows[0]["metrics"]
    assert "key" not in rows[0]["metrics"]
