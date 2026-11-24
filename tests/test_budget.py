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
                      "nets", "layers", "verdict", "detail", "derived"}


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
    assert (ep["value"], ep["max"], ep["verdict"], ep["hard"]) == (40.0, 176.52, "ok", False)      # 203 / 1.15; derived, so soft (C2)
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
    assert f[0]["name"] == "USB_D_N/USB_D_P x rule-area 38,10" and f[0]["refs"] == ["J1", "U1"]
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


def test_separation_rows_hard_minimum_target_and_scored(tmp_path):
    B = usb_board(tmp_path)                                   # courtyard gap U1 - J1 = 38.0 mm
    sep = lambda **k: {"separation": [dict({"name": "s", "a": ["U*"], "b": ["J1"]}, **k)]}       # noqa: E731
    r = BG.separation_rows(B, sep(min_mm=40.0, source="ds p.4"))[0]
    assert (r["value"], r["max"], r["verdict"], r["hard"], r["minimum"], r["refs"]) == (38.0, 40.0, "fail", True, True, ["U1", "J1"])
    assert BG.separation_rows(B, sep(min_mm=20.0, source="ds p.4"))[0]["verdict"] == "ok"
    assert BG.separation_rows(B, sep(min_mm=20.0, target_mm=50.0, source="ds p.4"))[0]["verdict"] == "over-target-pass"
    s = BG.separation_rows(B, sep(why="clock harmonic"))[0]                                      # scored: no limit, never fails
    assert (s["value"], s["verdict"], s["hard"], s["detail"]) == (38.0, "n/a", False, "clock harmonic")
    assert BG.separation_rows(B, sep(min_mm=40.0, enabled=False)) == []
    assert BG.separation_rows(B, sep(a=["Q*"]))[0]["value"] is None


def test_hard_separation_is_held_by_the_intent_gate(tmp_path):
    from fluxplace import intent as IT
    B = usb_board(tmp_path)
    cons = {"separation": [{"name": "s", "a": ["U1"], "b": ["J1"], "min_mm": 40.0, "source": "ds p.4"},
                           {"name": "scored", "a": ["U1"], "b": ["J1"]}]}
    rows = [r for r in IT.check(B, cons) if r["kind"] == "away"]
    assert len(rows) == 1 and rows[0]["name"] == "sep:s" and rows[0]["met"] is False and rows[0]["hard"] is True
    assert (rows[0]["value"], rows[0]["limit"]) == (38.0, 40.0)


def test_tracks_between_pads_closed_form():
    assert BG.tracks_between(0.4, 0.2, 0.127, 0.127) == 0          # the strategy's acceptance case: 0.4 mm pitch at a 0.127 mm floor
    assert BG.tracks_between(0.5, 0.25, 0.09, 0.09) == 0           # gap 0.25 < 2 x 0.09 + 0.09 = 0.27
    assert BG.tracks_between(0.508, 0.127, 0.127, 0.127) == 1      # gap 0.381 = 2 x 0.127 + 0.127 exactly
    assert BG.tracks_between(0.8, 0.4, 0.1, 0.1) == 1
    assert BG.tracks_between(1.27, 0.6, 0.127, 0.127) == 2


def _grid(ref, x, y, n, pitch=0.8, pad=0.4):
    return PG.fp(ref, x, y, [PG.pad(f"{r}_{c}", c * pitch, r * pitch, f"{ref}g{r}_{c}", pad, pad) for r in range(n) for c in range(n)])


