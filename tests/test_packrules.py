"""docs/PACKAGING-RULES.md gate: one test per rule + readiness semantics."""
import json
import os
import types

from fluxplace import packrules as PR, deliverver as DV, procs

BOARD = '''(kicad_pcb (version 20240108)
(title_block (title "T") (rev "1.5.3"))
(gr_text "Proj v1.5.3" (at 0 0) (layer "F.SilkS"))
(footprint "A:R" (layer "F.Cu") (property "Reference" "R1") (pad "1" smd rect (at 0 0))
  (model "${KIPRJMOD}/m.step"))
)'''


import pytest


@pytest.fixture(autouse=True)
def _no_real_drc(monkeypatch):
    def fake(cmd, **k):
        json.dump({"violations": [], "unconnected_items": []}, open(cmd[cmd.index("-o") + 1], "w"))
    monkeypatch.setattr(procs, "run", fake)


def _ctx(tmp_path, board=BOARD, **kw):
    out = tmp_path / "o"; out.mkdir(exist_ok=True)
    (out / "m.step").write_text("x")
    b = out / "b.kicad_pcb"; b.write_text(board)
    c = dict(out=str(out), board=str(b), cons={"project": {"version": "1.5.3"}}, findings=[],
             kicad_cli="kicad-cli", prev_board=None, prev_bom=None, prev_label=None, bom_rows=[],
             dnp=[], board_refs={"R1"}, claims=[], removed=[], missing_datasheets=[], prev_known_open=[])
    c.update(kw)
    return c


def _status(res, rule):
    return next(r for r in res if r["rule"] == rule)


def test_p1_p2_p4_hooks_missing_are_not_checked_never_pass(tmp_path, monkeypatch):
    import fluxplace.tune as T
    monkeypatch.delattr(T, "interface_pair_check", raising=False)
    res = PR.check(_ctx(tmp_path, prev_board="x.kicad_pcb"))
    assert _status(res, "P1")["status"] == "NOT CHECKED"
    assert _status(res, "P2")["status"] == "NOT CHECKED"
    assert _status(res, "P4")["status"] == "NOT CHECKED"
    assert not PR.ready(res)


def test_p1_hook_fail_warn_and_waiver(tmp_path, monkeypatch):
    import fluxplace.tune as T
    monkeypatch.setattr(T, "interface_pair_check", lambda b, c: {"fail": ["ETH0 skew 40 mm"], "warn": ["ETH1 13 mm > target"]}, raising=False)
    r = _status(PR.check(_ctx(tmp_path)), "P1")
    assert r["status"] == "FAIL" and r["warns"] == ["ETH1 13 mm > target"]
    r = _status(PR.check(_ctx(tmp_path), ["P1:ETH0: IEEE 802.3 allows it, source X"]), "P1")
    assert r["status"] == "PASS" and r["waived"][0][1].startswith("IEEE")


def test_p2_first_version_plain_review_and_baseline_hook(tmp_path, monkeypatch):
    assert _status(PR.check(_ctx(tmp_path)), "P2")["status"] == "PASS"
    bad = [{"level": "FAIL", "code": "X", "refs": ["U1"]}]
    assert _status(PR.check(_ctx(tmp_path, findings=bad)), "P2")["status"] == "FAIL"
    assert _status(PR.check(_ctx(tmp_path, findings=None)), "P2")["status"] == "NOT CHECKED"
    import fluxplace.review as R
    monkeypatch.setattr(R, "baseline_check", lambda *a: {"fail": ["new FAIL Y"], "warn": []}, raising=False)
    assert _status(PR.check(_ctx(tmp_path, prev_board="p.kicad_pcb")), "P2")["status"] == "FAIL"


def test_p3_drc_errors_and_unconnected_fail(tmp_path, monkeypatch):
    def fake(cmd, **k):
        out = cmd[cmd.index("-o") + 1]
        json.dump(fake.data, open(out, "w"))
        assert "--refill-zones" in cmd and "--severity-all" in cmd
    monkeypatch.setattr(procs, "run", fake)
    fake.data = {"violations": [], "unconnected_items": []}
    assert _status(PR.check(_ctx(tmp_path)), "P3")["status"] == "PASS"
    fake.data = {"violations": [{"severity": "error", "type": "clearance"}], "unconnected_items": [1]}
    r = _status(PR.check(_ctx(tmp_path)), "P3")
    assert r["status"] == "FAIL" and "clearance" in r["reason"] and "unconnected" in r["reason"]


