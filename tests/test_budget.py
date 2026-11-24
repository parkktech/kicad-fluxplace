"""F2 (placer step 3): the one budget table. Rows: rail, support, pair, keepout, keepout-flight, separation, escape, swap."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as PG
from fluxplace import budget as BG, channel as CH, routediag as RD


def gduo(ref, x, y, p, n, pitch=0.5, **kw):
    return PG.fp(ref, x, y, [PG.pad("1", 0, 0, p, 0.3, 0.8), PG.pad("2", pitch, 0, n, 0.3, 0.8)], **kw)


def zone(x0, y0, x1, y1, layers='"*.Cu"'):
    return (f'(zone (net 0) (net_name "") (layers {layers}) (hatch edge 0.5) '
            f'(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed)) '
            f'(polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))\n')


USB = {"pairs": {"USB_": {"impedance_diff": 90}},
       "channel": {"USB_": {"length_mm_max": 203.0, "source": "TUSB4041I 7.4.1.3 p.25"}}}


def usb_board(tmp_path, extra="", name="b.kicad_pcb"):
    body = gduo("U1", 20, 20, "USB_D_P", "USB_D_N", court=(-1, -1, 1, 1)) + gduo("J1", 60, 20, "USB_D_P", "USB_D_N", court=(-1, -1, 1, 1))
    return RD.load(PG.board(tmp_path, body + extra, name=name))


def test_row_shape_and_verdicts():
    r = BG.row("pair", "X", "length", "mm", value=46.0, mx=40.0, source="s")
    assert r["verdict"] == "fail" and r["max"] == 40.0 and r["target"] is None and r["hard"] is False
    assert BG.row("pair", "X", "length", "mm", value=30.0, target=25.0, mx=40.0)["verdict"] == "over-target-pass"
    assert BG.row("pair", "X", "length", "mm", value=20.0, target=25.0, mx=40.0)["verdict"] == "ok"
    assert BG.row("pair", "X", "length", "mm", value=20.0)["verdict"] == "n/a"
    assert BG.row("pair", "X", "skew", "ps", mx=9.1)["verdict"] == "n/a"                      # no value yet
    # minimum rows: a floor
    assert BG.row("separation", "S", "distance", "mm", value=3.0, mx=5.0, minimum=True)["verdict"] == "fail"
    assert BG.row("separation", "S", "distance", "mm", value=25.0, target=30.0, mx=20.0, minimum=True)["verdict"] == "over-target-pass"
    assert BG.row("separation", "S", "distance", "mm", value=35.0, target=30.0, mx=20.0, minimum=True)["verdict"] == "ok"
    assert set(r) == {"kind", "name", "metric", "unit", "value", "target", "max", "source", "klass", "hard", "minimum", "refs",
                      "nets", "layers", "verdict", "detail"}


def test_no_source_lists_only_rows_with_a_limit():
    a = BG.row("pair", "A", "length", "mm", mx=10.0)
    b = BG.row("pair", "B", "length", "mm", mx=10.0, source="[channel.X] NO SOURCE")
    c = BG.row("pair", "C", "length", "mm", mx=10.0, source="datasheet p.3")
    d = BG.row("swap", "D", "crossing", "count")
    assert [r["name"] for r in BG.no_source([a, b, c, d])] == ["A", "B"]



def test_review_and_table_read_the_same_limits(monkeypatch):
    assert CH.resolve_budget(USB, "USB_D_N") == ({"length_mm_max": 203.0}, {"length_mm_max": "[channel.USB_] TUSB4041I 7.4.1.3 p.25"}, "USB_")
    seen = []
    real = BG.pair_limits
    monkeypatch.setattr(BG, "pair_limits", lambda *a, **k: (seen.append(a[1]), real(*a, **k))[1])
    CH.resolve_budget(USB, "USB_D_N")
    assert seen == ["USB_D_N"]                       # channel (review) goes through budget.pair_limits
