"""Audits say INCOMPLETE (exit 1 unless allowed), never a bare pass, when part of the design was not covered."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import review as R, audit as AU, routeaudit as RA, qualaudit as QA


def _f(level, code, msg="m"):
    return {"level": level, "code": code, "msg": msg, "refs": []}


def test_summarize_names_what_was_not_covered():
    out = []
    n = R.summarize([_f("INFO", "ELEC_NOT_EVALUATED", "U1 no data"), _f("WARN", "PREROUTE_UNAVAILABLE")], out.append)
    assert n["incomplete"] == 2 and n["fail"] == 0
    assert any(l.startswith("review: INCOMPLETE - not covered: ELEC_NOT_EVALUATED x1, PREROUTE_UNAVAILABLE x1") for l in out)


def test_summarize_complete_has_no_incomplete_line():
    out = []
    assert R.summarize([_f("WARN", "SOMETHING")], out.append)["incomplete"] == 0
    assert not any("INCOMPLETE" in l for l in out)


def _run_review(monkeypatch, findings, *extra):
    monkeypatch.setattr(cli, "_review_gate", lambda a, stage="review", hard=False: findings)
    cli.main(["review", "--board", "x.kicad_pcb", *extra])


def test_review_incomplete_exits_1(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _run_review(monkeypatch, [_f("INFO", "SILK_SKIPPED")])
    assert e.value.code == 1


def test_review_incomplete_allowed(monkeypatch):
    _run_review(monkeypatch, [_f("INFO", "SILK_SKIPPED")], "--allow-incomplete")
    _run_review(monkeypatch, [_f("INFO", "SILK_SKIPPED")], "--fail-on", "never")


def test_review_clean_exits_0(monkeypatch):
    _run_review(monkeypatch, [_f("INFO", "OTHER")])


def test_waived_incomplete_code_is_covered():
    facts_findings = [_f("INFO", "SILK_SKIPPED", "silkscreen check not run")]
    kept = [f for f in facts_findings if not R._waived(f, ["SILK_SKIPPED:.*"])]
    assert R.incomplete(kept) == []


def test_drc_scope_unknown_severities_is_incomplete(tmp_path, capsys):
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    with pytest.raises(SystemExit) as e:
        cli.main(["drc-scope", "--board", str(b)])
    assert e.value.code == 1
    assert "INCOMPLETE" in capsys.readouterr().out
    cli.main(["drc-scope", "--board", str(b), "--allow-incomplete"])


def test_drc_scope_full_severities_is_clean(tmp_path):
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rule_severities": {"clearance": "error"}}}}))
    cli.main(["drc-scope", "--board", str(b)])


def test_drc_scope_missing_report_is_incomplete(tmp_path):
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rule_severities": {"clearance": "error"}}}}))
    assert "not found" in AU.drc_scope(str(b), str(tmp_path / "nope.json"))["incomplete"][0]


def test_drc_types_report_without_violations_list_raises(tmp_path, monkeypatch):
    def fake(cmd, **k):
        open(cmd[cmd.index("--output") + 1], "w").write("{}")
    monkeypatch.setattr(RA.subprocess, "run", fake)
    with pytest.raises(RuntimeError):
        RA.drc_types("b.kicad_pcb")


def test_not_done_reports_ungraded_pairs(monkeypatch):
    rows = [{"pair": "A/B", "p": "A", "n": "B", "status": "unknown", "causes": [], "why": "ps limit, no delay", "skew": None, "limit": None}]
    monkeypatch.setattr(QA, "pair_rows", lambda *a: rows)
    monkeypatch.setattr(QA, "rf_rows", lambda *a: [])
    nd = QA.not_done({}, {}, {}, set(), ctx=([], set(), None))
    assert nd["ungraded"] == [{"pair": "A/B", "cause": "ps limit, no delay"}] and nd["nets"] == set()
