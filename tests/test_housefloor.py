"""k10: with no [fab] table every floor is the strictest registry house hard minimum + margin, never the .kicad_pro minimum alone."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import limits as LM  # noqa: E402
from fluxplace import packrules as PR  # noqa: E402
from fluxplace import pairbase as PB  # noqa: E402
from fluxplace import routerules as RR  # noqa: E402
from fluxplace import zgeom as ZG  # noqa: E402

CU = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


def _board(tmp_path, track=0.09, clr=0.09):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb\n\t(layers\n" + "".join(f'\t\t({i} "{n}" signal)\n' for i, n in enumerate(CU)) + "\t)\n)\n")
    (tmp_path / "b.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rules": {"min_track_width": track, "min_clearance": clr}}}}))
    return str(pcb)


def test_house_floors_strictest_plus_margin():
    f, src = LM.house_floors({}, 6)
    hard = {k: max(LM.fab_bounds(h, 6)[k][0] for h in LM.registry_houses(6)) for k in LM.HOUSE_MARGIN}
    for k, m in LM.HOUSE_MARGIN.items():
        assert abs(f[k] - (hard[k] + m)) < 1e-9
    assert f["via_annular_min"] >= 0.13 - 1e-9 and "strictest hard minimum" in src


def test_routerules_without_fab_lifts_above_pro_and_names_source(tmp_path, capsys):
    R = RR.rules({}, _board(tmp_path), B={"parts": {}})
    hard_t = max(LM.fab_bounds(h, 6)["track_min"][0] for h in LM.registry_houses(6))
    assert R["min_track"] >= hard_t + 0.01 - 1e-9 and R["min_clearance"] >= hard_t - 1e-9
    assert R["min_ring"] >= 0.13 - 1e-9
    assert "INFO floors" in capsys.readouterr().out and R["floor_source"]["min_track"] == "house"


def test_larger_pro_floor_still_wins(tmp_path):
    R = RR.rules({}, _board(tmp_path, track=0.2, clr=0.2), B={"parts": {}})
    assert R["min_track"] == 0.2 and R["floor_source"]["min_track"] == ".kicad_pro"


def test_fab_table_untouched(tmp_path):
    R = RR.rules({"fab": {"min_track_mm": 0.1, "min_clearance_mm": 0.1}}, _board(tmp_path), B={"parts": {}})
    assert R["min_track"] == 0.1 and R["floor_source"] == {}


def test_zgeom_floors_and_pairbase_floor(tmp_path):
    b = _board(tmp_path)
    _p, ht, hs = ZG.floors({}, b)
    assert ht >= 0.11 - 1e-9 and hs >= 0.11 - 1e-9
    # a pair width at the DRC minimum is not worth keeping; the floor passed to modal_width is the house floor
    seen = {}
    orig = PB.modal_width
    PB.modal_width = lambda segs, nets, above=0.0: seen.setdefault("above", above)
    try:
        PB.widths_for(b, None, [("A", "B")], {})
    finally:
        PB.modal_width = orig
    assert seen["above"] >= 0.11 - 1e-9


def test_p4_fails_impossible_under_hard_and_names_items(tmp_path):
    from tests.test_packrules import _ctx, _status
    row = {"kind": "track", "net": "ETH_P", "layer": "F.Cu", "n": 4, "old": 0.09, "loc": "(1.0, 2.0)",
           "verdicts": {"jlcpcb": {"status": "impossible", "text": "0.09 mm is below every published jlcpcb option"}}}
    c = _ctx(tmp_path, no_pcbway=True, fabconform={"coverage": {"raw": 4, "listed": 4, "ok": True}, "rows": [row]})
    r = _status(PR.check(c), "P4")
    assert r["status"] == "FAIL" and "ETH_P" in r["reason"] and "jlcpcb" in r["reason"]
    row["verdicts"]["jlcpcb"] = {"status": "option", "text": "x"}
    assert "hard minimum" not in _status(PR.check(c), "P4")["reason"]