def test_escape_rows_dual_row_connector_and_grids(tmp_path):
    conn = [PG.pad(str(i + 1), i * 0.4, 0, f"n{i}", 0.2, 0.7) for i in range(10)] + \
           [PG.pad(str(i + 11), i * 0.4, 3.0, f"m{i}", 0.2, 0.7) for i in range(10)]
    B = RD.load(PG.board(tmp_path, PG.fp("J10", 20, 20, conn)))
    r = BG.escape_rows(B, {"route": {"min_track_mm": 0.127, "min_clearance_mm": 0.127}})[0]
    assert (r["kind"], r["name"], r["value"]) == ("escape", "J10", 0)
    assert (r["pitch_mm"], r["pad_mm"], r["gap_mm"], r["pins"], r["rows_deep"]) == (0.4, 0.2, 0.2, 20, 1)
    assert (r["rows_surface"], r["layers_needed"], r["corridor_mm"]) == (1, 1, 0.254)
    cons = {"route": {"min_track_mm": 0.1, "min_clearance_mm": 0.1, "min_via_mm": 0.45}}
    B2 = RD.load(PG.board(tmp_path, _grid("U9", 20, 20, 6) + _grid("U8", 50, 20, 10), name="g.kicad_pcb"))
    u8, u9 = BG.escape_rows(B2, cons)
    assert (u9["value"], u9["tracks_between_vias"], u9["rows_deep"], u9["rows_surface"], u9["layers_needed"], u9["corridor_mm"]) == (1, 1, 3, 2, 2, 0.4)
    assert (u8["name"], u8["rows_deep"], u8["layers_needed"]) == ("U8", 5, 3)             # 5 rows: 2 + 2 on two layers, 1 more layer
    tight = {"route": {"min_track_mm": 0.2, "min_clearance_mm": 0.2, "min_via_mm": 0.45}}
    assert BG.escape_rows(B2, tight)[0]["layers_needed"] is None                           # nothing passes between pads or vias
    # a perimeter part with a thermal pad is one ring deep: 4 x 5 pads at 0.5 mm on a 4 mm square, centre pad 2.5 mm
    side = [(i * 0.5 - 1.0, -2.0) for i in range(5)] + [(i * 0.5 - 1.0, 2.0) for i in range(5)] + \
           [(-2.0, i * 0.5 - 1.0) for i in range(5)] + [(2.0, i * 0.5 - 1.0) for i in range(5)]
    qfn = [PG.pad(str(i + 1), x, y, f"q{i}", 0.25, 0.25) for i, (x, y) in enumerate(side)] + [PG.pad("21", 0, 0, "GND", 2.5, 2.5)]
    q = BG.escape_rows(RD.load(PG.board(tmp_path, PG.fp("U7", 30, 30, qfn), name="q.kicad_pcb")), cons)[0]
    assert (q["pins"], q["rows_deep"], q["layers_needed"], q["pitch_mm"], q["value"]) == (21, 1, 1, 0.5, 0)


def test_polarity_swap_is_proposed_only_when_the_legs_cross(tmp_path):
    cons = {"swap": [{"name": "d", "kind": "polarity", "nets": ["D_P", "D_N"], "source": "ds 2.3 p.12"}]}
    crossed = RD.load(PG.board(tmp_path, gduo("U1", 20, 20, "D_P", "D_N") + gduo("J1", 20, 40, "D_N", "D_P")))
    r = BG.swap_rows(crossed, cons)[0]
    assert (r["kind"], r["value"], r["max"], r["verdict"], r["hard"], r["refs"]) == ("swap", 1, 0, "fail", False, ["J1", "U1"])
    assert r["detail"].startswith("proposal: swap P/N")
    straight = RD.load(PG.board(tmp_path, gduo("U1", 20, 20, "D_P", "D_N") + gduo("J1", 20, 40, "D_P", "D_N"), name="s.kicad_pcb"))
    assert (BG.swap_rows(straight, cons)[0]["value"], BG.swap_rows(straight, cons)[0]["verdict"]) == (0, "ok")
    listed = BG.swap_rows(straight, {"swap": [{"name": "g", "kind": "gates", "source": "ds p.3"}]})[0]
    assert (listed["value"], listed["verdict"]) == (None, "n/a")


PLAN = {"rails": {"+5V": {"source": {"spec": "U100"}, "drop": {"source": "module datasheet 3.1 p.16", "klass": "INF"},
                          "choice": {"layers": ["F.Cu"]},
                          "edges": [{"load": "J10", "dist_mm": 61.45, "reach_mm": {"target": 42.44, "max": 84.89}}]}},
        "supports": [{"ref": "C9", "role": "bypass", "serves": "U1.7", "net": "+5V", "dist_mm": 30.0, "sure": True, "how": "net",
                      "limit": {"target": 3.0, "max": 5.0, "source": "default", "klass": "INF"}},
                     {"ref": "C8", "role": "bypass", "serves": "U1.7", "net": "+5V", "dist_mm": 30.0, "sure": False, "how": "spread",
                      "limit": {"target": 3.0, "max": 5.0, "source": "default", "klass": "INF"}}]}


