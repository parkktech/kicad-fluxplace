"""Generic pin-function groups, the magnetics profile, and the substitution diff (no network, no board file)."""
import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import pingroups as PG, partdiff as PD, magnetics as MG
from test_magnetics import SPEC, board, renet, codes


def pads_of(f, code):
    return sorted({int(x) for m in f if m["code"] == code for x in __import__("re").findall(r"pad (\d+)|T3\.(\d+)", m["msg"])
                   for x in x if x})


def test_review_profile_keeps_magnetics_codes_and_adds_nothing_on_a_clean_board():
    assert PG.check(*board(), SPEC) == []
    p, n = renet(board()[0], {"24": "L0_P", "23": "L0_N", "22": "CX0"})
    f = PG.check(p, n, SPEC)
    assert f == MG.check(p, n, SPEC)                               # the magnetics profile, codes unchanged
    assert codes(f).count("MAG_LINE_ON_CT") == 2


def test_generic_engine_on_converted_channels_finds_the_same_pads():
    spec = copy.deepcopy(SPEC)
    assert PG.check(*board(), spec, generic_channels=True) == []
    p, n = renet(board()[0], {"24": "L0_P", "23": "L0_N", "22": "CX0", "18": "L1_P", "17": "L1_N", "16": "CX1"})
    f = PG.check(p, n, spec, generic_channels=True)
    got = {m for x in f if x["level"] == "FAIL" for m in __import__("re").findall(r"pad[s]? (\d+)", x["msg"])}
    assert {"24", "22", "18", "16"} <= got
    assert {x["code"] for x in f if x["level"] == "FAIL"} == {"PINGRP_ROLE_MISUSE", "PINGRP_PAIR_SPLIT"}
    # polarity swap: WARN, FAIL when strict
    p, n = renet(board()[0], {"23": "L0_N", "22": "L0_P"})
    f = PG.check(p, n, spec, generic_channels=True)
    assert [x["code"] for x in f] == ["PINGRP_POLARITY"] or {x["code"] for x in f} <= {"PINGRP_POLARITY", "PINGRP_PAIR_SPLIT"}


ESD = {"components": [{"ref": "U1", "value": "ESD", "fp": "SOT-23-6", "pinmap_source": "e.pdf#p1",
                       "pin_groups": [{"name": "A", "roles": {"in_p": "1", "in_n": "2", "out_p": "6", "out_n": "5", "gnd": "3"},
                                       "pairs": [["in_p", "in_n"], ["out_p", "out_n"]], "tied": [["in_p", "out_p"], ["in_n", "out_n"]]}]}]}


def _esd(pads):
    parts = {"U1": {"pads": pads, "footprint": "SOT-23-6"}}
    np = {}
    for ref, p in parts.items():
        for k, v in p["pads"].items():
            np.setdefault(v, []).append((ref, k))
    return parts, np


def test_esd_array_pair_stays_a_pair_and_gnd_stays_gnd():
    ok = {"1": "D_P", "2": "D_N", "6": "D_P", "5": "D_N", "3": "GND"}
    assert PG.check(*_esd(ok), ESD) == []
    f = PG.check(*_esd(dict(ok, **{"2": "E_N"})), ESD)                    # N leg of the pair on another pair
    assert "PINGRP_PAIR_SPLIT" in codes(f) and "PINGRP_TIED_SHORT" in codes(f)
    f = PG.check(*_esd(dict(ok, **{"3": "D_P"})), ESD)                    # a signal on the ground pin
    assert codes(f, "FAIL") == ["PINGRP_ROLE_MISUSE"]
    f = PG.check(*_esd(dict(ok, **{"5": "D_P", "6": "D_N"})), ESD)       # swapped through the part
    assert "PINGRP_TIED_SHORT" in codes(f)


def test_undeclared_multichannel_part_is_flagged():
    spec = {"components": [{"ref": "U9", "value": "mux", "pinmap": {"A1": "1", "A2": "2", "A3": "3", "A4": "4", "Y": "5", "S": "6"}}]}
    f = PG.check({"U9": {"pads": {}, "footprint": "x"}}, {}, spec)
    assert codes(f) == ["PINGRP_NONE_DECLARED"]


