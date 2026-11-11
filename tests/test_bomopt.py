"""Tests for part-count optimization — synthetic specs, no pcbnew, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import bomopt as B
from fluxplace import review as R


def comp(ref, value="", fp=None, **kw):
    if fp is None:
        fp = {"R": "R_0402_1005Metric", "C": "C_0402_1005Metric"}.get(ref[0], "X")
    return dict({"ref": ref, "value": value, "lib": "L", "fp": fp}, **kw)


def two(ref, a, b):
    return {a: [[ref, "1"]], b: [[ref, "2"]]}


def mknets(*items):
    """items: (ref, netA, netB)"""
    nets = {}
    for ref, a, b in items:
        nets.setdefault(a, []).append([ref, "1"])
        nets.setdefault(b, []).append([ref, "2"])
    return nets


def run(comps, items=(), mpn=None, cons=None):
    return B.analyze(comps, mknets(*items), mpn, cons)


def codes(res):
    return [f["code"] for f in res["findings"]]


def of(res, code):
    return [f for f in res["findings"] if f["code"] == code]


# ------------------------------------------------------------ parse_value
def test_parse_value():
    pv = B.parse_value
    assert pv("10k", "R") == 10000
    assert pv("10 k", "R") == 10000
    assert pv("4.7k", "R") == 4700
    assert pv("4k7", "R") == 4700
    assert pv("100R", "R") == 100
    assert pv("0R", "R") == 0
    assert pv("1M", "R") == 1e6
    assert pv("47k 1%", "R") == 47000
    assert abs(pv("100nF", "C") - 100e-9) < 1e-15
    assert abs(pv("4u7", "C") - 4.7e-6) < 1e-12
    assert abs(pv("2.2uH", "L") - 2.2e-6) < 1e-12
    for junk in ("", None, "abc", "10kfoo", "SMDJ18A"):
        assert pv(junk, "R") is None
    assert pv("10k", "D") is None


def test_kind_and_package():
    assert B.kind_of("FB3") == "FB" and B.kind_of("U1") == "U"
    assert B.kind_of("TVS1") == "other"
    assert B.package_of("R_0402_1005Metric") == "0402"
    assert B.package_of("C_0603_1608Metric") == "0603"
    assert B.package_of("R_Array_Convex_4x0402") == "R_Array_Convex_4x0402"


# ----------------------------------------------------------------- summary
def test_summary_counts():
    comps = [comp("R1", "10k"), comp("R2", "10k", dnp=True), comp("C1", "100n"),
             comp("U1", "MCU", fp="QFN"), comp("MH1", "", fp="MountingHole_3mm"),
             comp("TP1", ""), comp("FID1", ""), comp("J9", "x", fp="X")]
    res = run(comps, mpn={"R1": "A", "C1": "B", "U1": "C"},
              cons={"bom": {"exempt": ["J9"]}})
    s = res["summary"]
    assert s["total_parts"] == 4 and s["fitted_parts"] == 3 and s["dnp_parts"] == 1
    assert s["unique_mpns"] == 3
    assert s["by_kind"]["R"] == 1 and s["by_kind"]["C"] == 1 and s["by_kind"]["U"] == 1
    assert s["unique_values"] == 2
    assert s["passive_ratio"] == round(2 / 3, 3)
    assert s["single_use_mpns"] == 3


# ------------------------------------------------------------------ budgets
def test_budgets_only_when_set_and_exceeded():
    comps = [comp("R1", "10k"), comp("R2", "11k"), comp("R3", "12k", dnp=True)]
    assert not [f for f in run(comps)["findings"] if f["code"].startswith("BOM_BUDGET")]
    cons = {"bom": {"max_parts": 2, "max_unique": 2, "max_dnp": 0}}
    res = run(comps, cons=cons)
    assert not of(res, "BOM_BUDGET_PARTS")          # 2 fitted, not exceeded
    assert not of(res, "BOM_BUDGET_UNIQUE")
    assert of(res, "BOM_BUDGET_DNP")[0]["level"] == "WARN"
    cons = {"bom": {"max_parts": 1, "max_unique": 1}}
    res = run(comps, cons=cons)
    assert of(res, "BOM_BUDGET_PARTS")[0]["level"] == "FAIL"
    assert of(res, "BOM_BUDGET_UNIQUE")[0]["level"] == "FAIL"


# -------------------------------------------------------------------- array
def _pullups(n, value="10k", net="+3V3"):
    return ([comp(f"R{i}", value) for i in range(1, n + 1)],
            [(f"R{i}", net, f"S{i}") for i in range(1, n + 1)])


def test_array_candidate():
    c, it = _pullups(8)
    f = of(run(c, it), "BOM_ARRAY_CANDIDATE")
    assert len(f) == 1 and f[0]["level"] == "INFO" and len(f[0]["refs"]) == 8
    assert "saves 6 placement" in f[0]["msg"]
    assert f[0]["saves"]["placements"] == 6


def test_array_not_for_three_or_mixed():
    c, it = _pullups(3)
    assert not of(run(c, it), "BOM_ARRAY_CANDIDATE")
    c, it = _pullups(4)
    c[3]["value"] = "4k7"                       # one differs -> only 3 left
    assert not of(run(c, it), "BOM_ARRAY_CANDIDATE")
    c, it = _pullups(4)
    c[3]["fp"] = "R_0603_1608Metric"
    assert not of(run(c, it), "BOM_ARRAY_CANDIDATE")
    c, it = _pullups(5)
    assert of(run(c, it, cons={"bom": {"array_min": 6}}), "BOM_ARRAY_CANDIDATE") == []


# -------------------------------------------------------------------- merge
def test_value_merge_and_near_miss():
    c = [comp(f"R{i}", "4.7k") for i in range(1, 4)] + [comp("R9", "5.1k")]
    f = of(run(c), "BOM_VALUE_MERGE")
    assert len(f) == 1 and f[0]["refs"] == ["R9"]
    c = [comp(f"R{i}", "10k") for i in range(1, 4)] + [comp("R9", "12k")]   # 20 %
    assert not of(run(c), "BOM_VALUE_MERGE")
    # never across kinds or packages, never with 0
    c = [comp("R1", "100n"), comp("R2", "100n"), comp("C1", "105n")]
    assert not of(run(c), "BOM_VALUE_MERGE")
    c = [comp("R1", "10k"), comp("R2", "10k"), comp("R3", "10k", fp="R_0603_1608Metric")]
    assert not of(run(c), "BOM_VALUE_MERGE")
    c = [comp("R1", "0R"), comp("R2", "0R"), comp("R3", "0R"), comp("R4", "1R")]
    assert not of(run(c), "BOM_VALUE_MERGE")
    # equal counts: neither is "rarer"
    c = [comp("R1", "10k"), comp("R2", "10.5k")]
    assert not of(run(c), "BOM_VALUE_MERGE")


def test_value_merge_keep_values():
    c = [comp(f"R{i}", "4.7k") for i in range(1, 4)] + [comp("R9", "5.1k")]
    assert not of(run(c, cons={"bom": {"keep_values": ["R9"]}}), "BOM_VALUE_MERGE")


# ------------------------------------------------------------ package split
def test_package_split():
    c = [comp("C1", "100n"), comp("C2", "100n"), comp("C3", "0.1uF", fp="C_0603_1608Metric")]
    f = of(run(c), "BOM_PACKAGE_SPLIT")
    assert len(f) == 1 and f[0]["refs"] == ["C3"]
    c = [comp("C1", "100n"), comp("C2", "1u", fp="C_0603_1608Metric")]
    assert not of(run(c), "BOM_PACKAGE_SPLIT")


# --------------------------------------------------------------- single use
def test_single_use_is_one_finding():
    c = [comp("R1", "10k"), comp("R2", "11k"), comp("R3", "12k"), comp("R4", "12k")]
    f = of(run(c), "BOM_SINGLE_USE")
    assert len(f) == 1 and f[0]["refs"] == ["R1", "R2"]
    assert not of(run([comp("R1", "1k"), comp("R2", "1k")]), "BOM_SINGLE_USE")
    f = of(run(c, mpn={"R1": "A", "R2": "A", "R3": "B", "R4": "B"}), "BOM_SINGLE_USE")
    assert not f


# ----------------------------------------------------------------- parallel
def test_parallel_resistors_and_caps():
    c = [comp("R1", "10k"), comp("R2", "10k")]
    assert of(run(c, [("R1", "A", "B"), ("R2", "B", "A")]), "BOM_PARALLEL")
    # share only one net -> not parallel
    assert not of(run(c, [("R1", "A", "B"), ("R2", "A", "C")]), "BOM_PARALLEL")
    # bulk caps, three alike -> flagged; two -> not; bypass-size -> not
    caps = [comp(f"C{i}", "10u") for i in range(1, 4)]
    it = [(f"C{i}", "+5V", "GND") for i in range(1, 4)]
    assert of(run(caps, it), "BOM_PARALLEL")
    assert not of(run(caps[:2], it[:2]), "BOM_PARALLEL")
    byp = [comp(f"C{i}", "100n") for i in range(1, 4)]
    assert not of(run(byp, it), "BOM_PARALLEL")
    # three caps sharing only GND
    it = [("C1", "+5V", "GND"), ("C2", "+3V3", "GND"), ("C3", "+1V8", "GND")]
    assert not of(run(caps, it), "BOM_PARALLEL")


# --------------------------------------------------------------- 0-ohm, DNP
def test_series_zero_and_dnp():
    c = [comp("R1", "0R"), comp("R2", "0"), comp("R3", "10k"), comp("R4", "0R", dnp=True)]
    f = of(run(c), "BOM_SERIES_ZERO")
    assert len(f) == 1 and f[0]["refs"] == ["R1", "R2"]
    assert not of(run(c, cons={"bom": {"keep_links": ["R1", "R2"]}}), "BOM_SERIES_ZERO")
    assert of(run(c), "BOM_DNP_COUNT")[0]["refs"] == ["R4"]
    assert not of(run(c[:3]), "BOM_DNP_COUNT")


# ---------------------------------------------------------------- check_bom
SPEC = {"components": [comp("R1", "10k"), comp("R2", "11k")], "nets": {}}


def test_check_bom_silent_without_table_or_spec():
    f = {"parts": {}, "net_pads": {}, "mpn_map": {}}
    assert R.check_bom(f, {}, SPEC) == []
    assert R.check_bom(f, None, SPEC) == []
    assert R.check_bom(f, {"bom": {}}, None) == []


def test_check_bom_budget_and_summary():
    f = {"parts": {}, "net_pads": {}, "mpn_map": {}}
    out = R.check_bom(f, {"bom": {"max_parts": 1}}, SPEC)
    assert [x["code"] for x in out] == ["BOM_BUDGET_PARTS", "BOM_SUMMARY"]
    assert out[0]["level"] == "FAIL" and "bom-opt" in out[1]["msg"]
    out = R.run(dict(f), spec=SPEC, cons={"bom": {"max_parts": 5}})
    assert [x["code"] for x in out if x["code"].startswith("BOM_")] == ["BOM_SUMMARY"]
