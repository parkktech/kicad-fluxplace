"""D71ci: constraints.not_pairs - nets named like a pair but declared plain signals never form a pair."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import netclass as NC, krtchain, graph as G, drcguard

NOT = {"not_pairs": ["A_P", "A_N"]}


def test_netclass_pairs_skip_not_pairs():
    out = NC._pairs(["A_P", "A_N", "B_P", "B_N"], NOT)
    assert set(out) == {"B_P", "B_N"} and out == {"B_P": "B_N", "B_N": "B_P"}


def test_pair_nets_skip_not_pairs():
    assert krtchain.pair_nets({"pairs": {"A_": {}}, "not_pairs": ["A_P", "A_N"]}, ["A_P", "A_N"], None) == []


def test_diff_pairs_exclude_keyword():
    assert G.diff_pairs(["A_P", "A_N", "B_P", "B_N"], exclude=["A_N"]) == {"B_N": "B_P"}
    assert G.diff_pairs(["A_P", "A_N"]) == {"A_N": "A_P"}


def test_protected_nets_skip_not_pairs():
    got = drcguard.protected_nets(NOT, ["A_P", "A_N", "B_P", "B_N"])
    assert {"B_P", "B_N"} <= got and not ({"A_P", "A_N"} & got)
