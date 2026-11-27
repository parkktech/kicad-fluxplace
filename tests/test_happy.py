"""B15: the kicad-happy adapter parses recorded analyzer output; a missing plugin/schematic/simulator is UNKNOWN, never a pass; every adapter
code has an owner stage in gapmap; findings are warn-only until the constraints promote a code."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import gapmap as GM, happy as HP, ready as RDY, review as RV  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures", "happy")


def _j(n):
    with open(os.path.join(FX, n)) as fh:
        return json.load(fh)


def test_parse_recorded_emc_output():
    raw = HP.parse_emc(_j("emc-sample.json"))
    by = {r["rule"]: r for r in raw}
    assert set(by) == {"DC-001", "ES-001", "SU-001", "GP-001", "IO-002"}
    assert by["DC-001"]["code"] == "EMC_DC_001" and by["DC-001"]["severity"] == "error" and by["DC-001"]["refs"]
    assert by["DC-001"]["msg"].startswith("DC-001 Decoupling cap")


def test_emc_findings_are_warn_only_and_info_is_not_a_finding():
    fs = HP.as_findings(HP.parse_emc(_j("emc-sample.json")), {"happy": {}})
    assert fs and {f["level"] for f in fs} == {"WARN"}
    assert "EMC_GP_001" not in {f["code"] for f in fs}            # the sample's GP-001 is severity info


def test_promotion_by_code_and_by_prefix():
    cons = {"happy": {"fail": ["EMC_DC_001", "EMC_SU_"]}}
    lv = {f["code"]: f["level"] for f in HP.as_findings(HP.parse_emc(_j("emc-sample.json")), cons)}
    assert lv["EMC_DC_001"] == "FAIL" and lv["EMC_SU_001"] == "FAIL" and lv["EMC_ES_001"] == "WARN"


def test_parse_recorded_spice_output_skips_passes():
    raw = HP.parse_spice(_j("spice-sample.json"))
    assert {r["code"] for r in raw} == {"SPICE_CRYSTAL_CIRCUIT", "SPICE_LC_FILTER"}
    assert all(r["severity"] == "warning" for r in raw)
    assert any("Y300" in r["refs"] or r["refs"] for r in raw)


def test_no_happy_table_means_not_analysed():
    assert HP.settings({}) is None and HP.check_emc({"board": "x.kicad_pcb"}, {}) == []
    assert RDY.spice_lines("x.kicad_pcb", {}, {}) == []


def test_missing_plugin_is_unknown_never_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(HP, "skills_dir", lambda cfg=None: None)
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    fs = HP.check_emc({"board_path": str(b)}, {"happy": {}})
    assert [f["code"] for f in fs] == ["EMC_UNKNOWN"] and "NOT CHECKED" in fs[0]["msg"] and fs[0]["level"] == "WARN"
    ln = RDY.spice_lines(str(b), {"happy": {}}, {})[0]
    assert ln["status"] == RDY.UNCHECKED and "plugin" in ln["detail"]


def test_missing_schematic_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(HP, "skills_dir", lambda cfg=None: str(tmp_path))
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    r = HP.run_emc(str(b), {"happy": {}})
    assert r["status"] == HP.UNKNOWN and "schematic" in r["reason"]


def test_missing_simulator_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(HP, "skills_dir", lambda cfg=None: str(tmp_path))
    monkeypatch.setattr(HP, "simulator", lambda: None)
    s = tmp_path / "x.kicad_sch"
    s.write_text("(kicad_sch)")
    r = HP.run_spice(None, {"happy": {"schematic": str(s)}})
    assert r["status"] == HP.UNKNOWN and "simulator" in r["reason"]


def test_a_failing_analyzer_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(HP, "skills_dir", lambda cfg=None: str(tmp_path))
    monkeypatch.setattr(HP, "_run", lambda args, t: (False, "boom"))
    s, b = tmp_path / "x.kicad_sch", tmp_path / "x.kicad_pcb"
    s.write_text("(kicad_sch)")
    b.write_text("(kicad_pcb)")
    r = HP.run_emc(str(b), {"happy": {"schematic": str(s)}})
    assert r["status"] == HP.UNKNOWN and "boom" in r["reason"]


def test_schematic_found_beside_the_board(tmp_path):
    (tmp_path / "p.kicad_sch").write_text("")
    assert HP.find_schematic(str(tmp_path / "p.kicad_pcb"), {"happy": {}}) == str(tmp_path / "p.kicad_sch")


def test_every_adapter_code_has_an_owner_stage():
    cs = HP.codes()
    assert len(cs) > 60 and GM.missing(list(cs)) == []
    assert all(c in GM.fail_codes() for c in cs)                   # promotable codes are in the totality scan
    assert GM.owner("EMC_DC_001") == "floorplan" and GM.kind("EMC_DC_001") == "support-far"
    assert GM.kind("EMC_DP_001") == "pair-skew" and GM.owner("SPICE_CRYSTAL_CIRCUIT") == "ready"
    assert GM.owner("EMC_SOME_NEW_RULE") == GM.OWNER_DATA          # an unknown rule id still has an owner
