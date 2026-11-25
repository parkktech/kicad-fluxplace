"""placement-sides question: asked before placement when [placement] sides is undecided; answer recorded in the constraints."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_questions import args, bare_board, FLOORS
from fluxplace import questions as QS


def _q(b):
    return {x["id"]: x for x in json.load(open(QS.questions_path(b)))["questions"]}


def test_asked_with_default_both_light_back(tmp_path):
    b = bare_board(tmp_path)
    with pytest.raises(SystemExit) as e:
        QS.guard(args(), "modules", b, FLOORS, None)
    assert e.value.code == 3
    q = _q(b)["placement-sides"]
    assert q["default"] == "both-light-back"
    assert {o["choice"] for o in q["options"]} == {"front", "both", "both-light-back"}
    assert q["settle"].startswith("[placement] sides")


def test_not_asked_when_sides_declared(tmp_path):
    b = bare_board(tmp_path)
    S, cons, _ = QS.guard(args(), "floorplan", b, dict(FLOORS, placement={"sides": "front"}), None)
    assert "placement-sides" not in {x["id"] for x in S.open}


def test_found_lists_back_side_keepout(tmp_path):
    b = bare_board(tmp_path)
    cons = {"placement": {"keepout": {"NOSEAT": {"rect": [0, 0, 10, 10], "side": "B"},
                                      "FRONTONLY": {"rect": [0, 0, 5, 5], "side": "F"}}}}
    with pytest.raises(SystemExit):
        QS.guard(args(), "modules", b, dict(FLOORS, **cons), None)
    found = _q(b)["placement-sides"]["found"]
    assert "back-side closed areas: NOSEAT" in found and "FRONTONLY" not in found


def test_answer_both_sets_constraint(tmp_path):
    b = bare_board(tmp_path)
    S, cons, _ = QS.guard(args(answer=["placement-sides=both"]), "modules", b, FLOORS, None)
    assert cons["placement"]["sides"] == "both"


def test_both_light_back_asks_back_max_height(tmp_path):
    b = bare_board(tmp_path)
    with pytest.raises(SystemExit):
        QS.guard(args(answer=["placement-sides=both-light-back"]), "modules", b, FLOORS, None)
    q = _q(b)["back-max-height"]
    assert q["default"] == "none" and {o["choice"] for o in q["options"]} == {"2.0", "3.0", "none"}
    S, cons, _ = QS.guard(args(answer=["placement-sides=both-light-back", "back-max-height=2.0"]), "modules", b, FLOORS, None)
    assert cons["placement"]["back_max_height_mm"] == 2.0 and cons["placement"]["sides"] == "both-light-back"
