"""`fluxplace thermal`: [thermal.NAME] pre (locked GND vias + no-tracks rule area) and post (mask opening)."""
import math, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
import pcbnew
import cli

H_AT = (20.0, 20.0)
CONS = ('[thermal.t1]\nrect = [10, 10, 40, 40]\nside = "B"\nnet = "GND"\nexclude_refs = ["H1"]\nmask_open = true\n'
        '[thermal.t1.via]\ndiameter = 0.46\ndrill = 0.2\npitch = 2.0\n')
MM = pcbnew.FromMM
V = lambda x, y: pcbnew.VECTOR2I(MM(x), MM(y))


def _board(d, track=None):
    """A KiCad-written board: a 3.2 mm standoff H1 at H_AT, a GND/A pad pair at (60, 60), optional net-A track."""
    d.mkdir(parents=True, exist_ok=True)
    b = pcbnew.CreateEmptyBoard()
    b.SetCopperLayerCount(4)
    for a, c in zip([(0, 0), (100, 0), (100, 80), (0, 80)], [(100, 0), (100, 80), (0, 80), (0, 0)]):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(*a))
        s.SetEnd(V(*c))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(MM(0.1))
        b.Add(s)
    nets = {n: pcbnew.NETINFO_ITEM(b, n) for n in ("GND", "A")}
    for n in nets.values():
        b.Add(n)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference("H1")
    fp.SetPosition(V(*H_AT))
    p = pcbnew.PAD(fp)
    p.SetNumber("1")
    p.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    p.SetLayerSet(pcbnew.LSET.AllCuMask())
    p.SetSize(V(5.2, 5.2))
    p.SetDrillSize(V(3.2, 3.2))
    p.SetPosition(V(*H_AT))
    fp.Add(p)
    b.Add(fp)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference("R1")
    fp.SetPosition(V(60, 60))
    for i, (num, net) in enumerate((("1", "GND"), ("2", "A"))):
        p = pcbnew.PAD(fp)
        p.SetNumber(num)
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        ls = pcbnew.LSET()
        ls.AddLayer(pcbnew.F_Cu)
        p.SetLayerSet(ls)
        p.SetSize(V(0.6, 0.6))
        p.SetPosition(V(60 + 2 * i, 60))
        p.SetNet(nets[net])
        fp.Add(p)
    b.Add(fp)
    if track:
        for a, c in zip(track, track[1:]):
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(V(*a))
            t.SetEnd(V(*c))
            t.SetWidth(MM(0.2))
            t.SetLayer(pcbnew.F_Cu)
            t.SetNet(nets["A"])
            b.Add(t)
    path = str(d / "b.kicad_pcb")
    pcbnew.SaveBoard(path, b)
    return path


_N = [0]


def _cons(tmp_path, text=CONS):
    _N[0] += 1                               # a new file per call: the refusal cases must not overwrite the valid one
    p = tmp_path / f"c{_N[0]}.toml"
    p.write_text(text)
    return str(p)


def _zone(b, name):
    return [z for z in b.Zones() if z.GetZoneName() == name]


def test_pre_places_locked_gnd_vias_outside_the_cut_and_a_rule_area(tmp_path):
    brd, cons, out = _board(tmp_path), _cons(tmp_path), str(tmp_path / "pre.kicad_pcb")
    cli.main(["thermal", brd, "--constraints", cons, "--out", out])
    b = pcbnew.LoadBoard(out)
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == "GND"]
    assert len(vias) > 10 and all(v.IsLocked() for v in vias)
    for v in vias:
        x, y = pcbnew.ToMM(v.GetPosition().x), pcbnew.ToMM(v.GetPosition().y)
        assert 10 <= x <= 40 and 10 <= y <= 40
        assert math.hypot(x - H_AT[0], y - H_AT[1]) > 1.3 + 1.0          # pad half-size + cut_mm
    (z,) = _zone(b, "thermal t1")
    assert z.GetIsRuleArea() and z.GetDoNotAllowTracks() and not z.GetDoNotAllowZoneFills()
    assert z.GetLayer() == pcbnew.B_Cu


def test_pre_skips_sites_near_existing_copper(tmp_path):
    free = _cons(tmp_path)
    cli.main(["thermal", _board(tmp_path), "--constraints", free,
              "--out", str(tmp_path / "free.kicad_pcb")])
    n_free = sum(1 for t in pcbnew.LoadBoard(str(tmp_path / "free.kicad_pcb")).GetTracks() if t.Type() == pcbnew.PCB_VIA_T)
    t2 = tmp_path / "obs"
    brd = _board(t2, track=[(19, 10), (19, 40)])               # a foreign-net track on the grid column x = 19
    cli.main(["thermal", brd, "--constraints", _cons(t2), "--out", str(t2 / "o.kicad_pcb")])
    b = pcbnew.LoadBoard(str(t2 / "o.kicad_pcb"))
    vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == "GND"]
    assert 0 < len(vias) < n_free
    for v in vias:
        assert abs(pcbnew.ToMM(v.GetPosition().x) - 19) > 0.1 + 0.23 + 0.2   # clear of the track centre line


def test_post_adds_a_mask_opening_on_the_side(tmp_path):
    brd, cons = _board(tmp_path), _cons(tmp_path)
    pre = str(tmp_path / "pre.kicad_pcb")
    cli.main(["thermal", brd, "--constraints", cons, "--out", pre])
    out = str(tmp_path / "post.kicad_pcb")
    cli.main(["thermal", pre, "--constraints", cons, "--out", out, "--stage", "post"])
    shapes = [d for d in pcbnew.LoadBoard(out).GetDrawings()
              if d.GetLayer() == pcbnew.B_Mask and d.Type() == pcbnew.PCB_SHAPE_T and d.IsPolyShapeValid()]
    assert len(shapes) == 1 and shapes[0].GetPolyShape().Area() > 0


def test_post_without_mask_open_is_skipped(tmp_path, capsys):
    brd = _board(tmp_path)
    cons = _cons(tmp_path, CONS.replace("mask_open = true", "mask_open = false"))
    cli.main(["thermal", brd, "--constraints", cons, "--out", str(tmp_path / "p.kicad_pcb"), "--stage", "post"])
    assert "skipped (mask_open is not set)" in capsys.readouterr().out


def test_refusals(tmp_path):
    brd, cons = _board(tmp_path), _cons(tmp_path)
    with pytest.raises(SystemExit, match="side must be"):
        cli.main(["thermal", brd, "--constraints", _cons(tmp_path, CONS.replace('side = "B"', 'side = "X"')),
                  "--out", str(tmp_path / "x.kicad_pcb")])
    with pytest.raises(SystemExit, match="via needs"):
        cli.main(["thermal", brd, "--constraints", _cons(tmp_path, CONS.replace("pitch = 2.0", "")),
                  "--out", str(tmp_path / "x.kicad_pcb")])
    with pytest.raises(SystemExit, match="must differ"):
        cli.main(["thermal", brd, "--constraints", cons, "--out", brd])
    with pytest.raises(SystemExit, match="exclude_refs not on the board: NOPE"):
        cli.main(["thermal", brd, "--constraints", _cons(tmp_path, CONS.replace('"H1"', '"NOPE"')),
                  "--out", str(tmp_path / "x.kicad_pcb")])
    pre = str(tmp_path / "pre.kicad_pcb")
    cli.main(["thermal", brd, "--constraints", cons, "--out", pre])
    with pytest.raises(SystemExit, match="pre runs once"):
        cli.main(["thermal", pre, "--constraints", cons, "--out", str(tmp_path / "again.kicad_pcb")])
