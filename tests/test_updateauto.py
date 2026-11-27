"""w91: a board that already carries routed copper and no --from is classified against its own copper and pads; the entry stage and scope follow."""
import argparse
import os
import sys

import pytest

pytest.importorskip("pcbnew")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pipeline as PL                                      # noqa: E402
from tests import planboards as G                                         # noqa: E402
from tests.test_renet import routed, two                                  # noqa: E402
from tests.test_incplace import routed as inc_routed                      # noqa: E402

STAGES = PL.FLOWS["constructive"]


def args(board, cons=None, **kw):
    d = dict(board=board, constraints=cons, from_stage=None, to_stage=None)
    d.update(kw)
    return argparse.Namespace(**d)


def test_renet_starts_at_route_scoped_to_the_renetted_nets(tmp_path):
    u = PL._classify_update(args(routed(tmp_path, "b.kicad_pcb", ("Q", "A"))), STAGES)
    assert u["from"] == "route" and u["renet_nets"] == ["A", "B", "P", "Q"] and u["new"] == [] and len(u["scope"]) == 4
    assert u["line"].startswith("update: re-net 4 nets, 0 new parts") and u["line"].endswith("-> from route, scope 4 nets")


def test_new_parts_start_at_incremental_floorplan(tmp_path):
    p, cp = inc_routed(tmp_path)
    u = PL._classify_update(args(p, cp), STAGES)
    assert u["from"] == "floorplan" and u["new"] == ["R3", "R4", "R5"] and "N1" in u["scope"]
    assert "3 new parts" in u["line"]


def test_nothing_electrical_changed_starts_at_silk(tmp_path):
    u = PL._classify_update(args(routed(tmp_path, "b.kicad_pcb", ("B", "P"))), STAGES)
    assert u["from"] == "silk" and u["renet_nets"] == [] and u["new"] == [] and "no placement/route stages" in u["line"]


def test_board_without_copper_keeps_the_whole_flow(tmp_path):
    b = G.write(tmp_path, two("R1", 10, 10, "A", "B") + two("R2", 30, 10, "A", "B"), name="n.kicad_pcb")
    assert PL._classify_update(args(b), STAGES) is None


def test_open_connections_keep_the_whole_flow(tmp_path):
    b = routed(tmp_path, "b.kicad_pcb", ("B", "P"))
    t = open(b).read()
    t = t.replace('(segment (start 10.5 10) (end 29.5 10) (width 0.2) (layer "F.Cu") (net "B")', '(segment (start 10.5 10) (end 20 10) (width 0.2) (layer "F.Cu") (net "B")')
    open(b, "w").write(t)
    assert PL._classify_update(args(b), STAGES) is None


def _run(tmp_path, monkeypatch, b, **kw):
    seen = []
    for s in STAGES:
        monkeypatch.setitem(PL.FUNCS, s, lambda c, n, board, s=s: seen.append(s) or {"verdict": "pass", "line": "fake", "board": board})
    a = args(b, workdir=str(tmp_path / "W"), spec=None, status=False, waive=[], force=True, api=False, flow="constructive",
             route_budget=60, route_timeout=30, stall_rounds=1, replace_on_stall=False, layers=["F.Cu", "B.Cu"], track=0.2,
             clearance=0.2, router_py=sys.executable, router_dir=str(tmp_path / "krt"), **kw)
    return PL.run(a), seen, PL.load_state(a.workdir)


def test_pipeline_without_from_runs_silk_and_review_only_and_records_the_class(tmp_path, monkeypatch, capsys):
    rc, seen, st = _run(tmp_path, monkeypatch, routed(tmp_path, "b.kicad_pcb", ("B", "P")))
    assert rc == 0 and seen == ["silk", "review"]
    assert st["update"]["from"] == "silk" and st["update"]["line"].startswith("update: re-net 0 nets, 0 new parts")
    assert any(l.startswith("update:") for l in capsys.readouterr().out.splitlines())   # w93 prints its code-freeze line first


def test_explicit_from_wins(tmp_path, monkeypatch):
    rc, seen, st = _run(tmp_path, monkeypatch, routed(tmp_path, "b.kicad_pcb", ("Q", "A")), from_stage="review")
    assert rc == 0 and seen == ["review"] and "update" not in st
