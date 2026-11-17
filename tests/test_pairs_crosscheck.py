"""pairs.crosscheck_external: pairs the external pair router did not recognise or did not route as a pair (synthetic summaries
shaped like route_diff's --json-out and log)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import pairs as PR

PAIRS = [("A_DM", "A_DP"), ("B_N", "B_P"), ("C_N", "C_P"), ("D_N", "D_P"), ("E_N", "E_P")]
SUMMARY = {"routed_diff_pairs": ["B", "C"], "member_incomplete_pairs": ["C"], "single_ended_diff_pairs": ["D"],
           "routed_single": ["A_DM", "A_DP"],
           "pair_reports": [{"pair": "B", "p_net": "B_P", "n_net": "B_N", "outcome": "coupled", "incomplete_members": [], "no_coupled_trunk": False},
                            {"pair": "D", "p_net": "D_P", "n_net": "D_N", "outcome": "deferred", "failure_stage": "pre-route",
                             "failure_reason": "no corridor", "incomplete_members": []}]}


def by(rows):
    return {r["pair"]: r for r in rows}


def test_unrecognised_single_ended_and_incomplete_pairs_are_listed():
    rows = by(PR.crosscheck_external(PAIRS, SUMMARY))
    assert set(rows) == {"A_DM/A_DP", "C_N/C_P", "D_N/D_P", "E_N/E_P"}            # B coupled and complete: not listed
    assert rows["A_DM/A_DP"]["reason"] == "not_recognised" and "single-ended" in rows["A_DM/A_DP"]["detail"]
    assert rows["C_N/C_P"]["reason"] == "not_routed_as_pair" and "member_incomplete_pairs" in rows["C_N/C_P"]["detail"]
    assert rows["D_N/D_P"]["reason"] == "not_routed_as_pair" and "deferred at pre-route" in rows["D_N/D_P"]["detail"]
    assert rows["E_N/E_P"]["reason"] == "not_recognised" and "absent" in rows["E_N/E_P"]["detail"]


def test_log_only_no_pairs_found_names_the_patterns(tmp_path):
    log = ("Routing 2 nets as differential pairs: ['A_DM', 'A_DP']\n"
           "Error: No differential pairs found matching the patterns!\n"
           "  Patterns provided: ['A_DM', 'A_DP']\n")
    p = tmp_path / "pair.log"
    p.write_text(log)
    rows = PR.crosscheck_external(PAIRS[:2], log=str(p))
    assert [r["pair"] for r in rows] == ["A_DM/A_DP", "B_N/B_P"]
    assert "matched no differential pair" in rows[0]["detail"] and rows[1]["reason"] == "not_recognised"


def test_log_stems_count_as_recognised_and_json_path_is_read(tmp_path):
    import json
    j = tmp_path / "s.json"
    j.write_text(json.dumps({"routed_diff_pairs": ["B"]}))
    assert PR.crosscheck_external(PAIRS[1:2], str(j)) == []
    assert PR.crosscheck_external(PAIRS[1:2], log="Found 1 differential pair(s)\n[1/1] Routing diff pair B\n") == []
    assert PR.crosscheck_external([], SUMMARY) == []


def test_short_stem_does_not_claim_an_unrelated_pair():
    assert [r["pair"] for r in PR.crosscheck_external([("BT3_DM", "BT3_DP")], {"routed_diff_pairs": ["B"]})] == ["BT3_DM/BT3_DP"]
    assert PR.crosscheck_external([("BT3_DM", "BT3_DP")], {"routed_diff_pairs": ["BT3_D"]}) == []