# ---- substitution diff: the T3 case (Wurth 749020111A -> Pulse HX5084NL, same land pattern)
OLD_LINE = ["X1+", "X1-", "CX1", "CX2", "X2+", "X2-", "X3+", "X3-", "CX3", "CX4", "X4+", "X4-"]          # pads 24 .. 13
NEW_LINE = ["MCT1", "MX1+", "MX1-", "MCT2", "MX2+", "MX2-", "MCT3", "MX3+", "MX3-", "MCT4", "MX4+", "MX4-"]
OLD_CH = [dict(chip_p=str(3 * n + 2), chip_n=str(3 * n + 3), chip_ct=str(3 * n + 1), line_p=a, line_n=b, line_ct=c)
          for n, (a, b, c) in enumerate([("24", "23", "22"), ("20", "19", "21"), ("18", "17", "16"), ("14", "13", "15")])]
NEW_CH = [dict(chip_p=str(3 * n + 2), chip_n=str(3 * n + 3), chip_ct=str(3 * n + 1), line_p=str(23 - 3 * n), line_n=str(22 - 3 * n),
               line_ct=str(24 - 3 * n)) for n in range(4)]


def t3(mpn, line, ch, src, with_groups=True):
    c = {"ref": "T3", "value": mpn, "mpn": mpn, "fp": "H5084", "pinmap_source": src,
         "pinmap": {n: str(24 - i) for i, n in enumerate(line)}}
    if with_groups:
        c["channels"] = ch
    return c


def specs(with_groups=True, new_src="HX5084NL.pdf#p2"):
    nets = {}
    for i in range(24):
        nets.setdefault(f"N{i + 1}", []).append(["T3", str(i + 1)])
    old = {"components": [t3("749020111A", OLD_LINE, OLD_CH, "749020111A.pdf#p3", with_groups)], "nets": nets}
    new = {"components": [t3("HX5084NL", NEW_LINE, NEW_CH, new_src, with_groups)], "nets": nets}
    return old, new


@pytest.mark.parametrize("groups", [True, False])
def test_part_diff_names_the_pads_whose_function_moved(groups):
    old, new = specs(groups)
    f, tables = PD.compare_specs(old, new)
    pads = sorted(int(x["msg"].split("pad ")[1].split(" ")[0]) for x in f if x["code"] == "PART_SUBSTITUTION_PINOUT")
    if groups:
        assert pads == [16, 17, 18, 22, 23, 24]                      # by role: vendor pin NAMES do not matter
    else:
        assert {16, 17, 18, 22, 23, 24} <= set(pads)                  # names only: also the renamed ones (stated)
    assert not [x for x in f if x["code"] == "PART_SUBSTITUTION_UNVERIFIED"]
    m = next(x["msg"] for x in f if " pad 23 " in x["msg"])
    assert "was X1-" in m and "MX1+" in m and "N23" in m


def test_unrefreshed_pinmap_source_is_unverified():
    old, new = specs()
    new["components"][0]["pinmap_source"] = old["components"][0]["pinmap_source"]
    f, _ = PD.compare_specs(old, new)
    assert "PART_SUBSTITUTION_UNVERIFIED" in [x["code"] for x in f]
    del new["components"][0]["pinmap_source"]
    assert "PART_SUBSTITUTION_UNVERIFIED" in [x["code"] for x in PD.compare_specs(old, new)[0]]
    same = specs()
    same[1]["components"][0]["mpn"] = same[0]["components"][0]["mpn"]
    assert PD.compare_specs(*same)[0] == []                          # an unchanged MPN is not a substitution


def test_cli_part_diff_two_specs_and_new_mpn_question(tmp_path, capsys):
    old, new = specs()
    po, pn = tmp_path / "old.json", tmp_path / "new.json"
    po.write_text(json.dumps(old))
    pn.write_text(json.dumps(new))
    with pytest.raises(SystemExit) as e:
        cli.main(["part-diff", str(po), str(pn)])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "PART_SUBSTITUTION_PINOUT" in out and "pad 22" in out
    # --new-mpn with no pin table anywhere: a question for the datasheet, never a guess
    with pytest.raises(SystemExit) as e:
        cli.main(["part-diff", "--spec", str(po), "--ref", "T3", "--new-mpn", "HX9999"])
    assert e.value.code == 3
    assert "part-diff-pinmap:HX9999" in capsys.readouterr().out
    # ... or with its pin table given
    np_ = tmp_path / "newpart.json"
    np_.write_text(json.dumps({k: new["components"][0][k] for k in ("pinmap", "channels", "pinmap_source")}))
    with pytest.raises(SystemExit) as e:
        cli.main(["part-diff", "--spec", str(po), "--ref", "T3", "--new-mpn", "HX5084NL", "--new-part", str(np_)])
    assert e.value.code == 1 and "PART_SUBSTITUTION_PINOUT" in capsys.readouterr().out
