import json
import os

from fluxplace import events, snapshot as SN

HEAD = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'


def _seg(a, b, net, w=0.15):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "F.Cu") (net "{net}"))\n'


def _fp(ref, x, y):
    return (f'(footprint "X" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}")'
            f' (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net "N1")))\n')


def _board(path, body):
    edge = '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))\n'
    path.write_text(HEAD + edge + body + ")\n")
    return str(path)


def test_full_snapshot_shape_and_stable_ids(tmp_path):
    b = _board(tmp_path / "a.kicad_pcb", _fp("U1", 10, 10) + _seg((1, 1), (5, 1), "N1") + _seg((5, 1), (5, 5), "N2"))
    out1, out2 = str(tmp_path / "f1.json"), str(tmp_path / "f2.json")
    f1 = SN.full(b, out1, 1)
    f2 = SN.full(b, out2, 2)
    assert json.load(open(out1))["v"] == 1
    assert len(f1["tracks"]) == 2 and f1["tracks"][0]["id"] == f2["tracks"][0]["id"]
    assert set(f1["nets"].values()) >= {"N1", "N2"}
    assert f1["parts"][0]["ref"] == "U1" and f1["pads"][0]["num"] == "1"
    assert f1["bbox"][2] >= 0 and "F.Cu" in f1["layers"]


def test_delta_one_add_one_del(tmp_path):
    b = _board(tmp_path / "a.kicad_pcb", _seg((1, 1), (5, 1), "N1") + _seg((5, 1), (5, 5), "N2"))
    prev = SN.full(b, str(tmp_path / "f1.json"), 1)
    b2 = _board(tmp_path / "b.kicad_pcb", _seg((1, 1), (5, 1), "N1") + _seg((5, 1), (6, 6), "N2"))
    d = SN.delta(prev, b2, str(tmp_path / "d2.json"), 2)
    assert d["base"] == 1 and d["seq"] == 2
    assert len(d["add"]["tracks"]) == 1 and len(d["del"]["tracks"]) == 1
    assert d["del"]["tracks"][0] == prev["tracks"][1]["id"]
    assert d["add"]["tracks"][0]["id"] != prev["tracks"][1]["id"]
    assert d["parts"] == [] and d["add"]["vias"] == [] and d["del"]["vias"] == []


def test_snapper_full_then_delta_latest(tmp_path):
    work = tmp_path / "W"
    work.mkdir()
    b1 = _board(work / "b.kicad_pcb", _seg((1, 1), (5, 1), "N1"))
    sn = SN.Snapper(str(work), snap_s=0.0, full_every=20)
    assert sn.take(b1, "start") == "snap/000001.json"
    assert os.path.exists(work / "snap" / "latest.json")
    b2 = _board(work / "b.kicad_pcb", _seg((1, 1), (5, 1), "N1") + _seg((5, 1), (5, 5), "N1"))
    assert sn.take(b2, "progress") == "snap/000002.delta.json"
    d = json.load(open(work / "snap" / "000002.delta.json"))
    assert d["base"] == 1 and len(d["add"]["tracks"]) == 1
    assert sn.take(b2, "stage_end") == "snap/000003.json"
    assert json.load(open(work / "snap" / "latest.json"))["seq"] == 3


def test_snapper_throttle_and_events(tmp_path, monkeypatch):
    work = tmp_path / "W"
    work.mkdir()
    events.init(work, run="RS")
    b = _board(work / "b.kicad_pcb", _seg((1, 1), (5, 1), "N1"))
    sn = SN.Snapper(str(work), snap_s=3.0)
    assert sn.take(b, "progress") is not None
    assert sn.take(b, "progress") is None  # throttled within snap_s
    rows = [json.loads(x) for x in open(work / "events.jsonl")]
    assert [r["kind"] for r in rows] == ["snapshot"] and rows[0]["full"] is True
    assert rows[0]["path"].startswith("snap/")


def test_snapper_never_raises(tmp_path):
    sn = SN.Snapper(str(tmp_path / "W"))
    assert sn.take(str(tmp_path / "missing.kicad_pcb"), "x", force=True) is None