def test_table_joins_rail_support_and_pair_rows_in_one_shape(tmp_path):
    rows = BG.table(usb_board(tmp_path), USB, plan=PLAN)
    assert [(r["kind"], r["metric"], r["verdict"]) for r in rows] == [
        ("rail", "reach", "over-target-pass"), ("support", "distance", "fail"), ("support", "distance", "n/a"),
        ("pair", "length", "ok"), ("pair", "endpoint", "ok")]
    assert rows[0]["name"] == "+5V U100->J10" and rows[0]["hard"] and rows[2]["hard"] is False and rows[2]["value"] is None
    keys = set(BG.row("pair", "x", "m", "mm"))
    assert all(keys <= set(r) for r in rows)


def test_ready_budgets_line_open_items(tmp_path):
    from fluxplace import ready as RDY
    B = usb_board(tmp_path, zone(38, 10, 42, 30))
    cons = dict(USB, placement={"fixed": ["U1", "J1"]},
                swap=[{"name": "d", "kind": "polarity", "nets": ["USB_D_P", "USB_D_N"], "source": "ds p.1"}],
                separation=[{"name": "s", "a": ["U1"], "b": ["J1"], "min_mm": 5.0}])
    detail = {}
    line = RDY.budget_line(None, cons, B, None, None, detail)
    assert line["name"] == "budgets" and line["status"] == RDY.OPEN and line["open_items"] == 2
    assert line["blockers"][0].startswith("BUDGET_NO_SOURCE 1: separation s distance")
    assert line["blockers"][1].startswith("KEEPOUT_FLIGHT 1: USB_D_N/USB_D_P x rule-area 38,10")
    assert {r["kind"] for r in detail["budgets"]["rows"]} == {"pair", "keepout", "keepout-flight", "separation", "swap"}
    cons["separation"][0]["source"] = "ds p.4"
    clean = RDY.budget_line(None, cons, usb_board(tmp_path, name="c.kicad_pcb"), None, None, {})
    assert clean["status"] == RDY.PASS and "open_items" not in clean and "2 pair" in clean["detail"]


# ---- step-3 evaluation (EVAL-PLACER-STEP3)
def test_active_low_net_under_a_family_prefix_is_no_pair_leg_on_a_whole_board(tmp_path):
    from fluxplace import constraints as C
    nets = ["PCIE_TX_P", "PCIE_TX_N", "PCIE_RST_N", "PCIE_WAKE_N", "PCIE_CLKREQ_N", "GND"]
    cons = {"pairs": {"PCIE_": {"impedance_diff": 90}}}
    assert BG.pair_legs(nets, cons, complete=True) == {"PCIE_TX_P", "PCIE_TX_N"}
    assert "PCIE_RST_N" in BG.pair_legs(["PCIE_RST_N"], cons)                 # a partial list (DRC items): the tail rule stays
    body = gduo("U1", 20, 20, "PCIE_TX_P", "PCIE_TX_N") + gduo("J1", 60, 20, "PCIE_TX_P", "PCIE_TX_N") + \
        gduo("U2", 20, 40, "PCIE_RST_N", "PCIE_WAKE_N") + gduo("J2", 60, 40, "PCIE_RST_N", "PCIE_WAKE_N")
    board = PG.board(tmp_path, body)
    assert C.pair_legs_of(cons, board) == {"PCIE_TX_N": "PCIE_", "PCIE_TX_P": "PCIE_"}      # the written class takes the pair only


def test_footprint_rule_area_is_named_after_its_part_and_is_its_own_keepout(tmp_path):
    B = usb_board(tmp_path, zone(38, 10, 42, 30))
    B["zones"][0]["owner"] = "U1"                                              # routediag gives a footprint's rule area its owner
    k = BG.keepout_rows(B, USB)[0]
    assert (k["name"], k["refs"]) == ("U1 rule-area 1", ["U1"]) and k["source"].startswith("footprint rule area of U1")
    assert BG.flight_rows(B, USB, fixed={"U1", "J1"}) == []                    # the pair's own part owns it
    B["zones"][0]["owner"] = "Q9"
    assert len(BG.flight_rows(B, USB, fixed={"U1", "J1"})) == 1                # another part's area is crossed


