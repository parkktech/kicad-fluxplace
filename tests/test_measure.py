"""B3a: measure record (board sha, rules sha, status), content-sha cache, approx/failed measures cannot pass a gated stage,
zone-inclusive keys recorded (not enforced), summary row code, 5-run repeatability helper, no-.kicad_pro refusal."""
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import drcguard as DG, opengate as OG, pipeline as PL

ZONE_V = {"type": "clearance", "severity": "error", "items": [{"description": "Zone [GND] on F.Cu"}, {"description": "Track [A] on F.Cu"}]}
TRK_V = {"type": "clearance", "severity": "error", "items": [{"description": "Track [A] on F.Cu"}, {"description": "Track [B] on F.Cu"}]}


def _fake_drc(monkeypatch, report=None):
    calls = []

    def fake(board, kicad_cli="kicad-cli", timeout=600, **kw):
        calls.append(board)
        return report or {"violations": [], "unconnected_items": []}
    monkeypatch.setattr(DG, "drc_json", fake)
    DG._CACHE.clear()
    return calls


def test_edited_dru_gives_a_new_measure_not_a_cache_hit(tmp_path, monkeypatch):
    b = G.board(tmp_path, "")
    stem = b[:-len(".kicad_pcb")]
    open(stem + ".kicad_pro", "w").write("{}")
    open(stem + ".kicad_dru", "w").write("(version 1)\n")
    calls = _fake_drc(monkeypatch)
    m1 = DG.measure(b)
    DG.measure(b)
    assert len(calls) == 1                                           # same content: a hit
    open(stem + ".kicad_dru", "w").write("(version 1)\n(rule x (constraint clearance (min 0.3mm)))\n")
    m2 = DG.measure(b)
    assert len(calls) == 2 and m2["rules_sha"] != m1["rules_sha"] and m2["board_sha"] == m1["board_sha"]
    open(stem + ".kicad_pro", "w").write('{"a": 1}')                 # the project file is a rule source too
    assert DG.measure(b)["rules_sha"] != m2["rules_sha"] and len(calls) == 3
    assert m1["status"] == "exact" and DG.exact(m1)


def test_same_content_at_new_mtime_is_a_hit_and_new_content_a_miss(tmp_path, monkeypatch):
    b = G.board(tmp_path, "")
    calls = _fake_drc(monkeypatch)
    DG.measure(b)
    os.utime(b, (1, 1))
    DG.measure(b)
    assert len(calls) == 1
    open(b, "a").write("\n")
    DG.measure(b)
    assert len(calls) == 2


def test_kicad_cli_absent_is_failed_not_exact(tmp_path):
    b = G.board(tmp_path, "")
    DG._CACHE.clear()
    m = DG.measure_record(b, kicad_cli=str(tmp_path / "no-such-kicad-cli"))
    assert m["status"] == "failed" and not DG.exact(m) and m["board_sha"] and "error" in m


def test_route_ready_does_not_pass_on_an_approx_measure(tmp_path, monkeypatch):
    b = G.board(tmp_path, "")
    monkeypatch.setattr(PL, "_run_cli", lambda argv, log: (0, ""))
    monkeypatch.setattr(PL, "_collision_report", lambda *a, **k: None)
    monkeypatch.setattr(PL, "_scorecard", lambda *a, **k: ({"open": 0, "open_approx": True}, [], {}))
    c = type("C", (), {"a": type("A", (), {"waive": [], "force": False})(), "cs": lambda s: [], "dir": lambda s, n, name: str(tmp_path), "cons": {}, "spec": None})()
    r = PL.st_route_ready(c, 3, b)
    assert r["verdict"] != "pass"
    monkeypatch.setattr(PL, "_scorecard", lambda *a, **k: ({"open": 0}, [], {}))
    assert PL.st_route_ready(c, 3, b)["verdict"] == "pass"


def test_open_count_marks_the_fallback_approx(tmp_path, monkeypatch):
    monkeypatch.setattr(OG, "unconnected", lambda board: (_ for _ in ()).throw(RuntimeError("no report")))
    monkeypatch.setattr(PL.RC, "text_open", lambda B: {"N": 2})
    flag = []
    assert PL._open_count("x", None, flag) == 2 and flag == [False]


def test_zone_inclusive_keys_are_recorded_beside_the_unchanged_ones():
    m = DG.measure_from_report({"violations": [ZONE_V, TRK_V], "unconnected_items": []})
    assert m["errors"] == 1 and sum(m["keys"].values()) == 2                      # today: zone item in the keys, not in `errors` (B3b enforces)
    assert m["errors_zone"] == 2 and m["keys_zone"] == m["keys"]
    a = DG.measure_from_report({"violations": [ZONE_V, TRK_V], "unconnected_items": []}, severity="all")
    assert sum(a["keys"].values()) == 1 and sum(a["keys_zone"].values()) == 2 and a["errors"] == 1 and a["errors_zone"] == 2


def test_summary_row_has_its_own_code():
    m = {"fresh": 1, "saved": 1, "cli_fresh": 22, "stale": [], "connections": OG.connections({"N": [[{"ref": "A", "num": "1", "x": 0, "y": 0, "layers": ["F.Cu"]}], [{"ref": "B", "num": "1", "x": 3, "y": 0, "layers": ["F.Cu"]}]]})}
    f = OG.findings(m)
    assert sum(x["code"] == "UNROUTED" for x in f) == len(m["connections"])
    s = [x for x in f if "open connection(s)" in x["msg"]]
    assert len(s) == 1 and s[0]["code"] == "UNROUTED_SUMMARY"
    assert OG.waived(s[0], ["UNROUTED"]) and not OG.waived(s[0], [r"UNROUTED:^N$"])


def test_repeatability_helper_compares_zone_inclusive_multisets(tmp_path, monkeypatch):
    b = G.board(tmp_path, "")
    open(b[:-len(".kicad_pcb")] + ".kicad_pro", "w").write("{}")
    seen = []
    reports = iter([{"violations": [ZONE_V, TRK_V]}] * 5)

    def fake(board, *a, **k):
        seen.append(board)
        return next(reports)
    monkeypatch.setattr(DG, "drc_json", fake)
    r = DG.repeatability(b, runs=5)
    assert r["identical"] and r["runs"] == 5 and len(set(seen)) == 5 and b not in seen          # a fresh copy each run
    assert r["errors_zone"] == [2] * 5
    flick = iter([{"violations": [TRK_V]}, {"violations": [TRK_V, ZONE_V]}])
    monkeypatch.setattr(DG, "drc_json", lambda board, *a, **k: next(flick))
    r = DG.repeatability(b, runs=2)
    assert not r["identical"] and r["errors_zone"] == [1, 2]


def test_pipeline_refuses_a_board_with_no_kicad_pro(tmp_path):
    from test_pipeline import _args
    b = G.board(tmp_path, "")
    pro = b[:-len(".kicad_pcb")] + ".kicad_pro"
    if os.path.exists(pro):
        os.remove(pro)
    with pytest.raises(SystemExit) as e:
        PL.run(_args(tmp_path, b, from_stage="fanout", to_stage="fanout", flow="constructive"))
    assert "kicad_pro" in str(e.value) and "REFUSED" in str(e.value)
