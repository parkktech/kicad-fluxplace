"""Review-A fixes: pad angles on rotated / flipped footprints (KiCad-verified), trapezoid pads, unnetted overlaps,
pour-island severity, the guard's try/finally + UNCHECKED path + read-only skip, the pipeline ledger's unreadable
stage, and block membership (explicit only, BlockError at the caller boundary, spec passed to review, --write)."""
import argparse, json, math, os, sys
import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
import cli
from fluxplace import routediag as RD, legality as LGL, geom as GM, blocks as BK, review as RV, placechecks as PCK
from fluxplace import pipeline as PL


def _load(tmp_path, body, name="b.kicad_pcb", pro=None):
    p = G.board(tmp_path, body, name=name)
    if pro is not None:
        (tmp_path / name.replace(".kicad_pcb", ".kicad_pro")).write_text(json.dumps(pro))
    return RD.load(p), p


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


def _overlaps(B):
    return [x for x in LGL.collisions(B) if "copper_overlap" in x["kinds"]]


# ---- fix 1: pad angle is absolute in the file (KiCad 10 writes it that way; DRC-verified) -----------------------
@pytest.mark.parametrize("side", ["F", "B"])
@pytest.mark.parametrize("ang", [0, 90, 180, 270, 45])
def test_rotated_footprint_pad_copper_matches_kicad(tmp_path, ang, side):
    # U1: 2.0 x 0.2 pad along the footprint's x axis, file pad angle = footprint angle (absolute, as KiCad writes it).
    # KiCad's angle is counter-clockwise on a y-down screen, so the pad's long axis is (cos a, -sin a).
    # The neighbour sits on that axis: 1.5 mm out is clear (pad ends at 1.0), 0.9 mm out is copper overlap.
    def pad(num, net, w, h, a, dx=0.0, dy=0.0):
        return f'(pad "{num}" smd rect (at {dx} {dy} {a}) (size {w} {h}) (layers "F.Cu") (net "{net}"))'
    c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    for d, want in ((1.5, False), (0.9, True)):
        u = G.fp("U1", 20, 20, [pad("1", "A", 2.0, 0.2, ang)], angle=ang, side=side)
        j = G.fp("J1", 20 + d * c, 20 - d * s, [pad("1", "X", 0.2, 0.2, 0)], side=side)
        B, _ = _load(_sub(tmp_path, f"d{d}"), u + j)
        assert bool(_overlaps(B)) is want, (ang, side, d)


# ---- fix 7: trapezoid pads use the true polygon (corners checked against pcbnew's effective polygon) ----------
def test_trapezoid_pad_polygon_matches_kicad_corners(tmp_path):
    body = G.fp("U1", 20, 20, ['(pad "1" smd trapezoid (at 0 0 0) (size 2 1) (rect_delta 0.4 0.2) (layers "F.Cu") (net "A"))'])
    B, _ = _load(tmp_path, body)
    pts, r = GM.pad_shape([q for q in B["pads"] if q["ref"] == "U1"][0])
    assert r == 0
    want = [(-1.1, 0.7), (1.1, 0.3), (0.9, -0.3), (-0.9, -0.7)]          # pcbnew GetEffectivePolygon, relative to (20,20)
    for wx, wy in want:
        assert min(math.hypot(px - (20 + wx), py - (20 + wy)) for px, py in pts) < 1e-6


def test_trapezoid_cut_corner_is_clear_of_a_rect_neighbour(tmp_path):
    # the bounding rectangle reaches y=-0.5 at x=0.8, but the trapezoid's bottom edge is at y=-0.31 there: clear
    u = G.fp("U1", 20, 20, ['(pad "1" smd trapezoid (at 0 0 0) (size 2 1) (rect_delta 0.4 0.2) (layers "F.Cu") (net "A"))'])
    j = G.fp("J1", 20.8, 19.5, ['(pad "1" smd rect (at 0 0 0) (size 0.1 0.1) (layers "F.Cu") (net "X"))'])
    B, _ = _load(tmp_path, u + j)
    assert not _overlaps(B)
    j2 = G.fp("J1", 20.0, 20.0, ['(pad "1" smd rect (at 0 0 0) (size 0.1 0.1) (layers "F.Cu") (net "X"))'])
    B2, _ = _load(_sub(tmp_path, "in"), u + j2)
    assert _overlaps(B2)


