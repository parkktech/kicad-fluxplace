"""readychecks T1-T3: ready tells the truth (budgets/density), placement classes gate accept --allow-worse, the stage table and budget arithmetic."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as PG
from fluxplace import ready as RDY, readychecks as KO, routediag as RD, versions as VS, pipeline as PL


# ---- T1
def test_budgets_line_with_a_hard_fail_is_FAIL_and_names_the_row():
    B = RD.load(_two_board())
    cons = {"separation": [{"name": "close", "a": ["R1"], "b": ["R2"], "min_mm": 50.0}]}
    line = RDY.budget_line(None, cons, B, None, None, {})
    assert line["status"] == "FAIL" and "close" in line["detail"] and "hard" in line["detail"]


def test_budgets_line_without_fail_rows_is_PASS():
    line = RDY.budget_line(None, {}, RD.load(_two_board()), None, None, {})
    assert line["status"] == "PASS" and "fail" not in line["detail"].lower()


def test_no_ready_line_is_PASS_with_fail_text(monkeypatch):
    from fluxplace import budget as BG
    soft = BG.row("pair", "P", "length", "mm", value=50.0, target=25.0, mx=40.0, source="s")
    assert soft["verdict"] == "fail" and not soft["hard"]
    monkeypatch.setattr(RDY, "_budget_table", lambda *a: ([soft], {}))
    line = RDY.budget_line(None, {}, None, None, None, {})
    assert line["status"] == "OPEN" and "soft" in line["detail"]


def test_density_warn_is_WARN_status(monkeypatch):
    from fluxplace import density as DN
    rep = {"usage": {"limits": {"fail": 0.65}, "sides": {s: {"util": 0.55} for s in DN.SIDES}},
           "verdicts": {s: {"verdict": "warn"} for s in DN.SIDES}, "line": "density: F 55 %"}
    monkeypatch.setattr(DN, "crowding_report", lambda *a, **k: rep)
    assert RDY.density_line(None, {}, None)["status"] == "WARN"
    rep["verdicts"] = {s: {"verdict": "ok"} for s in DN.SIDES}
    assert RDY.density_line(None, {}, None)["status"] == "PASS"


def _two_board():
    import pathlib, tempfile
    return PG.board(pathlib.Path(tempfile.mkdtemp()), PG.two("R1", 20, 20, "A", "B", court=(-1, -0.5, 1, 0.5)) + PG.two("R2", 22, 20, "A", "C", court=(-1, -0.5, 1, 0.5)))


# ---- T2
def test_placement_line_fails_on_overlap_and_names_the_class(tmp_path):
    txt = PG.two("R1", 20, 20, "A", "B", court=(-1, -0.5, 1, 0.5)) + PG.two("R2", 20.2, 20, "A", "C", court=(-1, -0.5, 1, 0.5))
    B = RD.load(PG.board(tmp_path, txt))
    line, fs = KO.placement_line(B, {}, None)
    assert line["status"] == "FAIL" and fs and line["blockers"]


def test_placement_line_passes_on_a_clean_board(tmp_path):
    txt = PG.two("R1", 20, 20, "A", "B", court=(-1, -0.5, 1, 0.5)) + PG.two("R2", 30, 20, "A", "C", court=(-1, -0.5, 1, 0.5))
    line, fs = KO.placement_line(RD.load(PG.board(tmp_path, txt)), {}, None)
    assert line["status"] == "PASS" and not fs


def test_accept_allow_worse_refuses_on_placement_class_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(KO, "gate_refusal", lambda b, c, s=None: "placement classes FAIL (COLLISION 1)")
    cons = tmp_path / "c.toml"
    cons.write_text("")
    monkeypatch.setattr(VS, "ROOT", str(tmp_path))
    why = VS.placement_refusal({"constraints": "c.toml"}, "b.kicad_pcb")
    assert "COLLISION" in why
    monkeypatch.setattr(KO, "gate_refusal", lambda b, c, s=None: "")
    assert VS.placement_refusal({"constraints": "c.toml"}, "b.kicad_pcb") == ""


def test_accept_without_configured_constraints_says_not_checked(capsys):
    assert VS.placement_refusal({}, "b") == "" and "NOT CHECKED" in capsys.readouterr().out


# ---- T3
def test_stage_table_fails_a_stage_off_only_by_an_unset_key():
    lines, d = KO.stage_lines({"placetour": {"enabled": False}})
    st = lines[0]
    assert st["status"] == "FAIL" and "trace-finish" in st["detail"]
    row = {r["stage"]: r for r in d["stages"]}
    assert row["placetour"]["on"] is False and not row["placetour"]["unset"] and "enabled = false" in row["placetour"]["why"]
    assert row["route"]["on"] and row["trace-finish"]["unset"]


def test_stage_table_enabled_or_noted_off_passes():
    cons = {"finish": {"enabled": True}, "lookahead": {"enabled": False}, "placetour": {"enabled": False}}
    assert KO.stage_lines(cons)[0][0]["status"] == "PASS"
    cons = {"ready": {"stage_off": {"trace-finish": "no copper to clean on this board"}}}
    lines, d = KO.stage_lines(cons)
    assert lines[0]["status"] == "FAIL" and "lookahead" in lines[0]["detail"] and "trace-finish" not in lines[0]["detail"].split("off:")[0]


@pytest.mark.parametrize("stage", sorted(KO.SWITCHES))
def test_every_switch_is_a_real_stage_of_a_flow(stage):
    assert stage in PL.CONSTRUCTIVE or stage in PL.STAGES


def test_pair_budget_arithmetic_names_the_computed_need():
    from fluxplace import pairs as PA
    lines, d = KO.stage_lines({"pairlay": {"budget_min": 20}, "finish": {"enabled": True}}, npairs=21)
    b = [l for l in lines if l["name"] == "stage budgets"][0]
    need = 21 * PA.NODE_CAP_S / 60
    assert b["status"] == "FAIL" and f"{need:.0f} min" in b["detail"] and d["pair_budget"]["need_min"] == need
    lines, _ = KO.stage_lines({"pairlay": {"budget_min": 200}}, npairs=21)
    assert [l for l in lines if l["name"] == "stage budgets"][0]["status"] == "PASS"
