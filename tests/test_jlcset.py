"""jlcset: JLCPCB BOM/CPL/gerber-zip set (offline; live LCSC lookup stubbed)."""
import csv
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import jlcset as J

FP = '''	(footprint "%(fp)s"
		(layer "%(layer)s")
		(at 1 2 0)
		(property "Reference" "%(ref)s" (at 0 0 0))
		(property "Value" "%(val)s" (at 0 0 0))
		(property "MPN" "%(mpn)s" (at 0 0 0))
		%(attr)s
	)
'''


def _board(tmp_path):
    parts = [
        dict(fp="R_0603_1608Metric", layer="F.Cu", ref="R1", val="10k", mpn="RC1", attr="(attr smd)"),
        dict(fp="R_0603_1608Metric", layer="F.Cu", ref="R2", val="10k", mpn="RC1", attr="(attr smd)"),
        dict(fp="R_0603_1608Metric", layer="F.Cu", ref="R3", val="100k", mpn="RC2", attr="(attr smd dnp)"),
        dict(fp="SOT-23", layer="F.Cu", ref="Q1", val="MMBT", mpn="Q-MPN", attr="(attr smd)"),
        dict(fp="SOT-23", layer="B.Cu", ref="Q2", val="MMBT", mpn="Q-MPN", attr="(attr smd)"),
        dict(fp="Lib:Hole", layer="F.Cu", ref="MH1", val="hole", mpn="", attr="(attr exclude_from_bom)"),
        dict(fp="Conn", layer="F.Cu", ref="J1", val="CONN", mpn="J-MPN", attr="(attr smd)"),
    ]
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb\n" + "".join(FP % d for d in parts) + ")\n")
    pos = tmp_path / "pos.csv"
    rows = [("R1", "R_0603_1608Metric", 1, 1, 0, "top"), ("R2", "R_0603_1608Metric", 2, 1, 90, "top"),
            ("Q1", "SOT-23", 3, 1, 0, "top"), ("Q2", "SOT-23", 4, 1, 30, "bottom"),
            ("MH1", "Hole", 5, 5, 0, "top"), ("J1", "Conn", 6, 1, 0, "top"), ("X9", "Orphan", 7, 7, 0, "top")]
    with open(pos, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"])
        for r, pk, x, y, rot, side in rows:
            w.writerow([r, "v", pk, x, y, rot, side])
    return str(p), str(pos)


def test_rotation_correction_top_bottom_and_last_match_wins():
    rules = J.rotation_rules()
    assert J.correct("R_0603_1608Metric", "top", 1, 1, 30, rules) == (1, 1, 30)
    assert J.correct("SOT-23", "top", 1, 1, 0, rules)[2] == 270        # "^SOT-23" = -90
    assert J.correct("SOT-23-5", "top", 1, 1, 0, rules)[2] == 270
    assert J.correct("SOT-223", "top", 1, 1, 0, rules)[2] == 180      # "^SOT-223" is the later rule; it wins over "^SOT-23"
    assert J.correct("SOT-23", "bottom", 1, 1, 30, rules)[2] == (-(30 + 90) + 180) % 360
    x, y, r = J.correct("PinHeader_2x05_P1.27mm_Vertical", "top", 1, 1, 0, rules)
    assert (x, y, r) == (1 + 2.54, 1 + 0.635, 90)


def test_emit_bom_cpl_dnp_and_consigned(tmp_path):
    board, pos = _board(tmp_path)
    out = tmp_path / "out"
    for d in ("gerbers", "drill"):
        (out / d).mkdir(parents=True)
    for n in ("b-F_Cu.gtl", "b-B_Cu.gbl", "b-In1_Cu.g1", "b-F_Courtyard.gbr", "b-F_Fab.gbr", "b-job.gbrjob", "b-User_1.gbr", "b-Edge_Cuts.gm1"):
        (out / "gerbers" / n).write_text("x")
    for n in ("b-PTH.drl", "b-NPTH.drl", "b-drl_map.gbr"):
        (out / "drill" / n).write_text("x")
    cons = {"sourcing": {"cn_need": 100, "lcsc": {"RC1": "C25804"}, "consign": ["^J1$"]}}
    calls = []

    def check(mpns, need):
        calls.append(sorted(mpns))
        return {"Q-MPN": {"grade": "CN_LOW", "row": {"lcsc": "C81464", "stock": 40}}}

    s = J.emit(board, str(out), pos, cons, None, check, log=lambda *_: None)
    jd = out / "jlcpcb"
    assert calls == [["Q-MPN"]]                       # explicit, consigned and DNP parts are never looked up
    bom = list(csv.reader(open(jd / "BOM-JLCPCB.csv")))
    assert bom[0] == ["Comment", "Designator", "Footprint", "LCSC Part #"]
    assert ["10k", "R1,R2", "R_0603_1608Metric", "C25804"] in bom
    assert ["MMBT", "Q1,Q2", "SOT-23", "C81464"] in bom
    flat = ",".join(r[1] for r in bom[1:])
    assert "R3" not in flat and "MH1" not in flat      # DNP and exclude_from_bom
    cpl = {r[0]: r for r in list(csv.reader(open(jd / "CPL-JLCPCB.csv")))[1:]}
    assert set(cpl) == {"R1", "R2", "Q1", "Q2", "J1"}   # orphan X9 and MH1 dropped: CPL refs == BOM refs
    assert list(csv.reader(open(jd / "CPL-JLCPCB.csv")))[0] == ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]
    assert cpl["Q2"][3] == "Bottom" and cpl["Q1"][3] == "Top" and cpl["R1"][1] == "1.0000mm"
    assert s["consigned"] == ["J1"] and s["thin"] == ["Q1", "Q2"] and s["cpl_missing"] == []
    txt = (jd / "CONSIGNED.txt").read_text()
    assert "J1\tJ-MPN" in txt and "Q1\tQ-MPN\tC81464" in txt
    names = zipfile.ZipFile(jd / "b-gerbers.zip").namelist()
    assert sorted(names) == ["b-B_Cu.gbl", "b-Edge_Cuts.gm1", "b-F_Cu.gtl", "b-In1_Cu.g1", "b-NPTH.drl", "b-PTH.drl"]


def test_previous_bom_notes_supply_lcsc(tmp_path):
    p = tmp_path / "prev.csv"
    p.write_text("Refs,Qty,Value,Footprint,MPN,Manufacturer,Notes\nC1,1,1u,C_0603,CMPN,x,swap; LCSC C342614 7.3k\nC2,1,2u,C_0603,NOPE,x,\n")
    assert J.previous_picks(str(p)) == {"CMPN": "C342614"}


def test_deliver_ships_jlcpcb_set_loose(tmp_path):
    from fluxplace import fab
    fd = tmp_path / "fab"
    for d in ("gerbers", "drill", "place", "jlcpcb"):
        (fd / d).mkdir(parents=True)
    (fd / "gerbers" / "g.gtl").write_text("x")
    (fd / "place" / "pos.csv").write_text("x")
    (fd / "jlcpcb" / "BOM-JLCPCB.csv").write_text("x")
    (fd / "MANIFEST.txt").write_text("FILE         : gerbers/g.gtl\nFILE         : place/pos.csv\nFILE         : jlcpcb/BOM-JLCPCB.csv\n")
    r = fab.deliver(str(fd), str(tmp_path / "d"), "G", centroid_name="c.csv", log=lambda *_: None)
    assert (tmp_path / "d" / "jlcpcb" / "BOM-JLCPCB.csv").is_file()
    assert "jlcpcb" not in " ".join(zipfile.ZipFile(r["zip"]).namelist())
