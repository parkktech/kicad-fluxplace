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
