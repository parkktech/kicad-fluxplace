"""rfroute: impedance width per layer from zgeom.single_width, mean over the outer layers; nets without a width are reported."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import rfroute as RFR, qualaudit as QA, routediag as RD, zgeom as ZG  # noqa: E402


def test_outer_layers():
    assert RFR.outer_layers(["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]) == ["F.Cu", "B.Cu"]


def test_widths_mean_of_layers_and_none_without_reference(monkeypatch):
    monkeypatch.setattr(RD, "load", lambda b: {})
    monkeypatch.setattr(QA, "_ctx", lambda b, B, c: ([], set(), None))
    w = {("A", "F.Cu"): 0.30, ("A", "B.Cu"): 0.34}
    monkeypatch.setattr(ZG, "single_width", lambda cons, st, pl, net, lay, **k: {"w": w.get((net, lay)), "z": 50.0, "err_pct": 0.0, "status": "ok"})
    r = RFR.widths("b", {}, ["A", "NOREF"], ["F.Cu", "B.Cu"])
    assert r["A"]["w"] == 0.32 and set(r["A"]["layers"]) == {"F.Cu", "B.Cu"} and r["NOREF"]["w"] is None


def test_split():
    assert RFR.split(["A", "B"], ["B", "C"]) == (["A"], ["B"])