def test_separation_against_a_keepout_rect(tmp_path):
    from fluxplace import intent as IT
    B = usb_board(tmp_path, zone(30, 19, 34, 21).replace('(net_name "")', '(net_name "") (name "ANT_CUT")'))
    assert [k["name"] for k in BG.keepouts(B, USB)] == ["ANT_CUT"]
    cons = dict(USB, separation=[{"name": "ant-metal", "a": ["keepout:ANT_CUT"], "b": ["U1", "J1"], "min_mm": 10.0, "source": "ds 4.1 p.9"}])
    r = BG.separation_rows(B, cons)[0]                                         # U1 courtyard ends at x 21, the cut-out starts at 30
    assert (r["value"], r["max"], r["verdict"], r["hard"], r["refs"]) == (9.0, 10.0, "fail", True, ["keepout:ANT_CUT", "U1"])
    assert [x for x in IT.check(B, cons) if x["kind"] == "away"] == []         # no part on side a: the budget row grades it
    assert BG.separation_rows(B, dict(USB, separation=[{"a": ["keepout:NONE"], "b": ["U1"]}]))[0]["value"] is None


def test_rail_class_narrower_than_the_plan_is_shown_on_the_budgets_line(tmp_path):
    from fluxplace import ready as RDY
    plan = {"rails": {"+3V3": {"choice": {"kind": "track", "layers": ["In2.Cu"], "width_mm": 0.676}},
                      "+1V8": {"choice": {"kind": "track", "layers": ["F.Cu"], "width_mm": 0.127}},
                      "+5V": {"choice": {"kind": "corridor", "layers": ["F.Cu"], "width_mm": 2.2}}}}
    cons = dict(USB, power={"+3V3": {"max_current_ma": 900}, "+1V8": {"max_current_ma": 20}, "+5V": {"max_current_ma": 4200, "pour": True}})
    rc = BG.rail_classes(cons, plan)
    assert rc == {"+1V8": {"plan_mm": 0.127, "class_mm": 0.3, "under": False}, "+3V3": {"plan_mm": 0.676, "class_mm": 0.5, "under": True}}
    line = RDY.budget_line(None, cons, usb_board(tmp_path), None, None, {"power_plan": plan})
    assert line["status"] == RDY.PASS and line["detail"].endswith("1 rail class(es) narrower than the plan (+3V3 0.5 < 0.676 mm)")


def test_summary_counts_fail_and_over_target_rows():
    rows = [BG.row("support", "a", "distance", "mm", 30.0, 3.0, 5.0, "s", hard=True), BG.row("pair", "b", "length", "mm", 30.0, 25.0, None, "s"),
            BG.row("swap", "c", "crossing", "count", 1, None, 0, "s"), BG.row("pair", "d", "length", "mm", 3.0, 25.0, 40.0, "s")]
    assert BG.summary(rows) == "2 fail (1 hard); 1 over target" and BG.summary(rows[3:]) == ""


# ---- placer step 3b: per-pin demand rows
def test_pin_demand_rows_join_the_table_and_name_the_part_and_the_ic(tmp_path):
    plan = {"pin_demand": [
        {"ic": "U412", "pad": "7", "net": "VBATT", "parts": ["C419"], "uf": 0.1, "dist_mm": 54.03, "limit_mm": 5.0,
         "cover": {"ref": "C1", "uf": 4.7, "dist_mm": 2.12}, "verdict": "over-target-pass"},
        {"ic": "U5", "pad": "32", "net": "+1V8", "parts": ["C54", "C55"], "uf": 1.0, "dist_mm": 36.44, "limit_mm": 5.0, "cover": None, "verdict": "fail"}]}
    rows = [r for r in BG.table(usb_board(tmp_path), USB, plan=plan) if r["metric"] == "pin-bypass"]
    a, b = rows
    assert (a["kind"], a["name"], a["value"], a["target"], a["max"], a["verdict"], a["hard"]) == \
        ("support", "U412.7 own bypass (C419)", 54.03, 5.0, None, "over-target-pass", False)
    assert a["refs"] == ["C419", "U412"] and a["detail"] == "only a larger capacitor at the pin: C1 4.7 uF at 2.12 mm"
    assert (b["name"], b["max"], b["verdict"], b["hard"], b["refs"], b["detail"]) == \
        ("U5.32 own bypass (C54, C55)", 5.0, "fail", False, ["C54", "C55", "U5"], "no capacitor at the pin")
    assert BG.summary(rows) == "1 fail (0 hard); 1 over target"


