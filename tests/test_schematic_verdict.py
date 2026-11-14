"""fix-c #7: --no-verify never yields PASS; block layout exits non-zero unless --allow-unverified."""
import cli


def test_no_verify_block_is_not_verified_and_fails_exit():
    v, code = cli._schematic_verdict(no_verify=True, nl_ok=True, legibility_total=0, allow_unverified=False)
    assert v == "NOT_VERIFIED" and code != 0


def test_no_verify_with_allow_flag_exits_zero_but_still_not_pass():
    v, code = cli._schematic_verdict(no_verify=True, nl_ok=True, legibility_total=0, allow_unverified=True)
    assert v == "NOT_VERIFIED" and code == 0


def test_verified_clean_passes_and_failed_netlist_fails():
    assert cli._schematic_verdict(no_verify=False, nl_ok=True, legibility_total=0, allow_unverified=False) == ("PASS", 0)
    assert cli._schematic_verdict(no_verify=False, nl_ok=False, legibility_total=0, allow_unverified=False) == ("FAIL", 1)
