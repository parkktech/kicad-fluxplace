"""--baseline: review/fab/deliver gate only on findings new vs the previous board."""
import argparse
import importlib.util
import json
import os
import pytest

from fluxplace import review as R

ROOT = os.path.dirname(os.path.dirname(__file__))
NETS = {"NA_P", "NA_N", "NB_P", "NB_N", "+5V"}


def F(code, msg, refs=(), level="FAIL"):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(refs)}


def mark(new, old):
    return [f["baseline"] for f in R.mark_baseline(new, old, NETS)]


def test_count_change_in_message_still_matches_as_inherited():
    old = [F("PIN_ESCAPE_BLOCKED", "U5: 2 fine-pitch pin(s) blocked: copper of GND (pin 1); copper of +5V (pin 2)", ["U5"])]
    new = [F("PIN_ESCAPE_BLOCKED", "U5: 1 fine-pitch pin(s) blocked: copper of GND (pin 1)", ["U5"])]
    assert mark(new, old) == ["inherited"]


def test_count_growing_is_new():
    old = [F("PIN_ESCAPE_BLOCKED", "U5: 1 fine-pitch pin(s) blocked", ["U5"])]
    new = [F("PIN_ESCAPE_BLOCKED", "U5: 2 fine-pitch pin(s) blocked", ["U5"])]
    assert mark(new, old) == ["new"]


def test_nets_in_message_identify_a_refless_finding():
    old = [F("PAIR_SKEW", "NA_N/NA_P: skew 1.7 mm > 1 mm limit"), F("PAIR_SKEW", "NB_N/NB_P: skew 9 mm > 1 mm limit")]
    new = [F("PAIR_SKEW", "NA_N/NA_P: skew 1.7 mm > 1 mm limit"), F("PAIR_SKEW", "NB_N/NB_P: skew 12 mm > 1 mm limit"),
           F("PAIR_SKEW", "NC_N/NC_P: skew 2 mm > 1 mm limit")]
    assert mark(new, old) == ["inherited", "new", "new"]


def test_smaller_skew_is_inherited_and_second_instance_of_a_key_is_new():
    old = [F("MODEL_FILE_MISSING", "J3 model a", ["J3"])]
    new = [F("MODEL_FILE_MISSING", "J3 model b", ["J3"]), F("MODEL_FILE_MISSING", "J3 model c", ["J3"])]
    assert mark(new, old) == ["inherited", "new"]
    assert mark([F("PAIR_SKEW", "NA_N/NA_P: skew 0.5 mm")], [F("PAIR_SKEW", "NA_N/NA_P: skew 4 mm")]) == ["inherited"]


def test_summarize_counts_only_new_and_lists_inherited():
    fs = R.mark_baseline([F("A", "x 1", ["U1"]), F("B", "y", ["U2"])], [F("A", "x 1", ["U1"])], NETS)
    lines = []
    n = R.summarize(fs, log=lines.append)
    assert n["fail"] == 1 and any("INHERITED" in l and " A " in l for l in lines)


def _cli():
    spec = importlib.util.spec_from_file_location("fp_cli_w105", os.path.join(ROOT, "cli.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_gate_aborts_only_on_new_and_json_records_baseline(tmp_path, monkeypatch):
    cli = _cli()
    board, base = tmp_path / "b.kicad_pcb", tmp_path / "p.kicad_pcb"
    board.write_text("new"), base.write_text("old")
    old = [F("OLD", "o 1", ["U1"])]
    cur = {str(board): [F("OLD", "o 1", ["U1"])], str(base): old}
    monkeypatch.setattr(cli, "_review_findings", lambda a, b, stage: ([dict(f) for f in cur[b]], {"net_pads": {}}))
    out = tmp_path / "r.json"
    a = argparse.Namespace(board=str(board), baseline=str(base), json=str(out), no_review=False)
    cli._review_gate(a, stage="review")                       # inherited only: no abort
    d = json.load(open(out))
    assert d["baseline"]["sha256"] == R.file_sha(str(base)) and d["findings"][0]["baseline"] == "inherited"
    cur[str(board)].append(F("NEW", "n", ["U2"]))
    with pytest.raises(SystemExit):
        cli._review_gate(argparse.Namespace(board=str(board), baseline=str(base), json=None, no_review=False), stage="fab")
    cli._review_gate(argparse.Namespace(board=str(board), baseline=None, json=None, no_review=False), stage="review", hard=False)
