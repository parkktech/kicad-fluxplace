import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import progress as PG


def _board(d, name="b.kicad_pcb", txt="(kicad_pcb)"):
    p = d / name
    p.write_text(txt)
    return str(p)


def _hist(*metrics):
    return {"history": [{"ts": f"2026-01-01T00:0{i}:00+00:00", "board": "b", "rev": "r", "metrics": m, "note": ""}
                        for i, m in enumerate(metrics)], "blocked": [], "accepted": None}


def _call(argv, rc=1, ts="2026-01-01T01:00:00+00:00"):
    return {"ts": ts, "cmd": argv[0], "argv": argv, "rc": rc}


def test_checkpoint_append_and_accepted(tmp_path):
    b = _board(tmp_path)
    st, cp = PG.checkpoint(b, None, "first", {"unconnected": 4})
    b2 = _board(tmp_path, txt="(kicad_pcb x)")
    st, cp2 = PG.checkpoint(b2, None, "second", {"unconnected": 2})
    d = json.load(open(tmp_path / ".fluxplace" / "progress.json"))
    assert len(d["history"]) == 2 and d["accepted"]["board"] == b and d["accepted"]["note"] == "second"
    assert cp["rev"] != cp2["rev"] and len(cp["rev"]) == 12 and d["history"][0]["metrics"] == {"unconnected": 4}


def test_measure_mocked_and_failure(tmp_path):
    b = _board(tmp_path)
    st, cp = PG.checkpoint(b, None, "", {"x": 1}, True, measure_fn=lambda p: {"unconnected": 3, "errors": 9})
    assert cp["metrics"] == {"x": 1, "unconnected": 3, "errors": 9}

    def bad(p):
        raise RuntimeError("no cli")
    with pytest.raises(RuntimeError):
        PG.checkpoint(b, None, "", {}, True, measure_fn=bad)
    assert len(PG.load(str(tmp_path))["history"]) == 1


def test_block(tmp_path):
    PG.block("NET_A", "needs placement change", str(tmp_path))
    assert PG.load(str(tmp_path))["blocked"][0]["item"] == "NET_A"


def test_parse_metrics():
    assert PG.parse_metrics(["a=1", "b=2.5"]) == {"a": 1, "b": 2.5}
    with pytest.raises(ValueError):
        PG.parse_metrics(["a"])


def test_no_data(tmp_path):
    v, ev = PG.verdict(PG.load(str(tmp_path)), [])
    assert v == "progressing" and ev == ["no data"]
    assert PG.assess(str(tmp_path))[2] == "progressing"


def test_looping_same_failing_command():
    st = _hist({"unconnected": 3})
    calls = [_call(["route", "b"])] * 3
    assert PG.verdict(st, calls)[0] == "looping"
    assert PG.verdict(st, calls[:2])[0] == "progressing"
    assert PG.verdict(st, [_call(["route", "b"]), _call(["route", "b"], rc=0), _call(["route", "b"])])[0] == "progressing"


def test_looping_oscillation():
    st = _hist({"u": 5}, {"u": 3}, {"u": 5}, {"u": 3}, {"u": 5})
    assert PG.verdict(st, [])[0] == "looping"
    assert PG.verdict(_hist({"u": 5}, {"u": 4}, {"u": 3}, {"u": 2}), [])[0] == "progressing"


def test_looping_blocked_item():
    st = _hist({"u": 1})
    st["blocked"] = [{"item": "NET_A", "reason": "r", "ts": "t"}]
    calls = [_call(["route", "--nets", "NET_A"], rc=0, ts=f"2026-01-01T01:0{i}:00+00:00") for i in range(4)]
    v, ev = PG.verdict(st, calls)
    assert v == "looping" and "NET_A" in ev[0]
    assert PG.verdict(st, calls[:3])[0] == "progressing"


