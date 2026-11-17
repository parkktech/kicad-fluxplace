import argparse, os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import reportdir as RD, outputs as OUT, partscli, parts as P


def ns(**k):
    return argparse.Namespace(**k)


def test_redirect_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_REPORT_DIR", raising=False)
    RD.set_dir(None)
    stem = str(tmp_path / "in" / "b")
    assert RD.redirect(ns(), stem) == stem                                    # (c) beside the input
    ex = tmp_path / "work" / "r.json"
    assert RD.redirect(ns(json=str(ex)), stem) == str(tmp_path / "work" / "b")  # (b) explicit file's directory
    assert RD.redirect(ns(json=True), stem) == stem                           # --json to stdout: unchanged
    monkeypatch.setenv("FLUXPLACE_REPORT_DIR", str(tmp_path / "env"))
    assert RD.redirect(ns(json=str(ex)), stem) == str(tmp_path / "env" / "b")  # (a) env beats (b)
    RD.set_dir(str(tmp_path / "cli"))
    try:
        assert RD.redirect(ns(), stem) == str(tmp_path / "cli" / "b") and (tmp_path / "cli").is_dir()
    finally:
        RD.set_dir(None)


def test_announce_names_each_file(capsys):
    RD.announce("/x/a.txt", None, "/x/a.json")
    assert capsys.readouterr().err.splitlines() == ["report: /x/a.txt", "report: /x/a.json"]


def test_review_style_compact_report_follows_json_dest(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXPLACE_REPORT_DIR", raising=False)
    RD.set_dir(None)
    board = tmp_path / "src" / "b.kicad_pcb"
    board.parent.mkdir()
    board.write_text("x")
    big = "\n".join(f"  FAIL CODE{i % 3}  message number {i} " + "x" * 60 for i in range(80))
    a = ns(board=str(board), json=str(tmp_path / "work" / "out.json"), full_output=False, quiet=False, top=5)
    (tmp_path / "work").mkdir()
    OUT.run_compact("review", a, lambda a_: print(big))
    assert (tmp_path / "work" / "b.review.txt").exists() and not (board.parent / "b.review.txt").exists()


def test_parts_reports_follow_report_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FLUXPLACE_REPORT_DIR", raising=False)
    spec = tmp_path / "src" / "s.json"
    spec.parent.mkdir()
    spec.write_text("{}")
    res = {"findings": [], "skipped": {}, "unchecked": {}, "rows": [], "lookup": True}
    monkeypatch.setattr(partscli, "load", lambda a: ({}, {}, {}, {}, None))
    monkeypatch.setattr(partscli, "_session", lambda *a: type("S", (), {"gate": lambda s, want_json=False: 0, "open": []})())
    monkeypatch.setattr(P, "evaluate", lambda *a, **k: res)
    monkeypatch.setattr(P, "format_report", lambda *a, **k: "parts: ok")
    a = ns(spec=str(spec), board=None, json=True, fix=False, out=None, no_api=True, quiet=False, report_dir=str(tmp_path / "w"))
    RD.set_dir(a.report_dir)
    try:
        partscli.cmd_parts(a)
    finally:
        RD.set_dir(None)
    cap = capsys.readouterr()
    json.loads(cap.out)                                                       # stdout stays pure JSON
    assert (tmp_path / "w" / "s.parts.json").exists() and not (spec.parent / "s.parts.json").exists()
    assert f"report: {tmp_path / 'w' / 's.parts.txt'}" in cap.err
    # no report dir: beside the spec, and each file is announced
    partscli.cmd_parts(ns(**{**vars(a), "report_dir": None}))
    assert (spec.parent / "s.parts.txt").exists()
    assert f"report: {spec.parent / 's.parts.json'}" in capsys.readouterr().err
