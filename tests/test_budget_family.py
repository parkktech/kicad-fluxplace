"""A6: a family is a classified pair's prefix; X_LED / X_CLKREQ / X_CC with a [pairs.X] family are not pairs."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import budget as B, krtchain, drcguard

NETS = ["X_TXP", "X_TXN", "X_LED", "X_CLKREQ", "X_CC", "GND"]
CONS = {"pairs": {"X": {}}}


def test_family_of_requires_a_leg():
    legs = B.pair_legs(NETS, CONS)
    assert legs == {"X_TXP", "X_TXN"}
    assert B.family_of(CONS["pairs"], "X_TXP", legs) == "X"
    for n in ("X_LED", "X_CLKREQ", "X_CC"):
        assert B.family_of(CONS["pairs"], n, legs) is None


def test_family_of_longest_prefix_and_bare_table():
    t = {"X": {}, "X_TX": {}}
    assert B.family_of(t, "X_TXP") == "X_TX"
    assert B.family_of(t, "Y_A") is None and B.family_of(None, "X") is None


def test_pair_nets_excludes_plain_signals_with_the_family_prefix():
    assert krtchain.pair_nets(CONS, NETS, None) == ["X_TXN", "X_TXP"]


def test_protected_nets_not_pair_protected():
    got = drcguard.protected_nets(CONS, NETS)
    assert got == {"X_TXP", "X_TXN"}