# ---- fix 6: pads with no net on either side are a WARN, a netted pair stays a FAIL ------------------------------
def test_unnetted_pad_overlap_is_warn_not_fail(tmp_path):
    u = G.fp("U1", 20, 20, ['(pad "1" smd rect (at 0 0 0) (size 1 1) (layers "F.Cu"))'])
    j = G.fp("J1", 20.2, 20, ['(pad "1" smd rect (at 0 0 0) (size 1 1) (layers "F.Cu"))'])
    B, _ = _load(tmp_path, u + j)
    f = [x for x in LGL.collisions(B) if x["pair"] == ["J1", "U1"]]
    assert len(f) == 1 and f[0]["levels"]["unnetted_overlap"] == "WARN" and "copper_overlap" not in f[0]["kinds"]
    assert not _overlaps(B)
    u2 = G.fp("U2", 30, 20, ['(pad "1" smd rect (at 0 0 0) (size 1 1) (layers "F.Cu") (net "A"))'])
    j2 = G.fp("J2", 30.2, 20, ['(pad "1" smd rect (at 0 0 0) (size 1 1) (layers "F.Cu"))'])
    B2, _ = _load(_sub(tmp_path, "n"), u2 + j2)
    assert [x["levels"]["copper_overlap"] for x in LGL.collisions(B2) if x["pair"] == ["J2", "U2"]] == ["FAIL"]


# ---- fix 4: pour islands are WARN unless [review] pour_island = "fail" ---------------------------------------
ZONE = ('(zone (net "GND") (layer "F.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 90 0) (xy 90 70) (xy 0 70)))'
        ' (filled_polygon (layer "F.Cu") (pts (xy 60 60) (xy 70 60) (xy 70 65) (xy 60 65))))\n')


def test_pour_island_is_warn_by_default_and_fail_when_asked(tmp_path):
    c = G.fp("C1", 15, 15, ['(pad "1" smd rect (at 0 0 0) (size 0.6 0.6) (layers "F.Cu") (net "GND"))'])
    sev = {"rule_severities": {"isolated_copper": "error"}}
    B, _ = _load(tmp_path, c + ZONE, pro={"board": {"design_settings": sev}})
    isl = [x for x in LGL.collisions(B, {}) if "pour_island" in x["kinds"]]
    assert isl and isl[0]["level"] == "WARN"
    isl = [x for x in LGL.collisions(B, {"review": {"pour_island": "fail"}}) if "pour_island" in x["kinds"]]
    assert isl and isl[0]["level"] == "FAIL"


# ---- fix 2 + 3 + 4: guard -----------------------------------------------------------------------------------
def test_guard_restores_in_place_board_when_the_command_raises(tmp_path, monkeypatch, capsys):
    src = G.board(tmp_path, G.fp("U1", 40, 20, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "A")))']))
    orig = open(src).read()
    def boom(a):
        open(a.out, "w").write(orig.replace("(at 40 20", "(at 41 20"))
        raise RuntimeError("boom")
    monkeypatch.setattr(cli, "cmd_route_ready", boom)
    with pytest.raises(RuntimeError, match="boom"):
        cli.main(["--full", "route-ready", src, "--out", src])
    assert open(src).read() == orig
    assert "restored" in capsys.readouterr().err


def test_guard_unreadable_board_is_unchecked_not_silent(tmp_path, capsys):
    bad = tmp_path / "bad.kicad_pcb"
    bad.write_text("(kicad_pcb (this is not")
    g = cli._guard_begin(argparse.Namespace(cmd="route-ready", board=str(bad), out=None, constraints=None))
    assert g is not None and g.get("unchecked")
    assert "guard: UNCHECKED" in capsys.readouterr().err


def test_guard_skips_read_only_commands(tmp_path):
    src = G.board(tmp_path, G.fp("U1", 40, 20, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))']))
    assert cli._guard_begin(argparse.Namespace(cmd="lint", board=src, out=None, constraints=None)) is None
    assert cli._guard_begin(argparse.Namespace(cmd="route-ready", board=src, out=None, constraints=None)) is not None


