import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import floorplan as FP, routediag as RD, placechecks as PCK
import pcbgen as g

CRT = (-5, -3, 5, 3)         # 10 x 6 = 60 mm2


def mod(ref, x, y, side="F"):
    return g.fp(ref, x, y, [g.pad("1", 0, 0, "N" + ref)], court=CRT, side=side)


def board(tmp_path, n=4, side="B"):
    b = "".join(mod(f"U{300 + i}", 10 + 12 * i, 20, side) for i in range(n))
    b += g.two("R1", 90, 70, "A", "B", court=(-1, -0.5, 1, 0.5))
    return g.board(tmp_path, b)


def cons(rect, **kw):
    c = {"placement": {"blocks": {"bt": ["U30*"]}, "region": {"r": {"rect": rect, "side": "B", "blocks": ["bt"]}}}}
    c["placement"].update(kw)
    return c


def test_region_overfull_and_utilisation_report(tmp_path):
    B = RD.load(board(tmp_path))
    f, rows = FP.check(B, cons([0, 10, 30, 30]))            # 600 mm2 for 240 mm2 of modules = 40 %
    r = [x for x in rows if x["name"] == "r"][0]
    assert abs(r["demand"] - 240.0) < 1 and 600 * 0.9 < r["free"] <= 600 and r["util"] < 0.7
    assert not [x for x in f if x["code"] == "REGION_OVERFULL"]
    f, rows = FP.check(B, cons([0, 12, 30, 28]))            # 480 mm2: 50 %, still fits
    assert not f
    f, rows = FP.check(B, cons([0, 14, 30, 26]))            # 360 mm2: 67 %
    assert not f
    f, rows = FP.check(B, cons([0, 14, 26, 26]))            # 312 mm2: 77 % > 70 %
    assert [x["code"] for x in f] == ["REGION_OVERFULL"] and f[0]["value"] > 70


def test_keepout_and_limit_are_configurable(tmp_path):
    B = RD.load(board(tmp_path))
    c = cons([0, 10, 30, 30])
    c["placement"]["keepout"] = {"k": {"rect": [0, 10, 30, 20], "side": "B"}}   # half the region is forbidden for B parts
    f, rows = FP.check(B, c)
    assert [x["code"] for x in f] == ["REGION_OVERFULL"] or rows[0]["util"] > 0.7
    c = cons([0, 14, 26, 26], floorplan={"max_utilisation": 0.9})
    assert not FP.check(B, c)[0]


def test_no_free_slot_for_a_big_part(tmp_path):
    big = g.fp("U399", 50, 50, [g.pad("1", 0, 0, "X")], court=(-8, -5, 8, 5), side="B")      # 16 x 10
    B = RD.load(g.board(tmp_path, big + g.two("R1", 90, 70, "A", "B", court=(-1, -0.5, 1, 0.5))))
    c = {"placement": {"blocks": {"bt": ["U39*", "R1"]}, "region": {"r": {"rect": [0, 0, 40, 8], "side": "B", "blocks": ["bt"]}}}}
    f, rows = FP.check(B, c)
    assert any("no free slot for U399" in x["msg"] for x in f)


def test_blocks_from_a_blocks_dir_and_unassigned_pool(tmp_path):
    d = tmp_path / "blocks"
    d.mkdir()
    (d / "bluetooth.json").write_text(json.dumps({"block": "bluetooth", "add_components": [{"ref": "U300"}, {"ref": "U301"}]}))
    B = RD.load(board(tmp_path))
    assert PCK.blocks_of(B, None, {"placement": {"blocks_dir": str(d)}}) == {"bluetooth": ["U300", "U301"]}
    f, rows = FP.check(B, {"placement": {"blocks_dir": str(d)}})
    names = {r["name"] for r in rows}
    assert "(side B)" in names and not f
