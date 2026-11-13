"""Drawing-sheet title block: TITLEBLOCK_EMPTY / TITLEBLOCK_REV_STALE, fill, the name question."""
import argparse, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import titleblock as TB
from fluxplace import review as R
from fluxplace import questions as QS

HEAD = '(kicad_pcb (version 20260206) (generator "pcbnew")\n\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper "A4")\n'
TAIL = '\t(layers (0 "F.Cu" signal))\n\t(gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts"))\n)\n'
BLOCK = '\t(title_block\n\t\t(title "Demo")\n\t\t(date "2026-01-01")\n\t\t(rev "1.6")\n\t\t(company "Acme")\n\t\t(comment 1 "keep me")\n\t)\n'
CONS = {"silk": {"title": {"company": "Acme Co"}}}


def mk(tmp_path, block="", name="demo-v16.kicad_pcb"):
    p = tmp_path / name
    p.write_text(HEAD + block + TAIL)
    return str(p)


def codes(f):
    return [x["code"] for x in f]


def test_empty_when_no_block_or_missing_title_or_rev(tmp_path):
    f = TB.findings(TB.read(mk(tmp_path)), mk(tmp_path), CONS)
    assert codes(f) == ["TITLEBLOCK_EMPTY"] and f[0]["level"] == "WARN" and "no title block at all" in f[0]["msg"]
    p = mk(tmp_path, '\t(title_block\n\t\t(title "Demo")\n\t)\n')
    f = TB.findings(TB.read(p), p, CONS)
    assert codes(f) == ["TITLEBLOCK_EMPTY"] and "no revision" in f[0]["msg"] and "no title" not in f[0]["msg"]
    p = mk(tmp_path, '\t(title_block\n\t\t(rev "1.6")\n\t)\n')
    assert "no title" in TB.findings(TB.read(p), p, CONS)[0]["msg"]


@pytest.mark.parametrize("stage,lvl", [("review", "WARN"), ("fab", "FAIL"), ("deliver", "FAIL"), ("pcbway", "FAIL")])
def test_empty_is_fail_at_fab_deliver_pcbway(tmp_path, stage, lvl):
    p = mk(tmp_path)
    assert TB.findings(TB.read(p), p, CONS, stage=stage)[0]["level"] == lvl


def test_rev_stale_is_fail_and_matching_forms_are_not(tmp_path):
    p = mk(tmp_path, BLOCK.replace('"1.6"', '"1.5"'))                 # file name says v16
    f = TB.findings(TB.read(p), p, CONS)
    assert codes(f) == ["TITLEBLOCK_REV_STALE"] and f[0]["level"] == "FAIL" and "1.5" in f[0]["msg"] and "1.6" in f[0]["msg"]
    for ok in ("1.6", "V1.6", "rev 1.6", "1.6.0"):
        q = mk(tmp_path, BLOCK.replace('"1.6"', f'"{ok}"'))
        assert TB.findings(TB.read(q), q, CONS) == [], ok
    # a declared version beats the file name (one resolution: silk's)
    p = mk(tmp_path, BLOCK)
    assert codes(TB.findings(TB.read(p), p, {"project": {"version": "2.0"}})) == ["TITLEBLOCK_REV_STALE"]


def test_filled_correct_block_is_silent_in_check_and_review(tmp_path):
    p = mk(tmp_path, BLOCK)
    assert TB.findings(TB.read(p), p, CONS, stage="fab") == []
    f = {"board_path": p, "title_block": TB.read(p)}
    assert R.check_title_block(f, CONS) == []
    assert R.check_title_block({"board_path": p}, CONS) == []             # no facts -> silent


def test_review_wires_the_check_and_its_stage(tmp_path):
    p = mk(tmp_path)
    base = dict(board_path=p, title_block=TB.read(p), net_pads={}, net_tracks={}, size_mm=(10, 10), copper_layers=4,
                plane_layers=[], layer_track_mm={}, stackup=[], vias=[], via_type_counts={}, track_geom=[],
                pth_pads=[], dup_bare_pads=[], design_min={}, parts={})
    by = {f["code"]: f["level"] for f in R.run(dict(base), cons=CONS)}
    assert by["TITLEBLOCK_EMPTY"] == "WARN"
    by = {f["code"]: f["level"] for f in R.run(dict(base), cons=CONS, stage="fab")}
    assert by["TITLEBLOCK_EMPTY"] == "FAIL"


