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


def _by(rows, metric, kind="pair"):
    return next(r for r in rows if r["kind"] == kind and r["metric"] == metric)


def test_pair_length_and_derived_endpoint_distance(tmp_path):
    B = usb_board(tmp_path)
    rows = BG.pair_rows(B, USB)
    assert [r["metric"] for r in rows] == ["length", "endpoint"]            # no stack: no loss row; no skew key: no skew row
    ln, ep = _by(rows, "length"), _by(rows, "endpoint")
    assert ln["name"] == "USB_D_N/USB_D_P" and ln["refs"] == ["J1", "U1"] and ln["nets"] == ["USB_D_N", "USB_D_P"]
    assert (ln["value"], ln["max"], ln["verdict"], ln["hard"]) == (46.0, 203.0, "ok", True)        # 40.0 mm x 1.15
    assert ln["source"] == "[channel.USB_] TUSB4041I 7.4.1.3 p.25"
    assert (ep["value"], ep["max"], ep["verdict"], ep["hard"]) == (40.0, 176.52, "ok", True)       # 203 / 1.15
    assert ep["source"].startswith("derived: ") and "TUSB4041I" in ep["source"]


def test_pair_over_its_length_fails_before_any_copper(tmp_path):
    cons = {"pairs": USB["pairs"], "channel": {"USB_": {"length_mm_max": 40.0, "length_mm_target": 25.0, "source": "x p.1"}}}
    rows = BG.pair_rows(usb_board(tmp_path), cons)
    assert (_by(rows, "length")["verdict"], _by(rows, "endpoint")["verdict"]) == ("fail", "fail")
    assert (_by(rows, "endpoint")["max"], _by(rows, "endpoint")["target"]) == (34.78, 21.74)       # 40 / 1.15, 25 / 1.15


def test_loss_limits_the_endpoint_distance_when_a_stack_is_given(tmp_path):
    rows6 = [{"type": "copper", "name": "F.Cu", "thickness": 0.035, "epsilon_r": 4.4},
             {"type": "prepreg", "name": None, "thickness": 0.1, "epsilon_r": 4.1},
             {"type": "copper", "name": "In1.Cu", "thickness": 0.0152, "epsilon_r": 4.4},
             {"type": "core", "name": None, "thickness": 1.2, "epsilon_r": 4.5},
             {"type": "copper", "name": "B.Cu", "thickness": 0.035, "epsilon_r": 4.4}]
    stack = (rows6, ["In1.Cu"], ["F.Cu", "B.Cu"])
    cons = {"pairs": USB["pairs"], "channel": {"USB_": {"loss_db_max": 1.0, "freq_ghz": 2.5, "source": "x p.2"}}}
    rows = BG.pair_rows(usb_board(tmp_path), cons, stack)
    lo = CH.loss_db_mm(rows6, ["In1.Cu"], "F.Cu", 2.5, BG.PAIR_W_MM)
    per_mm = lo[0] + lo[1]
    assert [r["metric"] for r in rows] == ["loss", "endpoint"]
    assert _by(rows, "loss")["value"] == round(46.0 * per_mm, 3) and _by(rows, "loss")["max"] == 1.0
    assert _by(rows, "endpoint")["max"] == round(1.0 / per_mm / 1.15, 2)


def test_pair_outside_every_family_has_no_rows(tmp_path):
    assert BG.pair_rows(usb_board(tmp_path), {}) == []           # the ready "pair classes" line names it (F1)


