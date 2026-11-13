"""Part-entry gate (fluxplace/parts.py): classification, cross-checks, fix + questions. No network: lookups are injected."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import parts as P
from fluxplace import questions as QS


def comp(ref, value, fp, **kw):
    return dict(ref=ref, value=value, lib="L", fp=fp, **kw)


def spec(*cs, nets=None):
    return {"components": list(cs), "nets": nets or {}}


def codes(res, level=None):
    return sorted((f["code"], f["refs"][0]) for f in res["findings"] if level in (None, f["level"]))


def test_value_is_not_an_mpn_for_electrical_values():
    for v in ("10k", "4.7uH", "100nF", "0R", "1uF", "3V3", "0603", "22pF/50V", "4R7", "1%"):
        assert not P.mpn_like(v), v
    for v in ("ESD2CANFD24DBZRQ1", "SMDJ18A", "BSS84Q", "43650-0315", "TPD4E02B04-Q1"):
        assert P.mpn_like(v), v
    assert P.mpn_in_value("TPD4E02B04-Q1 SIM ESD array") == "TPD4E02B04-Q1"
    assert P.mpn_in_value("M2.5 mount") is None


def test_mpn_in_value_missing_and_generic_codes():
    s = spec(comp("D1", "ESD2CANFD24DBZRQ1", "SOT-23-3"), comp("U1", "mystery chip", "SOIC-8"),
             comp("R1", "10k", "R_0603_1608Metric"), comp("MH1", "M3 hole", "MountingHole_3.2mm"),
             comp("C9", "100nF", "C_0603_1608Metric", dnp=True))
    r = P.run(s, {"parts": {"generic_passives": True}})
    c = codes(r)
    assert ("PART_MPN_IN_VALUE", "D1") in c and ("PART_MPN_MISSING", "U1") in c
    assert ("PART_GENERIC", "R1") in c and not [x for x in c if x[1] in ("MH1", "C9")]
    assert {f["level"] for f in r["findings"] if f["code"] == "PART_GENERIC"} == {"INFO"}
    assert r["skipped"] == {"mechanical": ["MH1"], "dnp": ["C9"]}
    r = P.run(s, {})
    assert {f["level"] for f in r["findings"] if f["code"] == "PART_GENERIC"} == {"FAIL"}
    assert r["unchecked"]["stock"] == 0 and not r["lookup"]          # offline is reported as not checked


def test_chip_size_from_vendor_series_code_vs_footprint():
    s = spec(comp("C36", "1uF", "C_0603_1608Metric", mpn="CC0805KKX7R6BB106"),
             comp("R11", "10R", "R_1206_3216Metric", mpn="RC0603FR-0710RL"),
             comp("C1", "100nF", "C_0402_1005Metric", mpn="GCM155R71H104KE02D"),
             comp("C2", "10uF", "C_1206_3216Metric", mpn="C3216X7R1E106K"))
    c = codes(P.run(s, {}), "FAIL")
    assert ("PART_PACKAGE_MISMATCH", "C36") in c and ("PART_PACKAGE_MISMATCH", "R11") in c
    assert not [x for x in c if x == ("PART_PACKAGE_MISMATCH", "C1") or x == ("PART_PACKAGE_MISMATCH", "C2")]


def test_distributor_package_and_pincount():
    s = spec(comp("J3", "MicroFit-3", "Molex_Micro-Fit_3.0_1x03_P3.00mm_Vertical", mpn="0436500215"),
             comp("U2", "x", "SOT-23-5", mpn="AAA1234"), comp("U3", "x", "SOIC-8_3.9x4.9mm_P1.27mm", mpn="BBB1234"))
    data = {"0436500215": {"matched": "0436500215", "pins": 2, "package": "", "manufacturer": "Molex"},
            "AAA1234": {"matched": "AAA1234", "package": "SC-74A, SOT-753", "manufacturer": "TI"},     # SOT-753 = 5 pins, fits SOT-23-5
            "BBB1234": {"matched": "BBB1234", "package": "8-TSSOP", "manufacturer": "TI"}}
    c = codes(P.run(s, {}, data=data, stock={}), "FAIL")
    assert ("PART_PINCOUNT_MISMATCH", "J3") in c and ("PART_PACKAGE_MISMATCH", "U3") in c
    assert ("PART_PACKAGE_MISMATCH", "U2") not in c and ("PART_PINCOUNT_MISMATCH", "U2") not in c


def test_exposed_pad_is_tolerated():
    s = spec(comp("U1", "x", "QFN-16-1EP_3x3mm_P0.5mm", mpn="QQQ123"))
    facts = {"parts": {"U1": {"pads_total": 17, "pads": 17, "footprint": "QFN-16-1EP_3x3mm_P0.5mm"}}}
    r = P.run(s, {}, facts=facts, data={"QQQ123": {"matched": "QQQ123", "package": "16-VQFN", "pins": 16}}, stock={})
    assert not [f for f in r["findings"] if f["code"] in ("PART_PINCOUNT_MISMATCH", "PART_PACKAGE_MISMATCH")]


def test_pinmap_source_needs_file_and_page(tmp_path):
    (tmp_path / "A.pdf").write_bytes(b"%PDF")
    s = spec(comp("U1", "x", "SOIC-8", mpn="A", pinmap_source="A.pdf#p3"),
             comp("U2", "x", "SOIC-8", mpn="A", pinmap_source="A.pdf"),
             comp("U3", "x", "SOIC-8", mpn="A"),
             comp("U4", "x", "SOIC-8", mpn="A", pinmap_source="gone.pdf#p1"),
             comp("U5", "x", "SOT-23-5", mpn="A", pinmap_source="A.pdf#p16 (ratings), #p14-15"))
    c = codes(P.run(s, {}, ds_dir=str(tmp_path)), "FAIL")
    assert [x for x in c if x[0] == "PART_PINMAP_UNSOURCED"] == [("PART_PINMAP_UNSOURCED", r) for r in ("U2", "U3", "U4")]


def test_stock_grades_and_not_checked():
    s = spec(comp("U1", "x", "SOIC-8", mpn="A"), comp("U2", "x", "SOIC-8", mpn="B"), comp("U3", "x", "SOIC-8", mpn="C"))
    r = P.run(s, {}, data={}, stock={"A": {"verdict": "NONE", "stock": 0}, "B": {"verdict": "LOW", "stock": 3},
                                     "C": {"verdict": "OK", "stock": 900}})
    assert codes(r) .count(("PART_NO_STOCK", "U1")) == 1
    lv = {f["refs"][0]: f["level"] for f in r["findings"] if f["code"] == "PART_NO_STOCK"}
    assert lv == {"U1": "FAIL", "U2": "WARN"}
    r2 = P.run(s, {}, data=None, stock=None)
    assert "PART_NO_STOCK" not in {f["code"] for f in r2["findings"]} and "NOT CHECKED" in P.format_report(r2, "parts")


def test_value_text_naming_another_part_is_flagged():
    s = spec(comp("J3", "MicroFit-3 PWR (43650-0315)", "Molex_1x03", mpn="0436500215"),
             comp("U1", "LM61460 5V", "VQFN-12", mpn="LM61460AASQRJRRQ1"))
    c = codes(P.run(s, {}), "WARN")
    assert ("PART_MPN_VALUE_CONFLICT", "J3") in c and ("PART_MPN_VALUE_CONFLICT", "U1") not in c


def test_fix_writes_only_the_unambiguous_and_asks_the_rest(tmp_path):
    s = spec(comp("D1", "ESD2CANFD24DBZRQ1", "SOT-23-3"), comp("D2", "ESD2CANFD24DBZRQ1", "SOT-23-3"),
             comp("Q1", "BSS138Q", "SOT-23-3"))
    data = {"ESD2CANFD24DBZRQ1": {"matched": "ESD2CANFD24DBZRQ1", "manufacturer": "TI", "package": "SOT-23-3"},
            "BSS138Q": {"matched": "BSS138Q-7-F", "manufacturer": "Diodes", "package": "SOT-23"}}
    sess = QS.Session(str(tmp_path / "b.kicad_pcb"), {}, {}, False, "parts")
    r = P.run(s, {}, data=data, stock={}, session=sess)
    sp2, done = P.apply_fix(s, r, data, sess)
    by = {c["ref"]: c for c in sp2["components"]}
    assert by["D1"]["mpn"] == by["D2"]["mpn"] == "ESD2CANFD24DBZRQ1" and by["D1"]["mfr"] == "TI"
    assert "mpn" not in by["Q1"]                                     # a near match is a question, never a guess
    assert [q["id"] for q in sess.open] == ["part-mpn:BSS138Q"]
    assert "mpn" not in s["components"][0]                           # the input is untouched
    sess2 = QS.Session(str(tmp_path / "b.kicad_pcb"), {}, {"part-mpn:BSS138Q": "record"}, False, "parts")
    sp3, _ = P.apply_fix(s, r, data, sess2)
    assert {c["ref"]: c for c in sp3["components"]}["Q1"]["mpn"] == "BSS138Q" and not sess2.open


def test_cli_parts_exit_codes_and_generic_question(tmp_path, capsys):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(spec(comp("R1", "10k", "R_0603_1608Metric", mpn="RC0603FR-0710KL", mfr="Yageo"),
                                  comp("R2", "1k", "R_0603_1608Metric"))))
    with pytest.raises(SystemExit) as e:
        cli.main(["parts", str(sp), "--no-api"])
    assert e.value.code == 3                                          # the generic-passive decision is a question
    assert os.path.exists(str(tmp_path / "s.questions.json"))
    out = capsys.readouterr().out
    assert "generic-passives" in out and "NOT CHECKED" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["parts", str(sp), "--no-api", "--answer", "generic-passives=accept"])
    assert e.value.code == 1                                          # R1 still lacks a datasheet
    assert (tmp_path / "s.parts.json").exists()


def test_review_runs_the_gate_without_duplicating_codes():
    from fluxplace import review as R
    s = spec(comp("D1", "ESD2CANFD24DBZRQ1", "SOT-23-3"), comp("C36", "1uF", "C_0603_1608Metric", mpn="CC0805KKX7R6BB106"))
    f = R.check_part_entry({"mpn_map": {}, "parts": {}}, s, {}, None, None)
    got = {x["code"] for x in f}
    assert "PART_MPN_IN_VALUE" in got and "PART_PACKAGE_MISMATCH" in got and "PART_MPN_MISSING" not in got
