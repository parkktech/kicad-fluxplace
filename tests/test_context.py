"""The footprint budgets: this test is what stops the tool silently growing back."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import context_check as CC
import cli


def test_every_budget_holds():
    rows = CC.measure()
    over = [(n, v, b) for n, v, b, _u in rows if v > b]
    assert not over, "context budget exceeded (see `fluxplace doctor --context`): %r" % over
    names = " ".join(n for n, *_ in rows)
    for cmd in ("review", "route-ready", "route-diagnose", "clusters", "anchor-check", "bom-opt",
                "lint", "verify-models", "sourcing", "datasheets", "spec-check", "drc-scope", "eval"):
        assert f"out {cmd}" in names, cmd


def test_doctor_context_cli(capsys):
    import pytest
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor", "--context"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "context: PASS" in out and "mcp tools/list" in out


def test_report_fails_when_over_budget():
    text, ok = CC.report([("x", 10, 5, "B")])
    assert not ok and "FAIL" in text