def test_guard_ledger_sidecar_can_be_turned_off(tmp_path):
    src = G.board(tmp_path, G.fp("U1", 40, 20, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))']))
    out = str(tmp_path / "o.kicad_pcb")
    cons = tmp_path / "c.toml"
    cons.write_text("[guard]\nledger = false\n")
    a = argparse.Namespace(cmd="route-ready", board=src, out=out, constraints=str(cons))
    g = cli._guard_begin(a)
    open(out, "w").write(open(src).read())
    assert cli._guard_end(g, a)
    assert not os.path.exists(str(tmp_path / "o.ledger.json"))
    a.constraints = None
    open(out, "w").write(open(src).read() + "\n")
    cli._guard_end(g, a)
    assert os.path.exists(str(tmp_path / "o.ledger.json"))


# ---- fix 3: the pipeline ledger fails a stage whose board cannot be snapshotted -----------------------------
def test_pipeline_stage_with_unreadable_output_fails(tmp_path, capsys):
    work = tmp_path / "w"
    work.mkdir()
    led = PL._Ledger(str(work), ["route"], 0)
    src = G.board(tmp_path, G.fp("U1", 40, 20, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))']))
    led.before(src)
    bad = tmp_path / "out.kicad_pcb"
    bad.write_text("(kicad_pcb (broken")
    res = {"verdict": "pass", "board": str(bad)}
    led.after("route", 1, res)
    assert res["verdict"] == "fail" and "UNCHECKED" in res["line"]
    assert "UNCHECKED" in open(str(work / "1-route" / "ledger.txt")).read()


def test_pipeline_stage_with_unreadable_input_fails(tmp_path, capsys):
    work = tmp_path / "w"
    work.mkdir()
    led = PL._Ledger(str(work), ["route"], 0)
    bad = tmp_path / "in.kicad_pcb"
    bad.write_text("(kicad_pcb (broken")
    led.before(str(bad))
    assert "guard: UNCHECKED" in capsys.readouterr().err
    res = {"verdict": "pass", "board": str(bad)}
    led.after("route", 1, res)
    assert res["verdict"] == "fail"


# ---- fixes 8/9/10: block membership ----------------------------------------------------------------------------
def _blocks_board(tmp_path):
    body = G.fp("U1", 40, 40, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "VX"))'])
    body += G.fp("C1", 44, 40, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "VX"))'])
    body += G.fp("R1", 60, 40, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "SIG"))'])
    body += G.fp("R2", 62, 40, ['(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "SIG"))'])
    B, _ = _load(tmp_path, body)
    return B


def test_blocks_of_with_only_blocks_table_returns_claimed_blocks_only(tmp_path):
    B = _blocks_board(tmp_path)
    got = PCK.blocks_of(B, None, {"blocks": {"Power": ["U1", "C1"]}})
    assert got == {"Power": ["U1", "C1"]} or sorted(got["Power"]) == ["C1", "U1"]
    assert set(got) == {"Power"}                       # R1/R2 get no automatic group


def test_block_clash_is_a_warn_block_config_not_an_exception(tmp_path):
    B = _blocks_board(tmp_path)
    cons = {"blocks": {"A": ["U1"], "B": ["U1"]}}
    assert PCK.blocks_safe(B, None, cons) == {}
    with pytest.raises(BK.BlockError):
        PCK.blocks_of(B, None, cons)
    facts = {"board_path": str(tmp_path / "b.kicad_pcb")}
    f = RV.check_blocks(facts, None, cons)
    assert [(x["level"], x["code"]) for x in f] == [("WARN", "BLOCK_CONFIG")]


def test_review_passes_the_spec_path_through_like_the_blocks_command(tmp_path, monkeypatch):
    B = _blocks_board(tmp_path)
    seen = []
    real = BK.of_board
    monkeypatch.setattr(BK, "of_board", lambda b, c=None, s=None: (seen.append(s), real(b, c, None))[1])
    RV.check_blocks({"board_path": str(tmp_path / "b.kicad_pcb")}, "some_spec.json", {})
    assert seen == ["some_spec.json"]


def test_blocks_write_without_board_is_a_clear_error(tmp_path):
    import test_blocks as TB
    ns = argparse.Namespace(spec=TB._spec(tmp_path), board=None, write=True, out=str(tmp_path / "o.kicad_pcb"), constraints=None,
                            link=6.0, sep=3.0, check=False, json=False)
    with pytest.raises(SystemExit) as e:
        BK.cmd_blocks(ns)
    assert "--board" in str(e.value)
