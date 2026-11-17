import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import constraints as C, legality as LGL, legalise as LG, placerules as PR, routediag as RD
import pcbgen as g


def hole(ref, x, y, drill, net=""):
    n = f' (net "{net}")' if net else ""
    p = (f'(pad "1" thru_hole circle (at 0 0) (size {drill + 2} {drill + 2}) (drill {drill}) '
         f'(layers "*.Cu"){n})')
    return g.fp(ref, x, y, [p], name="MountingHole", court=(-drill, -drill, drill, drill))


def npth(ref, x, y, drill):
    p = f'(pad "" np_thru_hole circle (at 0 0) (size {drill} {drill}) (drill {drill}) (layers "*.Cu"))'
    return g.fp(ref, x, y, [p], name="Hole")


def tht(ref, x, y, w=1.0):
    return g.fp(ref, x, y, [g.pad("1", 0, 0, "A", typ="thru_hole")], court=(-w, -w, w, w))


HW = {"placement": {"hardware": {}}}


def board(tmp_path, body):
    tmp_path.mkdir(exist_ok=True)
    return RD.load(g.board(tmp_path, body))


def test_hardware_off_without_table(tmp_path):
    B = board(tmp_path, hole("H1", 20, 20, 3.2))
    assert PR.hardware(B, {}) == [] and PR.hardware(B, {"placement": {}}) == []
    assert LG.keepout_rects(B, {}) == []


def test_hardware_detect_by_drill_table_and_overrides(tmp_path):
    ic = g.ic("U1", 60, 60, ["A", "B", "C", "D"])
    th = g.fp("J9", 70, 20, [g.pad("1", 0, 0, "N1", typ="thru_hole"), g.pad("2", 3, 0, "N2", typ="thru_hole")])
    body = hole("H1", 20, 20, 2.2) + hole("H2", 30, 20, 2.7) + hole("H3", 40, 20, 3.2) + npth("H4", 50, 20, 3.4) \
        + hole("H5", 20, 40, 3.2, net="VBUS") + hole("H6", 30, 40, 3.2) + ic + th + g.two("C1", 20, 60, "A", "B")
    B = board(tmp_path, body)
    cons = {"placement": {"hardware": {"exclude": ["H6"], "refs": ["C1"], "diameter_mm": {"C1": 4.0, "H3": 8.0}}}}
    got = {r: (c, d) for r, c, d in PR.hardware(B, cons)}
    assert {r: d for r, (c, d) in got.items()} == {"C1": 4.0, "H1": 5.0, "H2": 6.0, "H3": 8.0, "H4": 7.0}
    assert got["H2"][0] == (30.0, 20.0)                # J9 (two spots), U1, H5 (powered), H6 (excluded): not hardware
    assert PR.hardware(board(tmp_path / "g", hole("G1", 20, 20, 3.2, net="GND")), HW)[0][0] == "G1"   # a grounded mounting hole counts
    cons["placement"]["hardware"]["table"] = [[3.0, 5.5]]
    assert {r: d for r, c, d in PR.hardware(B, cons)}["H4"] == 5.5     # beyond the table: the last row


def test_hardware_circle_is_exact_and_placers_get_the_square(tmp_path):
    # hole at (20, 20): 7 mm washer = r 3.5. C1 centre (23.9, 23.9): its box corner (23.4,23.4) is 4.8 from the centre,
    # inside the bounding square (<= 3.5 each axis) but outside the circle
    body = hole("H1", 20, 20, 3.2) + g.two("C1", 23.9, 23.9, "A", "B", court=(-0.5, -0.5, 0.5, 0.5)) \
        + g.two("C2", 23.0, 20.0, "A", "B", court=(-0.5, -0.5, 0.5, 0.5))
    B = board(tmp_path, body)
    sq = LG.keepout_rects(B, HW)
    assert len(sq) == 1 and sq[0][0] == (16.5, 16.5, 23.5, 23.5) and sq[0][2] == ["H1"] and sq[0][4] == "hardware:H1"
    r = LGL.Legal(B, HW).issues()
    assert r["hardware"] == ["C2 in H1"] and r["forbidden"] == []
    cons = {"placement": {"hardware": {"allow": ["C2"]}}}
    assert LGL.Legal(B, cons).issues()["hardware"] == []
    assert any(f["code"] == "PLACEMENT_ILLEGAL" and "hardware" in str(f) for f in LGL.findings(B, HW))


def test_module_outline_expands_to_keepout_and_no_tht_only_when_asked():
    m = {"refs": ["J1"], "side": "B", "outline": [10, 10, 40, 40], "allow": ["X9"]}
    cons = {"placement": {"module": {"m2": dict(m)}}}
    PR.expand(cons)
    assert cons["placement"]["keepout"]["module.m2"] == {"rect": [10, 10, 40, 40], "side": "B", "allow": ["J1", "X9"],
                                                         "reason": "module m2"}
    assert not cons["placement"].get("no_tht")                   # tht defaults to true: only the keep-out
    cons = {"placement": {"module": {"m2": dict(m, tht=False)}}}
    PR.expand(cons)
    PR.expand(cons)                                              # idempotent
    assert cons["placement"]["no_tht"]["module.m2"] == {"rect": [10, 10, 40, 40], "allow": ["J1", "X9"], "reason": "module m2"}


