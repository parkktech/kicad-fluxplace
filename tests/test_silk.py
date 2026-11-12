"""silk.py: check codes and the reference-text fit on small synthetic boards.
Needs pcbnew under /usr/bin/python3 (silk_helper.py); every test skips when it is not importable there."""
import json, os, subprocess, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import silk as SK, boardwrite as BW

EFF = '(effects (font (size 1 1) (thickness 0.15)){})'


def _have_pcbnew():
    return subprocess.run([SK.PY, "-c", "import pcbnew"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _have_pcbnew(), reason="pcbnew not importable under " + SK.PY)


def part(ref, x, y, side="F", ref_at=(0, -2), ref_h=1.0, w=2.0, h=1.0, pads=True, hide=False, extra="", angle=0,
         mask=True, ref_layer=None, hole=None):
    s = side
    mir = " (justify mirror)" if s == "B" else ""
    lay = ref_layer or f"{s}.SilkS"
    eff = f'(effects (font (size {ref_h} {ref_h}) (thickness 0.15)){mir}{" hide" if False else ""})'
    pd = ""
    if pads:
        ml = f' "{s}.Mask"' if mask else ""
        pd = "".join(f'(pad "{i + 1}" smd rect (at {dx} 0) (size 0.8 0.8) (layers "{s}.Cu"{ml}))'
                     for i, dx in enumerate((-w / 2 + 0.4, w / 2 - 0.4)))
    if hole:
        pd += f'(pad "" np_thru_hole circle (at 0 0) (size {hole} {hole}) (drill {hole}) (layers "*.Cu" "*.Mask"))'
    vis = "" if not hide else " (hide yes)"
    return (f'(footprint "X" (layer "{s}.Cu") (at {x} {y} {angle}) '
            f'(property "Reference" "{ref}" (at {ref_at[0]} {ref_at[1]} 0) (layer "{lay}"){vis} {eff}) '
            f'(property "Value" "v" (at 0 3 0) (layer "{s}.Fab") (hide yes) {eff}) '
            f'(fp_rect (start {-w / 2 - 0.2} {-h / 2 - 0.2}) (end {w / 2 + 0.2} {h / 2 + 0.2}) '
            f'(stroke (width 0.05) (type solid)) (fill none) (layer "{s}.CrtYd")) {pd} {extra})\n')


HEAD = ('(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0") (general (thickness 1.6))\n'
        '(paper "A4") (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (9 "F.Adhes" user "F.Adhesive") '
        '(11 "B.Adhes" user "B.Adhesive") (13 "F.Paste" user) (15 "B.Paste" user) (5 "F.SilkS" user "F.Silkscreen") '
        '(7 "B.SilkS" user "B.Silkscreen") (1 "F.Mask" user) (3 "B.Mask" user) (25 "Edge.Cuts" user) '
        '(31 "F.CrtYd" user "F.Courtyard") (29 "B.CrtYd" user "B.Courtyard") (35 "F.Fab" user) (33 "B.Fab" user))\n'
        '(setup (pad_to_mask_clearance 0))\n')


def brd(tmp, body, name="b.kicad_pcb", outline=(0, 0, 40, 30)):
    x0, y0, x1, y1 = outline
    p = tmp / name
    p.write_text(HEAD + f'(gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type solid)) (fill none) '
                 f'(layer "Edge.Cuts"))\n' + body + ")\n")
    return str(p)


def codes(p, cons=None, side="both"):
    sc, cfg, f = SK.run_check(p, cons, side)
    return sc, cfg, f, {c: sum(1 for x in f if x["code"] == c) for c in SK.CODES}


def test_off_board(tmp_path):
    p = brd(tmp_path, part("R1", 20, 15) + part("R2", 20, 0.5, ref_at=(0, -2)))   # R2 text at y=-1.5: outside
    _, _, f, c = codes(p)
    assert c["SILK_OFF_BOARD"] == 1 and "R2" in f[[x["code"] for x in f].index("SILK_OFF_BOARD")]["msg"]


def test_over_pad_and_overlap(tmp_path):
    # R1's text sits on R2's pad; R3's text sits on R1's text
    p = brd(tmp_path, part("R1", 10, 15, ref_at=(0, -4)) + part("R2", 10, 11, pads=True, ref_at=(0, 3))
            + part("R3", 30, 15, ref_at=(0, -2)) + part("R4", 30, 15, ref_at=(0.3, -2), pads=False))
    _, _, _, c = codes(p)
    assert c["SILK_OVER_PAD"] >= 1 and c["SILK_OVERLAP"] >= 1


def test_over_hole(tmp_path):
    p = brd(tmp_path, part("R1", 10, 15, ref_at=(0, 0), pads=False) + part("H1", 10, 15, pads=False, hole=1.5,
                                                                         ref_at=(0, 6)))
    _, _, _, c = codes(p)
    assert c["SILK_OVER_HOLE"] == 1


def test_under_part(tmp_path):
    p = brd(tmp_path, part("R1", 10, 15, ref_at=(20, 0), pads=False) + part("U1", 30, 15, w=6, h=6, pads=False,
                                                                           ref_at=(0, 6)))
    # R1's text is at (30, 15): inside U1's courtyard
    _, _, _, c = codes(p)
    assert c["SILK_UNDER_PART"] == 1


def test_too_small_and_far(tmp_path):
    p = brd(tmp_path, part("R1", 10, 15, ref_h=0.5, pads=False, ref_at=(0, -2)) + part("R2", 30, 15, pads=False,
                                                                                     ref_at=(0, -8)))
    _, _, _, c = codes(p)
    assert c["SILK_TOO_SMALL"] == 1 and c["SILK_FAR_FROM_PART"] == 1


def test_ref_hidden_levels(tmp_path):
    p = brd(tmp_path, part("R1", 10, 15, pads=False, ref_layer="F.Fab") + part("J1", 30, 15, pads=False,
                                                                              ref_layer="F.Fab"))
    _, _, f, c = codes(p)
    lv = {x["refs"][0]: x["level"] for x in f if x["code"] == "SILK_REF_HIDDEN"}
    assert lv == {"R1": "INFO", "J1": "WARN"}


def test_missing_mark(tmp_path):
    p = brd(tmp_path, part("U1", 20, 15, w=6, h=6, pads=True, ref_at=(0, -4)))
    _, _, _, c = codes(p)
    assert c["SILK_MISSING_MARK"] == 1


def test_fit_fixes_off_board_ref(tmp_path):
    p = brd(tmp_path, part("R1", 20, 0.8, ref_at=(0, -2)))
    sc, cfg, before = SK.run_check(p)
    assert SK.counts(before)["SILK_OFF_BOARD"] == 1
    out = str(tmp_path / "o.kicad_pcb")
    b, a, res = SK.fix_board(p, out)
    assert not SK.fails(a) and res["moved"] == ["R1"] and not res["hidden"]


def test_fit_separates_overlapping_refs(tmp_path):
    p = brd(tmp_path, part("R1", 18, 15, ref_at=(0, -2)) + part("R2", 22, 15, ref_at=(-4, -2)))
    _, _, before = SK.run_check(p)
    assert SK.counts(before)["SILK_OVERLAP"] == 1
    out = str(tmp_path / "o.kicad_pcb")
    _, a, res = SK.fix_board(p, out)
    assert SK.counts(a).get("SILK_OVERLAP", 0) == 0 and not SK.fails(a)


def _boxed():
    # R1 sits in a 6x6 mm pocket ringed by a 'wall' of pad-covered parts, so every candidate within 4 mm is bad
    body = part("R1", 20, 15, w=1.0, h=0.6, pads=False, ref_at=(0, 0))
    k = 0
    for dx in range(-8, 9, 2):
        for dy in range(-8, 9, 2):
            if abs(dx) <= 1 and abs(dy) <= 1:
                continue
            k += 1
            body += part(f"W{k}", 20 + dx, 15 + dy, w=2.4, h=2.0, pads=False, ref_at=(0, 40),
                         extra='(pad "1" smd rect (at 0 0) (size 3.4 2.8) (layers "F.Cu" "F.Mask"))', ref_layer="F.Fab")
    return body


def test_boxed_in_ref_is_hidden_and_reported(tmp_path):
    p = brd(tmp_path, _boxed(), outline=(0, 0, 40, 30))
    out = str(tmp_path / "o.kicad_pcb")
    _, a, res = SK.fix_board(p, out, cons={"silk": {"max_ref_distance_mm": 2.0}})
    assert "R1" in res["hidden"]
    sc = SK.load_scene(out)
    assert not any(t["owner"] == "R1" and t["kind"] == "ref" for t in sc["texts"])      # off silk ...
    assert 'property "Reference" "R1"' in open(out).read()                               # ... still on the board


def test_b_side_mirroring(tmp_path):
    p = brd(tmp_path, part("R1", 20, 0.8, side="B", ref_at=(0, -2)))
    out = str(tmp_path / "o.kicad_pcb")
    _, a, res = SK.fix_board(p, out)
    assert res["moved"] == ["R1"] and not SK.fails(a)
    t = next(t for t in SK.load_scene(out)["texts"] if t["owner"] == "R1")
    assert t["side"] == "B" and t["mirror"] is True and (t["angle"] % 360) in (0.0, 270.0)   # readable from the back


def test_deterministic_and_geometry_untouched(tmp_path):
    p = brd(tmp_path, part("R1", 18, 15, ref_at=(0, -2)) + part("R2", 22, 15, ref_at=(-4, -2))
            + part("R3", 20, 0.8, ref_at=(0, -2)))
    o1, o2 = str(tmp_path / "1.kicad_pcb"), str(tmp_path / "2.kicad_pcb")
    SK.fix_board(p, o1)
    SK.fix_board(p, o2)
    key = lambda sc: sorted((t["owner"], t["kind"], t["pos"], t["angle"], t["h"]) for t in sc["texts"])
    assert key(SK.load_scene(o1)) == key(SK.load_scene(o2))             # (pcbnew assigns random uuids: compare texts)
    srt = lambda v: sorted(json.dumps(x, sort_keys=True) for x in v)
    a, b = SK.load_scene(p), SK.load_scene(o1)
    assert [(t["owner"], t["kind"]) for t in a["texts"]] != [] and srt(a["pads"]) == srt(b["pads"]) and srt(a["courts"]) == srt(b["courts"])


def test_waivable_in_review(tmp_path):
    from fluxplace import review as R
    p = brd(tmp_path, part("R1", 20, 0.8, ref_at=(0, -2)))
    facts = {"board_path": p}
    out = R.check_silk(facts, {})
    assert any(f["code"] == "SILK_OFF_BOARD" and f["level"] == "FAIL" for f in out)
    assert R.check_silk(facts, {"silk": {"enabled": False}}) == []
    assert all(R._waived(f, ["SILK_OFF_BOARD:^R1 "]) for f in out if f["code"] == "SILK_OFF_BOARD")


def test_auto_fit_hook_after_a_placement_write(tmp_path, capsys):
    p = brd(tmp_path, part("R1", 20, 15, ref_at=(0, -2)))
    out = str(tmp_path / "moved.kicad_pcb")
    BW.set_post_write(lambda path, refs: SK.after_write(path, refs, {}))
    try:
        BW.move_file(p, out, {"R1": (20, 0.8, 0)})          # the placement pushes R1 against the edge
    finally:
        BW.set_post_write(None)
    line = capsys.readouterr().out
    assert line.startswith("silk:") and "->" in line
    _, _, f = SK.run_check(out)
    assert SK.fails(f) == 0
    # auto_fit off: one summary line, board text untouched
    BW.set_post_write(lambda path, refs: SK.after_write(path, refs, {"silk": {"auto_fit": False}}))
    try:
        BW.move_file(p, str(tmp_path / "m2.kicad_pcb"), {"R1": (20, 0.8, 0)})
    finally:
        BW.set_post_write(None)
    assert "auto_fit off" in capsys.readouterr().out


def test_cli_exit_code_and_report(tmp_path):
    p = brd(tmp_path, part("R1", 20, 0.8, ref_at=(0, -2)))
    cli = os.path.join(os.path.dirname(__file__), "..", "cli.py")
    r = subprocess.run([sys.executable, cli, "silk", p], capture_output=True, text=True)
    assert r.returncode == 1 and "SILK_OFF_BOARD" in r.stdout
    o = str(tmp_path / "fixed.kicad_pcb")
    r = subprocess.run([sys.executable, cli, "silk", p, "--fix", "--out", o], capture_output=True, text=True)
    assert r.returncode == 0 and os.path.exists(o), r.stdout + r.stderr


# ------------------------------------------------------------------ title / version text
def gtext(txt, x, y, side="F", h=1.5):
    mir = " (justify mirror)" if side == "B" else ""
    return (f'(gr_text "{txt}" (at {x} {y} 0) (layer "{side}.SilkS") '
            f'(effects (font (size {h} {h}) (thickness 0.18)){mir}))\n')


TITLE = {"silk": {"title": {"company": "Parkktech.com"}}}


def tb(tmp, body, name="demo-v16.kicad_pcb", outline=(0, 0, 40, 30)):
    return brd(tmp, body, name=name, outline=outline)


def titles(sc, side="F"):
    return [t for t in sc["texts"] if t["kind"] == "board" and t["side"] == side]


def test_version_sources_and_stale(tmp_path):
    body = gtext("Parkktech.com v1.5", 20, 8)
    p = tb(tmp_path, body)                                           # file name demo-v16 -> 1.6
    _, cfg, f, c = codes(p)
    assert c["SILK_VERSION_STALE"] == 1 and cfg["_version"][1] == "board file name"
    assert "V1.5" in f[0]["msg"] and "V1.6" in f[0]["msg"] and "file name" in f[0]["msg"]
    p2 = tb(tmp_path, body, name="plain.kicad_pcb")                  # no version anywhere: silent
    assert codes(p2)[3]["SILK_VERSION_STALE"] == 0
    cons = {"silk": {"title": {"version": "1.5"}}}                   # explicit [silk.title] version wins over the name
    _, cfg, f, c = codes(p, cons)
    assert c["SILK_VERSION_STALE"] == 0 and cfg["_version"][1] == "[silk.title] version"
    _, cfg, f, c = codes(p, {"project": {"version": "1.7"}})
    assert c["SILK_VERSION_STALE"] == 1 and "[project]" in cfg["_version"][1]
    sc, cfg = SK.prepare(p2, {}, {"name": "x", "version": "V1.4"})
    assert cfg["_version"][0] == (1, 4) and "spec" in cfg["_version"][1]
    assert any(x["code"] == "SILK_VERSION_STALE" for x in SK.check(sc, cfg))


def test_two_board_texts_disagree(tmp_path):
    p = tb(tmp_path, gtext("Acme V1.6", 10, 8) + gtext("Acme v1.5", 10, 20), name="plain.kicad_pcb")
    f = codes(p)[2]
    assert [x["code"] for x in f].count("SILK_VERSION_STALE") == 1 and "disagree" in f[0]["msg"]


def test_title_missing_is_fail_when_required(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15, pads=False))
    f = codes(p, TITLE)[2]
    m = [x for x in f if x["code"] == "SILK_TITLE_MISSING"]
    assert len(m) == 1 and m[0]["level"] == "FAIL"
    cons = {"silk": {"title": {"company": "Parkktech.com", "required": False}}}
    assert [x for x in codes(p, cons)[2] if x["code"] == "SILK_TITLE_MISSING"][0]["level"] == "WARN"


