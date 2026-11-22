import json
import os

from fluxplace import events_sample as ES

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "events-sample")
KINDS = {"run_start", "stage_start", "stage_end", "progress", "iteration", "log", "snapshot", "parts", "artifact",
         "eta", "run_end"}


def _check_w(W):
    raw = open(os.path.join(W, "events.jsonl"), "rb").read().splitlines()
    rows = [json.loads(x) for x in raw]
    assert {r["kind"] for r in rows} == KINDS
    assert all(len(x) < 64 * 1024 for x in raw)
    assert all(r["v"] == 1 and "ts" in r and "seq" in r and "pid" in r for r in rows)
    snaps = [r for r in rows if r["kind"] == "snapshot"]
    assert sum(1 for r in snaps if r["full"]) == 2 and sum(1 for r in snaps if not r["full"]) == 3
    for r in snaps:
        assert os.path.exists(os.path.join(W, r["path"]))
    fulls = {json.load(open(os.path.join(W, r["path"])))["seq"] for r in snaps if r["full"]}
    for r in snaps:
        if not r["full"]:
            d = json.load(open(os.path.join(W, r["path"])))
            assert d["base"] in fulls
    assert json.load(open(os.path.join(W, "snap", "latest.json")))["seq"] == 4
    assert open(os.path.join(W, "png", "lookahead-1.png"), "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert json.load(open(os.path.join(W, "eta.json")))["model"]
    assert json.load(open(os.path.join(W, "run.json")))["run"] == ES.RUN
    return rows


def test_sample_has_every_kind_and_snapshot_mix(tmp_path):
    out = ES.sample(tmp_path / "S")
    _check_w(out)


def test_committed_fixture_matches_generator(tmp_path):
    assert os.path.isdir(FIXTURE), "run: python3 -m fluxplace.events --sample tests/fixtures/events-sample"
    _check_w(FIXTURE)


def test_replay_copies_and_reemits(tmp_path):
    src = ES.sample(tmp_path / "S")
    n_src = len(open(os.path.join(src, "events.jsonl")).read().splitlines())
    n = ES.replay(src, tmp_path / "R", speed=1e9)
    assert n == n_src
    rows = _check_w(str(tmp_path / "R"))
    assert len(rows) == n_src


def test_main_usage_and_args(tmp_path, capsys):
    assert ES.main(["--bogus"]) == 2
    assert ES.main(["--sample", str(tmp_path / "M")]) == 0
    assert ES.main(["--replay", str(tmp_path / "M"), "--speed", "1000", str(tmp_path / "N")]) == 0
    assert os.path.exists(tmp_path / "N" / "events.jsonl")
