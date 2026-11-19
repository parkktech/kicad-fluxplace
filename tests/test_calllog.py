import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import calllog as CL


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUXPLACE_CALL_LOG", "1")
    monkeypatch.delenv("FLUXPLACE_NO_CALL_LOG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _board(tmp_path):
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    return str(b)


def test_off_under_pytest_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_CALL_LOG", raising=False)
    assert not CL.enabled()


def test_off_switch(on, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_NO_CALL_LOG", "1")
    assert not CL.enabled()


def test_append_beside_board(on, capsys):
    b = _board(on)
    CL.run(lambda argv: print("verdict: ok\nmore"), ["lint", b])
    recs = CL.read(str(on / ".fluxplace" / "calls.jsonl"))
    assert len(recs) == 1 and recs[0]["cmd"] == "lint" and recs[0]["rc"] == 0
    assert recs[0]["argv"] == ["lint", b] and recs[0]["summary"].startswith("verdict: ok")
    assert set(recs[0]) >= {"ts", "cwd", "dur_s"}
    assert "verdict: ok" in capsys.readouterr().out


def test_exit_code_and_exception_recorded(on):
    b = _board(on)

    def f(argv):
        raise SystemExit(3)
    with pytest.raises(SystemExit):
        CL.run(f, ["review", b])
    with pytest.raises(ValueError):
        CL.run(lambda argv: (_ for _ in ()).throw(ValueError("boom")), ["fab", b])
    recs = CL.read(str(on / ".fluxplace" / "calls.jsonl"))
    assert [r["rc"] for r in recs] == [3, 1] and "boom" in recs[1]["summary"]


def test_redaction(on):
    b = _board(on)
    CL.run(lambda a: None, ["sourcing", b, "--api-key", "SECRET1", "--x", "apiKey=SECRET2", "token=SECRET3"])
    txt = (on / ".fluxplace" / "calls.jsonl").read_text()
    assert "SECRET" not in txt and "***" in txt


def test_default_location_without_path(on):
    CL.run(lambda a: None, ["doctor"])
    assert (on / "home" / ".cache" / "fluxplace" / "calls.jsonl").exists()


def test_rotation(on, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_CALL_LOG_MAX", "300")
    b = _board(on)
    for _ in range(6):
        CL.run(lambda a: None, ["lint", b])
    d = on / ".fluxplace"
    assert (d / "calls.jsonl.1").exists() and (d / "calls.jsonl").stat().st_size < 600
    assert not (d / "calls.jsonl.2").exists()


def test_log_failure_does_not_fail_command(on, monkeypatch):
    def bad(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(CL, "append", bad)
    assert CL.run(lambda a: 5, ["lint", _board(on)]) == 5


def test_tail_and_failed(tmp_path):
    p = tmp_path / "calls.jsonl"
    p.write_text("".join(json.dumps({"ts": "t", "argv": [str(i)], "rc": i % 2, "dur_s": 0}) + "\n" for i in range(10)) + "garbage\n")
    assert [r["argv"][0] for r in CL.read(str(p), 3)] == ["7", "8", "9"]
    assert [r["argv"][0] for r in CL.read(str(p), 10, failed=True)] == ["1", "3", "5", "7", "9"]


def test_calls_command(tmp_path, capsys):
    p = tmp_path / "calls.jsonl"
    p.write_text(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "argv": ["lint", "x"], "rc": 2, "dur_s": 1.5}) + "\n")
    cli.main(["calls", str(p), "--failed"])
    out = capsys.readouterr().out
    assert "rc=2" in out and "lint x" in out
    cli.main(["calls", str(p), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["rc"] == 2


def test_attempts_path_beside_calls(on):
    b = _board(on)
    assert CL.attempts_path(b) == os.path.join(os.path.dirname(CL.log_path([b])), "attempts.jsonl")
    assert os.path.basename(CL.attempts_path(b)) == "attempts.jsonl"
