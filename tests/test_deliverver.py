"""deliver for a NEW VERSION: carried BOM, carried assembly text, README, pins."""
import csv
import os
import re

from fluxplace import deliverver as DV, deps

PREV = """Refs,Qty,Value,Footprint,MPN,Manufacturer,Notes
C1 C2,2,10uF,C_0805,AAA1,Murata,keeps this note
R38,1,100k,R_0603,,,DO NOT POPULATE (D51). land kept
U1,1,IC,SOIC,OLD1,TI,old rationale
X9,1,gone,R_0603,GONE1,Acme,removed part
"""
NEW = """Designators,Qty,Value,Footprint,MPN
"C1,C2",2,10uF,C_0805,AAA1
U1,1,IC,SOIC,NEW1
R7,1,1k,R_0603,BBB2
"""
FPS = [dict(ref=r, lib="L:" + fp, side="F", pads=n, tht_pads=0, pitch=1.0, tht=False, dnp=d)
       for r, fp, n, d in (("C1", "C_0805", 2, False), ("C2", "C_0805", 2, False),
                           ("R38", "R_0603", 2, True), ("U1", "SOIC", 8, False),
                           ("R7", "R_0603", 2, False), ("J5", "Conn", 12, False))]


def _bom(tmp_path, prev=PREV, new=NEW):
    p, n, o = tmp_path / "p.csv", tmp_path / "n.csv", tmp_path / "o.csv"
    p.write_text(prev); n.write_text(new)
    rep = DV.carry_bom(str(p), str(n), str(o), label="v2", prev_label="v1",
                       footprints=FPS, log=lambda m: None)
    return rep, list(csv.DictReader(open(o)))


def _rows(tmp_path):
    _bom(tmp_path)
    return DV.read_bom(str(tmp_path / "o.csv"))


def test_bom_previous_format_carries_manufacturer_notes_and_keeps_dnp(tmp_path):
    rep, rows = _bom(tmp_path)
    assert list(rows[0]) == DV.BOM_COLS
    by = {r["Refs"]: r for r in rows}
    assert by["C1 C2"]["Manufacturer"] == "Murata" and by["C1 C2"]["Notes"] == "keeps this note"
    assert by["R38"]["Notes"].startswith("DO NOT POPULATE") and by["R38"]["MPN"] == ""
    assert by["R7"]["Notes"] == "NEW in v2"
    assert by["U1"]["Notes"].startswith("CHANGED in v2") and by["U1"]["Manufacturer"] == ""
    assert "X9" not in by and rep["removed"] == [("X9", "GONE1")]


def test_board_dnp_without_a_previous_line_gets_a_bom_line(tmp_path):
    rep, rows = _bom(tmp_path, prev=PREV.replace("R38,", "R99,"))
    assert any(r["Refs"] == "R38" and "DO NOT POPULATE" in r["Notes"] for r in rows)


def _new_md(dnp="R38"):
    return ("# Assembly instructions — v2\n\nGenerated x.\n\n## 1. Board\n\n| a |\n\n"
            "## 2. Do not populate\n\n**%s — DO NOT FIT.** lands\n\n## 3. Process notes\n\n- x\n\n"
            "## 4. Parts we supply (do not buy these)\n\nNone — every line on the BOM is a turnkey buy.\n\n"
            "## 5. Substitutions — the rule\n\nShort.\n" % dnp)


PREV_MD = (_new_md() + "\n## 6. J5 header\n\nFit the J5 12-pin header by hand. Keep spec-v1.json.\n\n"
           "## 7. Notes\n\n- **U1** order note\n")