def test_fill_writes_rev_date_company_title_and_changes_nothing_else(tmp_path):
    p = mk(tmp_path)
    before = open(p).read()
    cons = {"project": {"name": "Demo Box"}, "silk": {"title": {"company": "Parkktech.com"}}}
    r = TB.fill(p, None, cons, None, date="2026-11-12")
    after = open(p).read()
    assert r["after"] == {"present": True, "title": "Demo Box", "date": "2026-11-12", "rev": "1.6",
                          "company": "Parkktech.com", "comments": {}} and r["missing"] == []
    # byte-identical outside the inserted block
    sp = TB.block_span(after)
    assert after[:sp[0]].rstrip("\t") + after[sp[1] + 1:] == before
    assert TB.findings(TB.read(p), p, cons, stage="fab") == []             # re-check is clean
    TB.fill(p, None, cons, None, date="2026-11-12")                       # idempotent
    assert open(p).read() == after


def test_fill_title_from_spec_name_and_company_never_invented(tmp_path):
    p = mk(tmp_path)
    r = TB.fill(p, None, {}, {"name": "spec-name"}, date="2026-11-12")
    assert r["after"]["title"] == "spec-name" and r["after"]["company"] == ""        # no company declared -> none written
    assert TB.company({}) is None and TB.company({"project": {"company": "P"}}) == "P"


def test_fill_keeps_existing_title_company_and_comments_but_fixes_a_stale_rev(tmp_path):
    p = mk(tmp_path, BLOCK.replace('"1.6"', '"1.5"'))
    r = TB.fill(p, None, {"project": {"name": "Other"}, "silk": {"title": {"company": "Else"}}}, None, date="2026-11-12")
    a = r["after"]
    assert (a["title"], a["company"], a["rev"], a["date"], a["comments"]) == ("Demo", "Acme", "1.6", "2026-11-12", {1: "keep me"})
    assert open(p).read().count("(title_block") == 1


def test_fill_to_out_leaves_the_input_alone(tmp_path):
    p = mk(tmp_path)
    raw = open(p).read()
    TB.fill(p, str(tmp_path / "o.kicad_pcb"), {"project": {"name": "N"}}, None, date="2026-11-12")
    assert open(p).read() == raw and "(title_block" in open(tmp_path / "o.kicad_pcb").read()


def test_missing_name_is_a_question_and_nothing_is_written(tmp_path, capsys):
    p = mk(tmp_path)
    raw = open(p).read()
    S = QS.Session(p, {}, {}, False, "title-block")
    with pytest.raises(SystemExit) as e:
        TB.fill(p, None, {}, None, date="2026-11-12", session=S)
    assert e.value.code == 3 and open(p).read() == raw
    out = capsys.readouterr().out
    assert "title-block-name" in out and "[project] name" in out
    # answered: proceeds, title stays empty, rev/date written
    S = QS.Session(p, {}, {"title-block-name": "leave-title-empty"}, False, "title-block")
    r = TB.fill(p, None, {}, None, date="2026-11-12", session=S)
    assert r["after"]["title"] == "" and r["after"]["rev"] == "1.6" and r["missing"] == ["title"]
    assert codes(TB.findings(TB.read(p), p, {})) == ["TITLEBLOCK_EMPTY"]


def test_pcbnew_reads_the_filled_block_and_geometry_is_untouched(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    from pcbnew import FromMM as F, VECTOR2I as V
    b = pcbnew.BOARD()
    net = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(net)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference("R1")
    b.Add(fp)
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1"); pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD); pad.SetSize(V(F(1), F(1)))
    pad.SetLayerSet(pcbnew.PAD.SMDMask()); pad.SetNet(net)
    fp.Add(pad); fp.SetPosition(V(F(5), F(5)))
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(V(F(5), F(5))); t.SetEnd(V(F(9), F(5))); t.SetWidth(F(0.2)); t.SetLayer(pcbnew.F_Cu); t.SetNet(net)
    b.Add(t)
    p = str(tmp_path / "demo-v16.kicad_pcb")
    pcbnew.SaveBoard(p, b)

    def geo(path):
        bb = pcbnew.LoadBoard(path)
        return ([(f.GetReference(), f.GetPosition().x, f.GetPosition().y, [(q.GetNumber(), q.GetNetname()) for q in f.Pads()])
                 for f in bb.GetFootprints()],
                [(x.GetStart().x, x.GetEnd().x, x.GetNetname(), x.GetWidth()) for x in bb.GetTracks()], len(list(bb.Zones())))
    g0 = geo(p)
    TB.fill(p, None, {"project": {"name": "N"}, "silk": {"title": {"company": "C"}}}, None, date="2026-11-12")
    tb = pcbnew.LoadBoard(p).GetTitleBlock()
    assert (tb.GetTitle(), tb.GetRevision(), tb.GetCompany(), tb.GetDate()) == ("N", "1.6", "C", "2026-11-12")
    assert geo(p) == g0