def test_a_90_ohm_family_yields_a_netclass_equal_to_pairs_layer_geometry():
    import test_zgeom as TZ
    from fluxplace import pairs as PA
    nc = BG.netclasses(TZ.CONS, stack=(TZ.ROWS, TZ.PLANES, TZ.SIG))["USB_"]
    rule = {"w": 0.2, "gap": 0.25, "target": 90, "family": "USB_"}
    geo = PA.layer_geometry(rule, TZ.SIG, TZ.PLANES, ["F.Cu"] + TZ.SIG[1:], TZ.ROWS, 0.127, cons=TZ.CONS)
    assert nc["z"] == 90 and set(nc["layers"]) == set(TZ.SIG)
    for l in TZ.SIG:
        assert nc["layers"][l] == geo[l][:2]
    assert (nc["track_width"], nc["diff_pair_gap"]) == geo["F.Cu"][:2] and nc["diff_pair_width"] == nc["track_width"]
    assert set(BG.netclasses(TZ.CONS, stack=(TZ.ROWS, TZ.PLANES, TZ.SIG))) == {"ETH_", "USB_"}


def test_flight_line_through_an_all_layer_keepout_is_a_hard_row_naming_both(tmp_path):
    B = usb_board(tmp_path, zone(38, 10, 42, 30))
    ko = BG.keepout_rows(B, USB)
    assert len(ko) == 1 and ko[0]["layers"] == ["B.Cu", "F.Cu", "In1.Cu"] and ko[0]["value"] == 80.0 and ko[0]["rect"] == [38.0, 10.0, 42.0, 30.0]
    f = BG.flight_rows(B, USB, fixed={"U1", "J1"})
    assert len(f) == 1 and f[0]["kind"] == "keepout-flight" and f[0]["hard"] is True and f[0]["verdict"] == "fail"
    assert f[0]["name"] == "USB_D_N/USB_D_P x rule-area 1" and f[0]["refs"] == ["J1", "U1"]
    assert "3 of 3 copper layers" in f[0]["detail"]
    assert BG.flight_rows(B, USB, fixed={"U1"})[0]["hard"] is False              # one end can still move


def test_flight_line_cases_that_are_not_hard(tmp_path):
    outer = usb_board(tmp_path, zone(38, 10, 42, 30, '"F.Cu" "B.Cu"'), name="o.kicad_pcb")
    f = BG.flight_rows(outer, USB, fixed={"U1", "J1"})
    assert len(f) == 1 and f[0]["hard"] is False and "2 of 3 copper layers" in f[0]["detail"]     # an inner layer passes
    assert BG.flight_rows(usb_board(tmp_path, zone(38, 30, 42, 40), name="m.kicad_pcb"), USB, fixed={"U1", "J1"}) == []   # beside the line
    assert BG.flight_rows(usb_board(tmp_path, zone(15, 15, 25, 25), name="s.kicad_pcb"), USB, fixed={"U1", "J1"}) == []   # the part's own area


def test_declared_keepout_needs_layers_to_be_a_routing_keepout(tmp_path):
    B = usb_board(tmp_path)
    cons = dict(USB, placement={"keepout": {"parts_only": {"rect": [38, 10, 42, 30], "side": "both"},
                                            "ant": {"rect": [38, 10, 42, 30], "layers": "all", "source": "module datasheet p.9"}}})
    assert [r["name"] for r in BG.keepout_rows(B, cons)] == ["ant"]
    f = BG.flight_rows(B, cons, fixed={"U1", "J1"})
    assert f[0]["name"] == "USB_D_N/USB_D_P x ant" and f[0]["hard"] is True and f[0]["source"] == "module datasheet p.9"
    under = dict(USB, placement={"keepout": {"gnss": {"under": "U1", "margin_mm": 2.0, "layers": ["F.Cu", "In1.Cu"], "source": "ds p.7"},
                                             "gone": {"under": "U99", "layers": "all", "source": "ds p.7"}}})
    k = BG.keepout_rows(B, under)
    assert [r["name"] for r in k] == ["gnss"] and k[0]["rect"] == [17.0, 17.0, 23.0, 23.0] and k[0]["layers"] == ["F.Cu", "In1.Cu"]
    assert BG.flight_rows(B, under, fixed={"U1", "J1"}) == []                 # the pair starts inside the area: the part's own keep-out
