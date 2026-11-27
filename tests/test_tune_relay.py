"""st_tune: a needs_reroute pair is re-laid (pairs --only --unlock) and tuned; one the re-lay kept is length-tuned as it stands."""
import json, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pipeline as PL


def _ctx(tmp):
    return types.SimpleNamespace(cs=lambda: ["--constraints", "c.toml"], dir=lambda n, name: str(tmp))


def _run(tmp, relay_rc, rows_after_relay, mp):
    out = str(tmp / "b.kicad_pcb")
    open(out, "w").write("(kicad_pcb)")
    rep = out + ".tune.json"
    json.dump({"pairs": [{"pair": "A_P/A_N", "status": "needs_reroute"}, {"pair": "B_P/B_N", "status": "ok"}]}, open(rep, "w"))
    calls = []

    def fake(argv, log):
        calls.append(argv)
        if argv[0] == "pairs":
            assert argv[argv.index("--out") + 1] != argv[1]            # pairs refuses --out == the input board
            if relay_rc in (0, 1):
                open(argv[argv.index("--out") + 1], "w").write("(kicad_pcb)")
            return relay_rc, "pairs"
        if "--allow-layer-mismatch" in argv:
            json.dump({"pairs": [{"pair": "A_P/A_N", "status": "ok"}]}, open(argv[argv.index("--report") + 1], "w"))
        elif rows_after_relay is not None:
            json.dump({"pairs": rows_after_relay}, open(rep, "w"))
        return 0, "tune"
    mp.setattr(PL, "_run_cli", fake)
    rc, text = PL._relay_and_retune(_ctx(tmp), str(tmp), out, ["tune", "--board", out, "--out", out], "first")
    return calls, rc, text, json.load(open(rep))


def test_relaid_pair_is_tuned_again_and_no_mismatch_tune_runs(tmp_path, monkeypatch):
    calls, rc, _t, rep = _run(tmp_path, 0, [{"pair": "A_P/A_N", "status": "ok"}, {"pair": "B_P/B_N", "status": "ok"}], monkeypatch)
    assert [c[0] for c in calls] == ["pairs", "tune"] and calls[0][calls[0].index("--only") + 1] == "A_P,A_N" and "--unlock" in calls[0]
    assert rc == 0 and {r["status"] for r in rep["pairs"]} == {"ok"} and "relay: pairs --only A_P,A_N: done" in _t


def test_pair_the_relay_kept_is_length_tuned_as_it_stands_and_the_report_is_merged(tmp_path, monkeypatch):
    calls, rc, text, rep = _run(tmp_path, 0, [{"pair": "A_P/A_N", "status": "needs_reroute"}, {"pair": "B_P/B_N", "status": "ok"}], monkeypatch)
    last = calls[-1]
    assert "--allow-layer-mismatch" in last and last[last.index("--prefix") + 1] == "A_P" and "could not be re-laid" in text
    assert {r["pair"]: r["status"] for r in rep["pairs"]} == {"B_P/B_N": "ok", "A_P/A_N": "ok"}


def test_failed_relay_still_tunes_as_it_stands(tmp_path, monkeypatch):
    calls, rc, text, rep = _run(tmp_path, 99, [{"pair": "A_P/A_N", "status": "needs_reroute"}], monkeypatch)
    assert "failed (exit 99)" in text and "--allow-layer-mismatch" in calls[-1]


def test_retune_relaid_tunes_only_the_pairs_the_route_scope_touches(tmp_path, monkeypatch):
    seg = '(segment (start 1 1) (end 5 1) (width 0.15) (layer "F.Cu") (net "{}") (uuid "00000000-0000-0000-0000-00000000000{}"))\n'
    b = tmp_path / "b.kicad_pcb"
    b.write_text('(kicad_pcb (version 20260206) (generator "pcbnew")\n' + "".join(seg.format(n, i) for i, n in enumerate(["A_P", "A_N", "B_P", "B_N"])) + ")\n")
    seen = {}

    def fake_exec(c, n, name, board, later):
        seen.update(n=n, name=name, only=os.environ.get("FLUXPLACE_TUNE_ONLY"))
        return {"verdict": "pass", "line": "ok", "board": str(tmp_path / "t.kicad_pcb")}
    monkeypatch.setattr(PL, "_exec_stage", fake_exec)
    monkeypatch.setattr(PL, "save_state", lambda *a: None)
    st = {"route_scope": {"nets": ["A_N"]}}
    out = PL._retune_relaid(_ctx(tmp_path), str(tmp_path), str(b), st, 12, [])
    assert seen == {"n": "12b", "name": "tune", "only": "A_P"} and out.endswith("t.kicad_pcb") and "FLUXPLACE_TUNE_ONLY" not in os.environ
    assert PL._retune_relaid(_ctx(tmp_path), str(tmp_path), str(b), {}, 12, []) == str(b)
    assert PL._retune_relaid(_ctx(tmp_path), str(tmp_path), str(b), {"route_scope": {"nets": ["C_N"]}}, 12, []) == str(b)


def test_a_mismatched_pair_inside_its_skew_limit_is_not_relaid(tmp_path, monkeypatch):
    out = str(tmp_path / "b.kicad_pcb")
    open(out, "w").write("(kicad_pcb)")
    json.dump({"pairs": [{"pair": "A_P/A_N", "status": "needs_reroute", "skew": 0.29, "limit": 1.0}]}, open(out + ".tune.json", "w"))
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: (_ for _ in ()).throw(AssertionError("no cli call expected")))
    assert PL._relay_and_retune(_ctx(tmp_path), str(tmp_path), out, ["tune"], "t") == (0, "t")