def test_no_title_table_and_no_version_text_is_silent(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15, pads=False) + gtext("hello", 20, 5), name="plain.kicad_pcb")
    f = codes(p)[2]
    assert not [x for x in f if x["code"] in ("SILK_VERSION_STALE", "SILK_TITLE_MISSING", "SILK_TITLE_NO_ROOM")]


def test_title_is_named_in_overlap_findings(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15, ref_at=(0, 0), pads=True) + gtext("Parkktech.com V1.6", 20, 15))
    f = codes(p, TITLE)[2]
    assert any("title text" in x["msg"] and "TITLE" in x["refs"] for x in f if x["code"] in ("SILK_OVER_PAD", "SILK_OVERLAP"))


def test_fix_rewrites_and_moves_the_title(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15, ref_at=(0, 3)) + gtext("Parkktech.com v1.5", 20, 15))   # on R1's pads
    out = str(tmp_path / "o.kicad_pcb")
    b, a, res = SK.fix_board(p, out, cons=TITLE)
    assert any(x["code"] == "SILK_VERSION_STALE" for x in b) and not [x for x in a if x["level"] == "FAIL"], a
    sc = SK.load_scene(out)
    ts = titles(sc)
    assert [t["text"] for t in ts] == ["Parkktech.com  V1.6"]
    assert res["title"]["F"]["h"] >= 1.0 and res["title"]["F"]["margin_mm"] > 0
    assert (SK.counts(a).get("SILK_OVER_PAD", 0) == 0 and SK.counts(a).get("SILK_OVERLAP", 0) == 0)


