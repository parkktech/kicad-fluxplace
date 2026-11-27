"""Slow-tier replays of the ParkkRouter 1.5.1 / 1.5.2 / 1.5.3 update flows on stored real boards (tests/replay/data).
Each test is one stopgap condition ParkkRouter used to work around (docs/LESSONS-FLOWB rows 5, 6, 7, 12, 13, 14), asserted natively:
no fallback, constraints copied away from the project, relative [docs] datasheets path. Run: GATE_REPLAY=1 pytest tests/replay."""
import os
import pytest
from . import replaylib as R

pytestmark = pytest.mark.replay


def _replay(tmp_path_factory, ver, frm, to, timeout):
    tmp = str(tmp_path_factory.mktemp(ver))
    proj, cons = R.stage_version(tmp, ver)
    base = R.drc_counts(os.path.join(proj, "in.kicad_pcb"), os.path.join(proj, "in.kicad_pro"), os.path.join(tmp, "drc-in"))
    res = R.pipeline(proj, "in.kicad_pcb", cons, frm, to, timeout)
    res.update(base=base, proj=proj)
    if os.path.exists(res["board"]):
        res["after"] = R.drc_counts(res["board"], os.path.join(proj, "in.kicad_pro"), os.path.join(tmp, "drc-out"))
    print(f"REPLAY {ver} --from {frm} --to {to}: rc {res['rc']}, {res['secs']} s, DRC {base} -> {res.get('after')}")
    return res


@pytest.fixture(scope="module")
def v151(tmp_path_factory):
    return _replay(tmp_path_factory, "v151", "silk", "silk", 600)


@pytest.fixture(scope="module")
def v152(tmp_path_factory):
    if not R.ROUTER_OK:
        pytest.skip("router (KiCadRoutingTools venv / freerouting jar) not installed")
    return _replay(tmp_path_factory, "v152", "route", "silk", 2700)


@pytest.fixture(scope="module")
def v153(tmp_path_factory):
    if not R.ROUTER_OK:
        pytest.skip("router (KiCadRoutingTools venv / freerouting jar) not installed")
    return _replay(tmp_path_factory, "v153", "floorplan", "silk", 2700)


def _no_refusal(r):
    assert "datasheets not found" not in r["out"], r["log"]


# ---- 1.5.1: version-only re-silk on a routed board with an inherited +5V power-width gap
def test_151_datasheet_path_resolves_from_a_copied_constraints_file(v151):
    _no_refusal(v151)
    assert v151["rc"] != 4, v151["log"]


def test_151_from_silk_does_not_exit_on_an_inherited_route_gap(v151):
    assert v151["rc"] == 0, (v151["rc"], v151["log"])


def test_151_silk_fills_the_title_block_revision(v151):
    assert "1.5.1" in R.title_rev(v151["board"]), v151["log"]


def test_151_drc_under_the_project_rules_is_no_worse(v151):
    assert v151["after"]["violations"] <= v151["base"]["violations"], (v151["base"], v151["after"])


def test_151_opens_are_not_higher(v151):
    assert v151["after"]["opens"] <= v151["base"]["opens"], (v151["base"], v151["after"])


# ---- 1.5.2 / 1.5.3: re-net / incremental placement, then route on a board with inherited gaps, trace-finish enabled
@pytest.mark.parametrize("fx,ver", [("v152", "1.5.2"), ("v153", "1.5.3")])
class TestRouteUpdate:
    def test_route_gate_passes_on_pre_existing_gaps_only(self, request, fx, ver):
        r = request.getfixturevalue(fx)
        _no_refusal(r)
        assert R.stage_verdict(r["out"], "route") in ("pass", "warn"), r["log"]
        assert r["rc"] == 0, (r["rc"], r["log"])

    def test_silk_fills_the_title_block_revision(self, request, fx, ver):
        r = request.getfixturevalue(fx)
        assert ver in R.title_rev(r["board"]), r["log"]

    def test_drc_under_the_project_rules_is_no_worse(self, request, fx, ver):
        r = request.getfixturevalue(fx)
        assert r["after"]["violations"] <= r["base"]["violations"], (r["base"], r["after"])

    def test_opens_are_not_higher(self, request, fx, ver):
        r = request.getfixturevalue(fx)
        assert r["after"]["opens"] <= r["base"]["opens"], (r["base"], r["after"])

    def test_no_dangling_stub_under_0p2_mm_after_trace_finish(self, request, fx, ver):
        r = request.getfixturevalue(fx)
        assert R.stage_verdict(r["out"], "trace-finish") in ("pass", "warn"), r["log"]
        stubs = R.short_stubs(r["board"])
        assert stubs == [], stubs[:10]