def test_module_margin_outline_from_pads_and_load_expands(tmp_path):
    body = g.fp("J1", 30, 30, [g.pad("1", 0, 0, "A", w=2, h=2), g.pad("2", 10, 0, "A", w=2, h=2)]) + tht("T1", 31, 31)
    B = board(tmp_path, body)
    cons = {"placement": {"module": {"m": {"refs": ["J1"], "side": "F", "margin_mm": 1.0, "tht": False}}}}
    PR.expand(cons)                                              # no board: a margin outline waits
    assert not cons["placement"].get("keepout")
    rects = LG.keepout_rects(B, cons)                            # the readers expand with the board
    assert rects[0][0] == (28.0, 28.0, 42.0, 32.0) and rects[0][2] == ["J1"] and rects[0][4] == "module.m"
    r = LGL.Legal(B, cons).issues()
    assert r["forbidden"] == ["T1 in module.m"] and r["no_tht"] == ["T1 in module.m"]
    f = tmp_path / "c.toml"
    f.write_text('[placement.module.a]\nrefs = ["J1"]\nside = "B"\noutline = [0, 0, 5, 5]\ntht = false\n')
    c = C.load(str(f))
    assert c["placement"]["keepout"]["module.a"]["rect"] == [0.0, 0.0, 5.0, 5.0] and "module.a" in c["placement"]["no_tht"]


def _check_board(tmp_path):
    body = (hole("H1", 20, 20, 3.2) + g.two("C2", 23.0, 20.0, "A", "B", court=(-0.5, -0.5, 0.5, 0.5))
            + g.two("C3", 60, 60, "A", "B", side="B", court=(-1, -1, 1, 1)) + tht("T1", 61, 61))
    return g.board(tmp_path, body)


def test_placement_check_reports_every_rule_and_exits_1(tmp_path, capsys):
    brd = _check_board(tmp_path)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement.hardware]\n[placement.module.m]\nrefs = ["X"]\nside = "B"\noutline = [55, 55, 65, 65]\n'
                    'tht = false\n[placement]\nfixed = ["T1"]\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["placement-check", brd, "--constraints", str(cons), "--json"])
    assert e.value.code == 1
    res = json.loads(capsys.readouterr().out)
    by = {(r["kind"], r["name"]): r["violations"] for r in res["rules"]}
    assert [v["ref"] for v in by[("hardware", "H1")]] == ["C2"] and by[("hardware", "H1")][0]["distance_mm"] == 2.25
    assert [v["ref"] for v in by[("keepout", "module.m")]] == ["C3"]
    assert by[("keepout", "module.m")][0]["face"] == "B"          # T1 is on F: a B keep-out concerns B bodies only
    assert by[("no_tht", "module.m")][0]["pad"] == "1" and by[("no_tht", "module.m")][0]["fixed"] is True
    assert set(res["quadrants"]) == {"NW", "NE", "SW", "SE"} and res["quadrants"]["NW"]["F_pct"] > 0
    assert res["ok"] is False and res["violations"] == 3


def test_placement_check_ok_exit_0(tmp_path, capsys):
    brd = _check_board(tmp_path)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement.module.m]\nrefs = ["X"]\nside = "B"\noutline = [0, 0, 5, 5]\n')
    cli.main(["placement-check", brd, "--constraints", str(cons)])
    out = capsys.readouterr().out
    assert out.startswith("placement-check: OK") and "quadrant courtyard coverage" in out


def _under_board(tmp_path):
    body = "".join(g.two(r, x, 20, "A", "B", side="B", court=(-1, -1, 1, 1)) for r, x in (("C1", 15), ("C2", 20), ("C3", 25))) \
        + g.two("C4", 15, 30, "A", "B", side="F", court=(-1, -1, 1, 1))
    return g.board(tmp_path, body)


UNDER = {"placement": {"heights": {"C1": 1.0, "C2": 3.0, "C4": 9.0},
                       "module": {"m": {"refs": [], "side": "B", "outline": [10, 10, 40, 40], "under_height_mm": 2.5}}}}


def test_under_height_zero_is_nothing_under_the_module(tmp_path):
    cons = {"placement": {"heights": {"C1": 1.0},
                          "module": {"m": {"refs": [], "side": "B", "outline": [10, 10, 40, 40]}}}}
    r = LGL.Legal(RD.load(_under_board(tmp_path)), C.expand(cons) if hasattr(C, "expand") else PR.expand(cons)).issues()
    assert r["forbidden"] == ["C1 in module.m", "C2 in module.m", "C3 in module.m"]


def test_under_height_allows_low_rejects_tall_and_unknown(tmp_path):
    import copy
    cons = PR.expand(copy.deepcopy(UNDER))
    assert not cons["placement"].get("keepout")                      # the height rule is an intent region, not a plain keep-out
    r = LGL.Legal(RD.load(_under_board(tmp_path)), cons).issues()
    assert r["forbidden"] == ["C2 in module.m", "C3 in module.m"]    # C1 1.0 <= 2.3 ok; C2 3.0 too tall; C3 unknown; C4 is on F


def test_placement_check_reports_height_verdicts(tmp_path, capsys):
    brd = _under_board(tmp_path)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement.heights]\nC1 = 1.0\nC2 = 3.0\n[placement.module.m]\nrefs = []\nside = "B"\n'
                    'outline = [10, 10, 40, 40]\nunder_height_mm = 2.5\n')
    with pytest.raises(SystemExit):
        cli.main(["placement-check", brd, "--constraints", str(cons), "--json"])
    res = json.loads(capsys.readouterr().out)
    u = res["under"][0]
    assert u["limit_mm"] == 2.3 and [(p["ref"], p["verdict"]) for p in u["parts"]] == [
        ("C1", "ok"), ("C2", "too tall"), ("C3", "height unknown")]
    assert u["parts"][0]["source"].startswith("[placement.heights]") and res["violations"] == 2