def test_fix_creates_a_missing_title_and_is_deterministic(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15, ref_at=(0, 3)))
    o1, o2 = str(tmp_path / "1.kicad_pcb"), str(tmp_path / "2.kicad_pcb")
    SK.fix_board(p, o1, cons=TITLE)
    SK.fix_board(p, o2, cons=TITLE)
    key = lambda o: sorted((t["text"], t["pos"], t["angle"], t["h"]) for t in titles(SK.load_scene(o)))
    assert key(o1) == key(o2) and key(o1)[0][0] == "Parkktech.com  V1.6"
    assert not [x for x in SK.run_check(o1, TITLE)[2] if x["level"] == "FAIL"]


def test_title_no_room(tmp_path):
    # a 40 x 6 board cannot hold a 1.0 mm title that is 60 chars long in either direction
    p = tb(tmp_path, "", outline=(0, 0, 8, 6))
    cons = {"silk": {"title": {"company": "A very long company name that cannot possibly fit"}}}
    out = str(tmp_path / "o.kicad_pcb")
    b, a, res = SK.fix_board(p, out, cons=cons)
    nr = [x for x in a if x["code"] == "SILK_TITLE_NO_ROOM"]
    assert len(nr) == 1 and nr[0]["level"] == "FAIL" and "largest free rectangle" in nr[0]["msg"]
    assert not titles(SK.load_scene(out))                              # nothing shrunk below the minimum, nothing written


