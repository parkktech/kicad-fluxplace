"""PHASE3I B5: lock classes. hard = gated pair legs and RF, soft = low-speed pair legs and audio, free = the rest."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import lockpolicy as LP

NETS = {"USB_DP", "USB_DM", "MIC_P", "MIC_N", "LED_1", "RF_OUT", "VBAT"}
CONS = {"rf": {"nets": ["RF_OUT"]}}


def test_levels_by_class():
    assert LP.level("USB_DP", CONS, NETS) == LP.HARD          # high-speed family pair leg
    assert LP.level("USB_DM", CONS, NETS) == LP.HARD          # partner found by naming
    assert LP.level("RF_OUT", CONS, NETS) == LP.HARD          # RF net
    assert LP.level("MIC_P", CONS, NETS) == LP.SOFT           # low-speed pair leg
    assert LP.level("LED_1", CONS, NETS) == LP.FREE
    assert LP.level("VBAT", CONS, NETS) == LP.FREE


def test_audio_single_net_is_soft_and_unpaired_pair_leg_is_free():
    assert LP.level("MIC_AUDIO_OUT", {}, {"MIC_AUDIO_OUT"}) == LP.SOFT
    assert LP.level("USB_DP", {}, {"USB_DP"}) == LP.FREE      # no partner on the board: not a pair leg


def test_hard_locked_filters_to_hard():
    assert LP.hard_locked(["USB_DP", "MIC_P", "LED_1", "RF_OUT"], CONS, NETS) == ["RF_OUT", "USB_DP"]