def _rows(*notes):
    return [dict(Refs=[r], Qty="1", Value="", Footprint="", MPN=m, Manufacturer=mf, Notes=n)
            for r, m, mf, n in notes]


def test_p4_pcbway_set_and_dnp_excluded_from_centroid(tmp_path):
    c = _ctx(tmp_path, dnp=["R9"], bom_rows=_rows(("R9", "", "", "DO NOT POPULATE")))
    assert "PCBWay set incomplete" in _status(PR.check(c), "P4")["reason"]
    o = tmp_path / "o"
    (o / "1-GERBERS-x.zip").write_text("z"); (o / "2-BOM-x.csv").write_text("Refs\nR9\n")
    (o / "3-CENTROID-x.csv").write_text("Ref\nR9\n")
    assert "DNP R9 is in the PCBWay centroid" in _status(PR.check(c), "P4")["reason"]


def test_p5_dropped_manufacturer_notes_and_missing_dnp(tmp_path):
    prev = tmp_path / "prev.csv"
    prev.write_text("Refs,Qty,Value,Footprint,MPN,Manufacturer,Notes\nU1,1,x,f,AAA,TI,why\nR38,1,1,f,,,DO NOT POPULATE\n")
    c = _ctx(tmp_path, prev_bom=str(prev), board_refs={"R38", "U1"},
             bom_rows=_rows(("U1", "AAA", "", "")))
    r = _status(PR.check(c), "P5")
    assert r["status"] == "FAIL" and "Manufacturer dropped" in r["reason"] and "DNP line R38 missing" in r["reason"]
    c["bom_rows"] = _rows(("U1", "AAA", "TI", "why"), ("R38", "", "", "DO NOT POPULATE"))
    assert _status(PR.check(c), "P5")["status"] == "PASS"


def test_p6_manifest_known_open_and_datasheets(tmp_path):
    c = _ctx(tmp_path, missing_datasheets=["ABC123"], prev_label="v1",
             prev_known_open=["T3 pin table must be ticked"])
    r = _status(PR.check(c), "P6")
    assert r["status"] == "FAIL" and "datasheet missing: ABC123" in r["all"] and "missing: README-FIRST" in r["all"]
    r = _status(PR.check(c, ["P6:ABC123: consigned part, datasheet is behind a login (debt D9)"]), "P6")
    assert "datasheet missing: ABC123" not in r["all"] and r["waived"]
    assert PR.known_open("KNOWN OPEN\n----------\n  - one\n    cont\n  - two\n\nCONTENTS\n--------\nx\n") == ["one cont", "two"]


def test_p7_title_block_and_silk_version(tmp_path):
    assert _status(PR.check(_ctx(tmp_path)), "P7")["status"] == "PASS"
    r = _status(PR.check(_ctx(tmp_path, board=BOARD.replace('(rev "1.5.3")', '(rev "1.5")'))), "P7")
    assert r["status"] == "FAIL" and "title-block" in r["reason"]
    r = _status(PR.check(_ctx(tmp_path, board=BOARD.replace("v1.5.3", "v1.5"))), "P7")
    assert r["status"] == "FAIL" and "silk" in r["reason"]
    c = _ctx(tmp_path); c["cons"] = {}
    assert "no [project] version" in _status(PR.check(c), "P7")["reason"]


def test_p8_missing_model_is_fail_unless_debt_row(tmp_path):
    b = BOARD.replace("${KIPRJMOD}/m.step", "${KIPRJMOD}/gone.step")
    assert _status(PR.check(_ctx(tmp_path, board=b)), "P8")["status"] == "FAIL"
    r = _status(PR.check(_ctx(tmp_path, board=b), ["P8:R1: model debt D77, vendor STEP login-walled"]), "P8")
    assert r["status"] == "PASS" and r["waived"]


def test_waiver_without_reason_is_rejected_and_not_ready_marker(tmp_path):
    res = PR.check(_ctx(tmp_path, board=BOARD.replace("m.step", "gone.step")), ["P8:R1"])
    assert any(r["rule"] == "--" for r in res)
    PR.mark(str(tmp_path / "o"), res)
    assert os.path.exists(tmp_path / "o" / "NOT-READY")
    assert "NOT-READY" in PR.readme_block(res)


def test_known_open_status_lines_in_readme():
    prev = "KNOWN OPEN\n----------\n  - T3 HX5084 pin table\n  - J1 gender pending\n\nCONTENTS\n--------\nx\n"
    s = DV.known_open_status(prev, "- T3 HX5084 pin table fixed against datasheet")
    assert "CLOSED per --changes" in s and "STILL OPEN" in s
