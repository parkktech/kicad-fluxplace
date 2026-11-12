import json
import pytest
from fluxplace import density as D, routediag as RD
import pcbgen as G
from planboards import keepout


def _res(tmp_path, body, name="b.kicad_pcb", **kw):
    return D.compute(RD.load(G.board(tmp_path, body, name=name, **kw)), {}, None, grid=10)


def test_courtyard_util(tmp_path):
    r = _res(tmp_path, G.two("R1", 20, 20, "A", "B", court=(-5, -5, 5, 5)), outline=(0, 0, 100, 100))
    f = r["sides"]["F"]
    assert f["court_mm2"] == pytest.approx(100)
    assert f["usable_mm2"] == pytest.approx(10000, rel=1e-3)
    assert f["util"] == pytest.approx(0.01, rel=1e-2)
    assert r["sides"]["B"]["court_mm2"] == 0


def test_tht_both_sides(tmp_path):
    p = G.pad("1", 0, 0, "A", 1, 1, typ="thru_hole")
    r = _res(tmp_path, G.fp("J1", 20, 20, [p], court=(-5, -5, 5, 5)))
    assert r["sides"]["F"]["court_mm2"] == r["sides"]["B"]["court_mm2"] == pytest.approx(100)


def test_keepout_reduces_usable(tmp_path):
    r = _res(tmp_path, keepout(0, 0, 50, 100), outline=(0, 0, 100, 100))
    assert r["sides"]["F"]["usable_mm2"] == pytest.approx(5000, rel=1e-3)
    assert r["sides"]["F"]["keepout_mm2"] == pytest.approx(5000, rel=1e-3)


def test_bands(tmp_path):
    assert D.band(0.6) == "OK" and D.band(0.61) == "TIGHT" and D.band(0.75) == "TIGHT" and D.band(0.76) == "FAIL"
    r = _res(tmp_path, G.two("R1", 25, 25, "A", "B", court=(-20, -20, 20, 20)), outline=(0, 0, 50, 50))   # 64 %
    assert r["verdict"] == "TIGHT"
    r = _res(tmp_path, G.two("R1", 25, 25, "A", "B", court=(-23, -23, 23, 23)), outline=(0, 0, 50, 50))   # 84 %
    assert r["verdict"] == "FAIL"


def test_monotonic_growth(tmp_path):
    body = G.two("R1", 25, 25, "A", "B", court=(-15, -15, 15, 15))
    prev = None
    for g in (0, 2, 5, 10):
        u = _res(tmp_path, body, outline=(0, 0, 50 + g, 50))["total"]["util"]
        assert prev is None or u <= prev + 1e-12
        prev = u


def test_target(tmp_path):
    body = G.two("R1", 25, 25, "A", "B", court=(-20, -20, 20, 20))
    r = _res(tmp_path, body, outline=(0, 0, 50, 50))
    t = D.growth(r, 0.5)
    assert t["needed"] and t["right"] == pytest.approx(t["left"])
    r2 = _res(tmp_path, G.two("R1", 25, 25, "A", "B", court=(-20, -20, 20, 20)), outline=(0, 0, 50 + t["right"], 50), name="c.kicad_pcb")
    assert r2["sides"]["F"]["util"] == pytest.approx(0.5, rel=0.02)
    assert D.growth(r, 0.9)["needed"] is False


def test_cli(tmp_path, capsys):
    import argparse
    path = G.board(tmp_path, G.two("R1", 25, 25, "A", "B", court=(-5, -5, 5, 5)), outline=(0, 0, 50, 50))
    ap = argparse.ArgumentParser()
    D.add_parser(ap.add_subparsers())
    a = ap.parse_args(["density", path, "--target", "0.0005"])
    a.fn(a)
    assert "density: OK" in capsys.readouterr().out
    assert json.load(open(path.replace(".kicad_pcb", ".density.json")))["density"]["verdict"] == "OK"
