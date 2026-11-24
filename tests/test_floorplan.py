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


def test_block_cohesion_term_is_a_spread_of_each_block():
    import numpy as np
    from fluxplace import floorplan2 as F2
    X = np.array([0.0, 10.0, 50.0, 0.0])
    Y = np.array([0.0, 0.0, 0.0, 4.0])
    fixed = np.array([False, False, False, False])
    groups = {"A": np.array([0, 1]), "B": np.array([2, 3])}
    assert F2.block_spread(X, Y, fixed, groups) == (10 + 0) + (50 + 4)
    # a block of fixed modules only is not pulled
    assert F2.block_spread(X, Y, np.array([True, True, False, False]), groups) == 54.0
    assert F2.block_spread(X, Y, fixed, {}) == 0.0


# ---- C7b: rows measured on the output board (honoured / not honoured)
def _fake_stage(monkeypatch, tmp_path, fanout_ok=True, extra=""):
    from fluxplace import floorplan2 as FP2, intent as IT
    sys.path.insert(0, os.path.dirname(__file__))
    from test_intent import module, cap
    board = g.board(tmp_path, module("U1", 50, 40) + cap("C1", 20, 20, "A"))

    def once(b, cons, spec, grow, args, text_in, wd, tag, log=print):
        res = {"tag": tag, "grow": list(grow), "escaped": 5, "required": 5 if fanout_ok else 6, "moved_modules": 1, "movable": 2,
               "source": {"cross": 0, "wl": 1.0}, "annealed": {"cross": 0, "wl": 1.0}, "secs": 0.0, "construct_failed": [],
               "legality_new": {}, "fanout_ok": fanout_ok, "rounds": [{"why": ["U1.3 blocked"]}], "board": b, "blocked": [],
               "metrics": {"RF_EXT": None, "pair_spans": []}}
        return res, None, RD.load(b)
    monkeypatch.setattr(FP2, "floorplan_once", once)
    monkeypatch.setattr(FP2, "edge_order", lambda B, cons: ["L", "R", "T", "B"])
    monkeypatch.setattr(FP2, "span_summary", lambda s: "-")
    monkeypatch.setattr(FP2, "plan_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("skipped")))
    monkeypatch.setattr(IT, "prepare", lambda b, cons, spec, wd, **k: (b, cons, None))
    monkeypatch.setattr(IT, "gate", lambda *a, **k: 0)
    c = tmp_path / "c.toml"
    c.write_text('[[separation]]\nname = "met"\na = ["U1"]\nb = ["C1"]\nmin_mm = 5\nhard = true\n'
                 '[[separation]]\nname = "violated"\na = ["U1"]\nb = ["C1"]\nmin_mm = 500\nhard = true\n' + extra)
    return board, str(c), str(tmp_path / "o.kicad_pcb")


def test_floorplan_reports_the_violated_hard_row_as_not_honoured_and_writes_budgets_json(monkeypatch, tmp_path, capsys):
    import cli
    board, c, out = _fake_stage(monkeypatch, tmp_path)
    cli.main(["floorplan", board, "--out", out, "--constraints", c, "--assume-defaults"])
    o = capsys.readouterr().out
    line = next(l for l in o.splitlines() if l.startswith("rows on the output:"))
    assert "1 hard not honoured (violated)" in line and "met" not in line.split("(")[1] and "stage FAIL" not in line
    bj = json.load(open(out[:-len(".kicad_pcb")] + ".budgets.json"))
    assert {r["name"]: r["status"] for r in bj["honoured"] if r["kind"] == "separation"} == {"met": "honoured", "violated": "not_honoured"}


def test_floorplan_measures_a_keepout_separation_and_the_line_ends_stage_fail_on_a_fail(monkeypatch, tmp_path, capsys):
    import cli
    board, c, out = _fake_stage(monkeypatch, tmp_path, fanout_ok=False,
                                extra='[placement.keepout.ant]\nrect = [30, 30, 40, 50]\nlayers = "all"\nsource = "ds p.3"\n'
                                      '[[separation]]\nname = "ko"\na = ["keepout:ant"]\nb = ["C1"]\nmin_mm = 100\nhard = true\n')
    try:
        cli.main(["floorplan", board, "--out", out, "--constraints", c, "--assume-defaults"])
    except SystemExit:
        pass
    o = capsys.readouterr().out
    line = next(l for l in o.splitlines() if l.startswith("rows on the output:"))
    assert line.endswith("(stage FAIL: rows not enforced)") and "ko" in line
    p = out[:-len(".kicad_pcb")] + ".budgets.json"
    ko = next(r for r in json.load(open(p))["honoured"] if r["name"] == "ko")
    assert ko["value_out"] is not None and ko["status"] == "not_honoured"


def test_floorplan_honoured_rows_go_on_the_event_stream_as_criteria_with_hard(monkeypatch, tmp_path, capsys):
    import cli
    board, c, out = _fake_stage(monkeypatch, tmp_path)
    ev = tmp_path / "ev" / "events.jsonl"
    ev.parent.mkdir()
    monkeypatch.setenv("FLUXPLACE_EVENTS", str(ev))
    cli.main(["floorplan", board, "--out", out, "--constraints", c, "--assume-defaults"])
    lines = [json.loads(l) for l in open(ev)]
    res = [l for l in lines if l.get("kind") == "placement_progress" and l.get("cand") == "result"]
    crit = {x["id"]: x for x in res[-1]["criteria"]}
    assert crit["violated"]["ok"] is False and crit["violated"]["hard"] is True and crit["met"]["ok"] is True
    hon = [l for l in lines if l.get("kind") == "placement_progress" and l.get("phase") == "honoured"]
    assert hon and hon[-1]["why"].startswith("rows on the output: 1 honoured, 1 hard not honoured (violated)")
    assert {x["id"]: x["hard"] for x in hon[-1]["criteria"]}["violated"] is True


def test_floorplan_with_no_rows_writes_no_budgets_json(monkeypatch, tmp_path, capsys):
    import cli
    board, c, out = _fake_stage(monkeypatch, tmp_path)
    open(c, "w").write("")
    cli.main(["floorplan", board, "--out", out, "--constraints", c, "--assume-defaults"])
    assert "rows on the output: 0 honoured" in capsys.readouterr().out