def test_assembly_keeps_hand_written_sections_and_refreshes_pin_counts(tmp_path):
    rows = _rows(tmp_path)
    fps = [dict(p, pads=14) if p["ref"] == "J5" else p for p in FPS]
    md = DV.carry_assembly(_new_md(), PREV_MD, rows, fps, "- moved U1", "v2", "v1", log=lambda m: None)
    assert "## 6. J5 header" in md and "J5 14-pin header" in md and "J5 12-pin" not in md
    assert "spec-v2.json" in md and "## 7. Notes" in md
    assert re.search(r"## 8\. What changed since v1\n\n- moved U1", md)
    assert DV.claims(md, rows, fps) == []


def test_assembly_never_claims_a_line_the_bom_lacks(tmp_path):
    rows = [r for r in _rows(tmp_path) if "R38" not in r["Refs"]]
    md = DV.carry_assembly(_new_md(), PREV_MD, rows, FPS, "", "v2", "v1", log=lambda m: None)
    assert any("R38" in c and "DNP" in c for c in DV.claims(md, rows, FPS))
    md2 = md.replace("None — every line on the BOM is a turnkey buy.",
                     "| MPN | Why | Used by |\n|---|---|---|\n| ZZZ9 | lead | U9 |")
    assert any("ZZZ9" in c for c in DV.claims(md2, rows, FPS))


def test_supply_table_keeps_previous_rows_still_on_the_bom(tmp_path):
    rows = _rows(tmp_path)
    prev = PREV_MD.replace("None — every line on the BOM is a turnkey buy.",
                           "| MPN | Why | Used by |\n|---|---|---|\n| AAA1 | lead | C1 C2 |\n| GONE1 | lead | X9 |")
    md = DV.carry_assembly(_new_md(), prev, rows, FPS, "", "v2", "v1", log=lambda m: None)
    assert "| AAA1 |" in md and "GONE1" not in md


def test_readme_carries_unregenerated_sections_and_names_the_previous_version():
    prev = ("HEAD - V1\n=====\nintro\n\nTHE RULES (unchanged)\n---------------------\n 1. keep\n\n"
            "WHAT CHANGED SINCE V0\n---------------------\nold\n\nCONTENTS\n--------\nold list\n")
    t = DV.readme_first("v2", "v1", "proj", "board", "- new thing", "DRC ok", "a\nb", prev)
    assert "WHAT CHANGED SINCE V1\n" in t and "- new thing" in t
    assert "THE RULES (unchanged)" in t and "1. keep" in t
    assert "old list" not in t and "\nold\n" not in t and "a\nb" in t


def test_version_label():
    assert DV.version_label("utv-comms-v1.5.3-fab") == "v1.5.3"
    assert DV.version_label("/x/delivery-v1.5") == "v1.5"


def test_docx_xlsx_deps_are_pinned_and_named_by_doctor():
    req = open(os.path.join(os.path.dirname(__file__), "..", "requirements.txt")).read()
    assert re.search(r"^python-docx==\d", req, re.M) and re.search(r"^openpyxl==\d", req, re.M)
    pins = {d["key"]: d.get("pip") for d in deps.REQUIREMENTS} if hasattr(deps, "REQUIREMENTS") else {}
    assert ("==" in pins["python-docx"] and "==" in pins["openpyxl"])


def test_schematic_is_never_regenerated_or_substituted(tmp_path):
    b = tmp_path / "b"; b.mkdir()
    (b / "x.kicad_pcb").write_text("")
    a = type("A", (), {"sch": None})()
    assert DV._find_sch(a, str(b), "x") is None
    (b / "x.kicad_sch").write_text("s")
    assert DV._find_sch(a, str(b), "x") == str(b / "x.kicad_sch")
    other = tmp_path / "real.kicad_sch"; other.write_text("s")
    a.sch = str(other)
    assert DV._find_sch(a, str(b), "x") == str(other)