# ---- C2: a derived limit is never hard (D71do)
def test_derived_limit_is_never_hard():
    r = BG.row("support", "x", "distance", "mm", 9, 5, 5, "default 5 mm", "INF", True, derived="default")
    assert r["verdict"] == "fail" and r["hard"] is False and r["derived"] == "default"
    assert BG.row("support", "x", "distance", "mm", 9, 5, 5, "default 5 mm", "INF", True)["hard"] is True


def test_rail_row_without_a_page_is_soft(tmp_path):
    import copy
    plan = copy.deepcopy(PLAN)
    plan["rails"]["+5V"]["drop"]["source"] = "notes.md"
    r = [x for x in BG.table(usb_board(tmp_path), USB, plan=plan) if x["kind"] == "rail"][0]
    assert r["hard"] is False and r["derived"] == "no page cited"
    plan["rails"]["+5V"]["drop"]["source"] = "X.pdf#p5"
    r = [x for x in BG.table(usb_board(tmp_path, name="p.kicad_pcb"), USB, plan=plan) if x["kind"] == "rail"][0]
    assert r["hard"] is True and r["derived"] == ""


# ---- C3: a keep-out or separation never flags a part against its own module (L4, A4)
CT = (-1, -1, 1, 1)


def _ind_board(tmp_path, name="ind.kicad_pcb"):
    """U1 (20,20) with L1 (23,20) as its support part (keep-out under L1, all layers), U2 and J1 far away."""
    return usb_board(tmp_path, gduo("L1", 23, 20, "LA", "LB", court=CT) + gduo("U2", 60, 20, "UA", "UB", court=CT), name=name)


def test_keepout_is_not_measured_against_its_own_part_or_module(tmp_path):
    B = _ind_board(tmp_path)
    cons = dict(USB, placement={"keepout": {"ind": {"under": "L1", "layers": "all"}}},
                separation=[{"name": "s", "a": ["keepout:ind"], "b": ["U*", "L*"], "min_mm": 5.0}])
    r = BG.separation_rows(B, cons, home={"L1": "U1"})[0]
    assert r["value"] == 35.0 and "U2" in r["refs"] and "L1" not in r["refs"] and "U1" not in r["refs"]


def test_part_in_both_groups_is_not_measured_against_its_mates(tmp_path):
    B = _ind_board(tmp_path, name="both.kicad_pcb")
    cons = dict(USB, separation=[{"name": "s", "a": ["U1", "L1"], "b": ["U*"], "min_mm": 5.0}])
    r = BG.separation_rows(B, cons)[0]
    # U1 is in both groups: it is not measured against L1 (1.0 mm); the nearest mate left is U2 from L1 (35.0 mm)
    assert r["value"] == 35.0 and r["refs"] == ["L1", "U2"]


def test_two_parts_of_one_module_in_opposite_groups_are_still_measured(tmp_path):
    B = _ind_board(tmp_path, name="opp.kicad_pcb")
    cons = dict(USB, separation=[{"name": "s", "a": ["U1"], "b": ["L1"], "min_mm": 5.0}])
    r = BG.separation_rows(B, cons, home={"L1": "U1"})[0]
    assert r["verdict"] == "fail" and r["value"] == 1.0 and r["hard"] is True


def test_flight_line_is_not_blocked_by_a_keepout_of_its_own_module(tmp_path):
    B = _ind_board(tmp_path, name="fl.kicad_pcb")
    cons = dict(USB, placement={"keepout": {"ind": {"under": "L1", "layers": "all"}}})
    assert BG.flight_rows(B, cons, fixed=("U1", "J1"), home={"L1": "U1"}) == []
    f = BG.flight_rows(B, cons, fixed=("U1", "J1"))
    assert len(f) == 1 and f[0]["hard"] is True


