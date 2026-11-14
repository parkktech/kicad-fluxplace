"""fix-c #14: one skew precedence: the best-ranked (longest pair family / explicit rule) wins across mm and ps."""
from fluxplace import constraints as C


def test_longer_ps_family_beats_shorter_mm_family():
    cons = {"pairs": {"USB": {"skew_mm": 0.1}, "USB_DP": {"skew_ps": 5}}}
    mm, src = C.skew_limit_source(cons, "USB_DP_1", ps_per_mm=lambda m: 6.0)
    assert abs(mm - 5 / 6.0) < 1e-9 and src == "[pairs.USB_DP] skew_ps"


def test_explicit_rule_skew_ps_overrides_legacy_mm():
    cons = {"pairs": {"USB": {"skew_mm": 0.1}},
            "rule": [{"name": "dp", "select": {"net": "^USB_DP$"}, "set": {"skew_ps": 3}}]}
    mm, _src = C.skew_limit_source(cons, "USB_DP", ps_per_mm=lambda m: 6.0)
    assert abs(mm - 0.5) < 1e-9


def test_mm_only_family_unchanged():
    cons = {"pairs": {"USB": {"skew_mm": 0.1}}}
    assert C.skew_limit_source(cons, "USB_DP") == (0.1, "[pairs.USB] skew_mm")
