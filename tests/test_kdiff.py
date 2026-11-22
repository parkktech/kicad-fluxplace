"""K13 calibration gate: kernel check vs kicad-cli DRC on the calibration board (ROUTER-C1 section 9/11).

Rules: false accept (kicad flags, kernel silent) must be 0. Kernel-only flags (false refusal) are allowed only
inside the measured KiCad epsilon band (shortfall < 1 um). KICAD_BLIND lists cases kicad-cli does not judge
(its shorting check does not fire for track end-to-end touches on this build); they are checked on the kernel side only.
"""
import pytest

import kdiff

KICAD_BLIND = {"different_net_touch"}


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    return kdiff.run(str(tmp_path_factory.mktemp("kdiff")))


def test_no_false_accept(rows):
    fa = [r["case"] for r in rows if r["expect_violation"] and r["kicad"] and not r["kernel"]]
    assert fa == []


def test_expectations_hold_on_kernel(rows):
    bad = [r["case"] for r in rows if r["expect_violation"] is not None and bool(r["kernel"]) != r["expect_violation"]]
    assert bad == []


def test_agreement_except_documented(rows):
    for r in rows:
        if r["expect_violation"] is None or r["case"] in KICAD_BLIND:
            continue
        if r["kernel"] and not r["kicad"]:
            # kernel-only flag: must be an epsilon-band shortfall (eps_* probes, all within 1 um)
            assert r["case"].startswith("eps_"), r["case"]
        else:
            assert bool(r["kicad"]) == bool(r["kernel"]), r["case"]


def test_kicad_band(rows):
    by = {r["case"]: r for r in rows}
    assert by["clr_minus_1um"]["kicad"] == ["clearance"]       # 1 um shortfall flagged
    assert by["eps_500nm"]["kicad"] == []                       # 500 nm shortfall accepted
    assert by["clr_at"]["kicad"] == [] and by["clr_plus_1um"]["kicad"] == []


def test_precedence_and_edge(rows):
    by = {r["case"]: r for r in rows}
    assert by["local_pad_0p3_wins"]["kernel"] == ["clearance"]
    assert by["dru_raises_0p4"]["kernel"] == ["clearance"]
    assert by["edge_minus_1um"]["kicad"] == ["edge"] and by["edge_minus_1um"]["kernel"] == ["edge"]
    assert by["edge_at"]["kernel"] == [] and by["edge_at"]["kicad"] == []


def test_touch_is_kernel_short(rows):
    by = {r["case"]: r for r in rows}
    assert by["different_net_touch"]["kernel"] == ["short"]
    assert by["different_net_touch"]["kicad"] == []  # kicad-cli blind spot, see KICAD_BLIND