def test_title_on_the_back_is_mirrored(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15, ref_at=(0, 3), side="B"))
    cons = {"silk": {"title": {"company": "Parkktech.com", "side": "B"}}}
    out = str(tmp_path / "o.kicad_pcb")
    b, a, res = SK.fix_board(p, out, cons=cons)
    t = titles(SK.load_scene(out), "B")
    assert len(t) == 1 and t[0]["mirror"] is True and t[0]["text"] == "Parkktech.com  V1.6"
    assert not [x for x in a if x["level"] == "FAIL"], a
    assert not titles(SK.load_scene(out), "F")


def test_post_write_hook_prints_the_stale_version(tmp_path, capsys):
    p = tb(tmp_path, part("R1", 20, 15, pads=False) + gtext("Parkktech.com v1.5", 20, 5))
    out = str(tmp_path / "demo-v16-moved.kicad_pcb")
    BW.set_post_write(lambda path, refs: SK.after_write(path, refs, {}))
    try:
        BW.move_file(p, out, {"R1": (22, 15, 0)})
    finally:
        BW.set_post_write(None)
    assert "SILK_VERSION_STALE" in capsys.readouterr().out


@pytest.mark.parametrize("txt", ["3V3", "1V8", "5V", "12V", "+3.3V", "24V", "V+", "VBAT", "VIN", "-5V", "V1", "V12",
                                 "3V3 5V", "VCC12", "REV", "Vref1"])