def test_project_bundles_every_referenced_model_into_the_delivery(tmp_path):
    """k8: models named by ${KIPRJMOD}/../lib, a stock KiCad path and a path under another library all land in <out>/lib/3dmodels and the
    delivered board points at them; the previous delivery's partial lib does not hide the missing ones."""
    src = tmp_path / "src"
    (src / "lib" / "3dmodels" / "kicad" / "Cap").mkdir(parents=True)
    (src / "lib" / "3dmodels" / "kicad" / "Cap" / "C.step").write_text("c")
    (src / "lib" / "3dmodels" / "J.step").write_text("jj")
    stock = tmp_path / "stock" / "R.3dshapes"
    stock.mkdir(parents=True)
    (stock / "R.step").write_text("r")
    (src / "lib" / "3dmodels" / "MOLEX_43650-0315.step").write_text("m")
    (src / "stage").mkdir()
    board = src / "stage" / "b.kicad_pcb"
    board.write_text('(kicad_pcb\n (footprint "A" (model "${KIPRJMOD}/../lib/3dmodels/kicad/Cap/C.step"))\n'
                     ' (footprint "B" (model "${KIPRJMOD}/../lib/3dmodels/J.step"))\n'
                     ' (footprint "C" (model "${KICAD10_3DMODEL_DIR}/R.3dshapes/R.step"))\n'
                     ' (footprint "E" (model "${KICAD10_3DMODEL_DIR}/Conn.3dshapes/Molex_Micro-Fit_43650-0315_1x03.step"))\n'
                     ' (footprint "D" (model "${KIPRJMOD}/../lib/3dmodels/gone.step")))\n')
    prev = tmp_path / "prev" / "lib"
    (prev / "3dmodels").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    os.environ["KICAD10_3DMODEL_DIR"] = str(tmp_path / "stock")
    try:
        logs = []
        DV.project(str(board), str(out), [str(src / "lib"), str(prev)], log=logs.append)
    finally:
        del os.environ["KICAD10_3DMODEL_DIR"]
    txt = (out / "b.kicad_pcb").read_text()
    for rel in ("kicad/Cap/C.step", "J.step", "kicad/R.3dshapes/R.step"):
        assert "${KIPRJMOD}/lib/3dmodels/" + rel in txt and (out / "lib" / "3dmodels" / rel).is_file()
    assert "${KIPRJMOD}/lib/3dmodels/MOLEX_43650-0315.step" in txt          # unresolved stock path -> the project library's copy
    assert "${KIPRJMOD}/lib/3dmodels/gone.step" in txt and not (out / "lib" / "3dmodels" / "gone.step").exists()   # unresolved: left, reported
    assert any("MODEL_BUNDLE_UNRESOLVED" in m for m in logs)


def test_no_bom_builds_board_bom_with_dnp(tmp_path):
    fps = [dict(FPS[0], value="10uF"), dict(FPS[1], value="10uF"), dict(FPS[2], value="100k"), dict(FPS[3], value="IC", mpn="U-1")]
    rows = DV.board_bom("b.kicad_pcb", fps, {"C1": "AAA1", "C2": "AAA1"})
    by = {tuple(r["Refs"]): r for r in rows}
    assert by[("C1", "C2")]["MPN"] == "AAA1" and by[("C1", "C2")]["Qty"] == "2"
    assert DV.is_dnp(by[("R38",)]) and by[("U1",)]["MPN"] == "U-1"
    p = DV.write_bom(rows, str(tmp_path / "o.csv"))
    assert {r["Refs"][0] for r in DV.read_bom(p)} == {"C1", "R38", "U1"}


def test_verify_line_with_baseline_counts_new_and_inherited():
    f = [{"level": "FAIL", "baseline": "inherited"}, {"level": "WARN", "baseline": "inherited"},
         {"level": "WARN", "baseline": "new"}]
    line = DV._verify_line(f, "/nonexistent", baseline="/x/W15.kicad_pcb")
    assert "0 new FAIL vs W15.kicad_pcb; 1 inherited FAIL, 1 inherited WARN" in line
    assert "FAIL," in DV._verify_line(f, "/nonexistent") and "new FAIL" not in DV._verify_line(f, "/nonexistent")