# ---- C4: escape rows can fail (X1, B2)
def test_escape_layers_fail_when_the_stackup_is_short(tmp_path):
    cons = {"route": {"min_track_mm": 0.1, "min_clearance_mm": 0.1, "min_via_mm": 0.45}, "stackup": {"planes": ["In1.Cu"]}}
    B2 = RD.load(PG.board(tmp_path, _grid("U9", 20, 20, 6) + _grid("U8", 50, 20, 10), name="g.kicad_pcb"))
    rows = {r["name"]: r for r in BG.table(B2, cons) if r["kind"] == "escape" and r["metric"] == "layers_needed"}
    assert (rows["U9"]["value"], rows["U9"]["verdict"]) == (2, "ok")
    assert (rows["U8"]["value"], rows["U8"]["verdict"], rows["U8"]["hard"]) == (3, "fail", False)
    tight = dict(cons, route={"min_track_mm": 0.2, "min_clearance_mm": 0.2, "min_via_mm": 0.45})
    t = {r["name"]: r for r in BG.escape_layer_rows(B2, tight)}
    assert (t["U8"]["value"], t["U8"]["max"], t["U8"]["verdict"]) == (3, 2, "fail")


def test_escape_rows_do_not_change_with_rotation(tmp_path):
    side = [(i * 0.5 - 1.0, -2.0) for i in range(5)] + [(i * 0.5 - 1.0, 2.0) for i in range(5)] + \
           [(-2.0, i * 0.5 - 1.0) for i in range(5)] + [(2.0, i * 0.5 - 1.0) for i in range(5)]
    cons = {"route": {"min_track_mm": 0.1, "min_clearance_mm": 0.1, "min_via_mm": 0.45}}
    conn = [PG.pad(str(i + 1), i * 0.4, 0, f"n{i}", 0.2, 0.7) for i in range(10)] + \
           [PG.pad(str(i + 11), i * 0.4, 3.0, f"m{i}", 0.2, 0.7) for i in range(10)]
    seen = []
    for a in (0, 90, 180, 270):
        qfn = [PG.pad(str(i + 1), x, y, f"q{i}", 0.25, 0.8) for i, (x, y) in enumerate(side)]
        B = RD.load(PG.board(tmp_path, PG.fp("J10", 20, 20, conn, angle=a) + PG.fp("U7", 50, 30, qfn, angle=a), name=f"r{a}.kicad_pcb"))
        seen.append(sorted((r["name"], r["value"], r["pitch_mm"], r["pad_mm"], r["gap_mm"], r["rows_deep"], r["layers_needed"],
                            r["corridor_mm"]) for r in BG.escape_rows(B, cons)))
    assert all(s == seen[0] for s in seen) and len(seen[0]) == 2


# ---- C5: fixtures that must FAIL each row kind (L6, B2, A6)
def test_rail_edge_over_its_reach_fails_in_the_table(tmp_path):
    import copy
    plan = copy.deepcopy(PLAN)
    plan["rails"]["+5V"]["edges"][0]["dist_mm"] = 90.0                     # reach max 84.89
    r = [x for x in BG.table(usb_board(tmp_path), USB, plan=plan) if x["kind"] == "rail"][0]
    assert r["verdict"] == "fail" and r["hard"] is True and r["value"] == 90.0


_LOSS_ROWS = [{"type": "copper", "name": "F.Cu", "thickness": 0.035, "epsilon_r": 4.4},
              {"type": "prepreg", "name": None, "thickness": 0.1, "epsilon_r": 4.1},
              {"type": "copper", "name": "In1.Cu", "thickness": 0.0152, "epsilon_r": 4.4},
              {"type": "core", "name": None, "thickness": 1.2, "epsilon_r": 4.5},
              {"type": "copper", "name": "B.Cu", "thickness": 0.035, "epsilon_r": 4.4}]


def test_pair_over_its_loss_fails(tmp_path):
    stack = (_LOSS_ROWS, ["In1.Cu"], ["F.Cu", "B.Cu"])
    cons = {"pairs": USB["pairs"], "channel": {"USB_": {"loss_db_max": 0.01, "freq_ghz": 2.5, "source": "x p.2"}}}
    lo = _by(BG.pair_rows(usb_board(tmp_path), cons, stack), "loss")
    assert lo["verdict"] == "fail" and lo["hard"] is True and lo["max"] == 0.01