def test_version_rule_ignores_voltage_and_net_labels(txt):
    assert SK.version_tokens(txt) == []


@pytest.mark.parametrize("txt,want", [("V1.6", ["1.6"]), ("Acme V1.6", ["1.6"]), ("rev 2", ["2"]), ("Rev1.0.1", ["1.0.1"]),
                                      ("REV 3.1", ["3.1"]), ("Board v2", ["2"]), ("(V1.5)", ["1.5"])])
def test_version_rule_matches_real_tokens(txt, want):
    assert SK.version_tokens(txt) == want


def test_lone_v_integer_counts_only_with_title_or_company():
    assert SK.version_tokens("V1") == []
    assert SK.version_tokens("V1", is_title=True) == ["1"]
    assert SK.version_tokens("V1", company="V1") == ["1"]


def test_net_labels_never_trigger_stale(tmp_path):
    body = "".join(gtext(t, 5 + 5 * i, 8) for i, t in enumerate(["3V3", "1V8", "V1", "VBAT", "+3.3V", "24V"]))
    p = tb(tmp_path, body)                                           # file name says 1.6
    assert codes(p)[3]["SILK_VERSION_STALE"] == 0


def test_bare_board_without_constraints_runs(tmp_path):
    p = tb(tmp_path, part("R1", 20, 15) + gtext("Acme V1.5", 5, 5), name="plain.kicad_pcb")
    sc, cfg, f, c = codes(p, None)
    assert cfg["title"] is None and c["SILK_TITLE_MISSING"] == 0 and c["SILK_VERSION_STALE"] == 0