def test_stalled_no_improvement_and_many_calls():
    st = _hist(*[{"u": 4}] * 6)
    assert PG.verdict(st, [])[0] == "stalled"
    assert PG.verdict(_hist({"u": 4}, {"u": 4}), [_call(["x", str(i)], 0) for i in range(61)])[0] == "stalled"
    assert PG.verdict(_hist({"u": 4}, {"u": 4}), [_call(["x", str(i)], 0) for i in range(60)])[0] == "progressing"
    assert PG.verdict(_hist(*([{"u": 4}] * 5 + [{"u": 3}])), [])[0] == "progressing"


def test_calls_since_checkpoint_and_exit_codes(tmp_path, capsys):
    b = _board(tmp_path)
    PG.checkpoint(b, None, "", {"unconnected": 3})
    fl = tmp_path / ".fluxplace"
    old = _call(["route", "b"], ts="2000-01-01T00:00:00+00:00")
    new = [_call(["route", "b"], ts="2999-01-01T00:00:00+00:00")] * 3
    (fl / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [old] + new) + "junk\n")
    assert len(PG.calls_since(str(tmp_path), PG.load(str(tmp_path))["history"][-1]["ts"])) == 3
    with pytest.raises(SystemExit) as e:
        cli.main(["progress", "show", str(tmp_path)])
    assert e.value.code == 2
    assert capsys.readouterr().out.splitlines()[0] == f"accepted: {b}"


def test_cli_checkpoint_block_show_brief(tmp_path, capsys):
    b = _board(tmp_path)
    cli.main(["progress", "checkpoint", b, "--note", "n1", "--metric", "unconnected=4", "--metric", "pairs_not_done=2"])
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == f"brief: fluxplace progress brief --workdir {tmp_path}"
    cli.main(["progress", "checkpoint", b, "--metric", "unconnected=0", "--metric", "pairs_not_done=2"])
    cli.main(["progress", "block", "NET_Z", "--reason", "placement", "--workdir", str(tmp_path)])
    capsys.readouterr()
    cli.main(["progress", "show", str(tmp_path)])
    t = capsys.readouterr().out
    assert "NET_Z: placement" in t and "verdict: progressing" in t
    cli.main(["progress", "brief", str(tmp_path)])
    t = capsys.readouterr().out
    assert "unconnected 4 -> 0" in t and "NET_Z (placement)" in t and t.startswith("1. accepted: " + b)
    cli.main(["progress", "brief", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["verdict"] == "progressing"


def test_brief_no_data_and_missing_ledger(tmp_path):
    t, d = PG.brief(str(tmp_path))
    assert "accepted: none" in t and "7. session ledger: none" in t and d["verdict"] == "progressing"


def test_brief_do_not_retry_and_ledger(tmp_path):
    b = _board(tmp_path)
    PG.checkpoint(b, None, "", {"u": 1})
    (tmp_path / "b.kicad_pcb.session").mkdir()
    (tmp_path / "b.kicad_pcb.session" / "ledger.txt").write_text("\n".join(f"l{i}" for i in range(8)))
    calls = [_call(["route", "b"], ts="2999-01-01T00:00:00+00:00")] * 2 + [_call(["fab", "b"], ts="2999-01-01T00:00:00+00:00")]
    (tmp_path / ".fluxplace" / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in calls))
    t, d = PG.brief(str(tmp_path))
    assert "2x route b" in t and t.index("2x route b") < t.index("1x fab b")
    assert "l7" in t and "l2" not in t.split("7. session ledger")[1] and d["ledger"]["tail"][-1] == "l7"


def test_brief_truncation_keeps_first_three_and_drops_oldest_history(tmp_path):
    b = _board(tmp_path)
    for i in range(8):
        PG.checkpoint(b, None, "note-%d" % i + "x" * 40, {"u": 8 - i})
    t, _ = PG.brief(str(tmp_path), max_chars=700)
    assert len(t) <= 700 and t.splitlines()[0].startswith("1. accepted:") and "2. metrics" in t and "3. blockers" in t
    t_small, _ = PG.brief(str(tmp_path), max_chars=1)
    assert "3. blockers" in t_small                      # items 1-3 survive any cap
    full, _ = PG.brief(str(tmp_path))
    assert full.count("\n  ") >= 5
