"""fix-c #15: a ps-only skew family with no known layer delay never raises out of a caller."""
import pytest
from fluxplace import constraints as C, pairs as P

PS_ONLY = {"pairs": {"USB": {"skew_ps": 5}}}


def test_raw_limit_still_raises_for_ps_without_delay():
    with pytest.raises(ValueError):
        C.skew_limit_mm(PS_ONLY, "USB_DP")


def test_checked_limit_reports_delay_unknown():
    lim, src = C.skew_limit_checked(PS_ONLY, "USB_DP")
    assert lim is None and src.startswith("PAIR_DELAY_UNKNOWN")


def test_checked_limit_with_delay_is_a_number():
    lim, src = C.skew_limit_checked(PS_ONLY, "USB_DP", ps_per_mm=6.0)
    assert abs(lim - 5 / 6.0) < 1e-9


def test_routing_guard_limit_never_raises():
    assert P.guard_limit(PS_ONLY, "USB_DP") == float("inf")
    assert P.guard_limit({"pairs": {"USB": {"skew_mm": 0.2}}}, "USB_DP") == 0.2