def test_pair_endpoints_too_far_apart_fail(tmp_path):
    cons = {"pairs": USB["pairs"], "channel": {"USB_": {"length_mm_max": 30.0, "source": "x p.2"}}}
    ep = _by(BG.pair_rows(usb_board(tmp_path), cons), "endpoint")
    assert ep["verdict"] == "fail" and ep["hard"] is False and ep["derived"] == f"endpoint = length / detour {CH.DETOUR:g}"


def test_separation_and_flight_rows_do_not_change_with_rotation(tmp_path):
    seps, flights = [], []
    for a in (0, 90, 180, 270):
        B = RD.load(PG.board(tmp_path, gduo("U1", 20, 20, "USB_D_P", "USB_D_N", court=CT, angle=a)
                             + gduo("J1", 60, 20, "USB_D_P", "USB_D_N", court=CT, angle=a), name=f"rot{a}.kicad_pcb"))
        cons = dict(USB, separation=[{"name": "s", "a": ["U1"], "b": ["J1"], "min_mm": 5.0}])
        seps.append(BG.separation_rows(B, cons)[0]["value"])
        flights.append(len(BG.flight_rows(B, USB)))
    # both courtyards are 2 mm squares: the gap is 60 - 20 - 2 = 38 mm at every rotation
    assert all(abs(v - 38.0) <= 0.01 for v in seps) and len(set(flights)) == 1


# ---- C6: stable row names (B4)
def test_unnamed_separation_keeps_its_name_when_a_row_is_removed_above_it(tmp_path):
    B = usb_board(tmp_path)
    first = {"a": ["U1"], "b": ["J1"], "min_mm": 5.0}
    second = {"a": ["J1"], "b": ["U2"], "min_mm": 5.0}
    both = BG.separation_rows(B, dict(USB, separation=[first, second]))
    alone = BG.separation_rows(B, dict(USB, separation=[second]))
    assert both[1]["name"] == alone[0]["name"] == "sep J1 / U2"
    disabled = BG.separation_rows(B, dict(USB, separation=[dict(first, enabled=False), second]))
    assert disabled[0]["name"] == "sep J1 / U2"


def test_honoured_ok_to_fail_is_not_honoured_and_fail_to_ok_is_honoured():
    b = [BG.row("pair", "X", "length", "mm", value=20.0, mx=40.0, source="s", hard=True)]
    a = [BG.row("pair", "X", "length", "mm", value=46.0, mx=40.0, source="s", hard=True)]
    h = BG.honoured(b, a)
    assert len(h) == 1 and h[0]["status"] == "not_honoured" and h[0]["hard"] is True
    assert h[0]["value_in"] == 20.0 and h[0]["value_out"] == 46.0 and h[0]["limit"] == 40.0
    h2 = BG.honoured(a, b)
    assert h2[0]["status"] == "honoured" and h2[0]["value_in"] == 46.0 and h2[0]["value_out"] == 20.0
    assert BG.honoured(b, [BG.row("pair", "X", "length", "mm", value=30.0, target=25.0, mx=40.0, source="s")])[0]["status"] == "honoured"


def test_honoured_row_missing_in_after_is_not_measured_and_row_only_in_after_has_no_value_in():
    gone = BG.row("pair", "A", "length", "mm", value=20.0, mx=40.0, source="s")
    new = BG.row("pair", "B", "length", "mm", value=20.0, mx=40.0, source="s")
    h = {(r["name"]): r for r in BG.honoured([gone], [new])}
    assert h["A"]["status"] == "not_measured" and h["A"]["value_out"] is None and h["A"]["value_in"] == 20.0
    assert h["B"]["status"] == "honoured" and h["B"]["value_in"] is None and h["B"]["value_out"] == 20.0


def test_honoured_summary_counts_hard_and_soft_apart():
    h = [{"name": "H1", "status": "not_honoured", "hard": True}, {"name": "S1", "status": "not_honoured", "hard": False},
         {"name": "M1", "status": "not_measured", "hard": False}, {"name": "O1", "status": "honoured", "hard": True}]
    assert BG.honoured_summary(h) == ("rows on the output: 1 honoured, 1 hard not honoured (H1), 1 soft not honoured, "
                                      "1 not measured")
