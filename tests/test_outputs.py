"""Compact-by-default output layer (fluxplace/outputs.py)."""
import argparse, io, os, sys, contextlib
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import outputs as O
import cli


def _review_text(n=300):
    lines = ["REVIEW (review) - b.kicad_pcb"]
    for i in range(n):
        lines.append(f"  FAIL MODEL_MISSING                U{i} footprint X has no 3D model " + "x" * 60)
    for i in range(30):
        lines.append(f"  WARN UNROUTED_PAIR                P{i}/N{i}: a side has no routed copper")
    lines.append(f"review: {n} FAIL, 30 WARN, 0 INFO")
    return "\n".join(lines)


def test_compact_budget_and_verdict():
    txt, p = O.compact_text("review", _review_text(), 1, paths=["/x/b.review.txt"])
    assert len(txt.encode()) <= O.MAX_BYTES and txt.count("\n") < O.MAX_LINES
    assert txt.splitlines()[0].startswith("review: FAIL (exit 1)")
    assert "MODEL_MISSING" in txt and "UNROUTED_PAIR" in txt   # both codes surface
    assert "/x/b.review.txt" in txt
    assert p["counts"][("FAIL", "MODEL_MISSING")] == 300


def test_top_n_and_cap_per_code():
    txt, _ = O.compact_text("review", _review_text(), 1, top=3)
    assert "worst 3 of 330" in txt


def test_diagnose_blocks_and_moves():
    raw = ("route-diagnose - /a/b.kicad_pcb: 2 failed net(s) from board\n"
           "+3V3  (4 pads: U1.1, C1.1)\n"
           "  FAR_SUPPORT_PART: C1 is 21.7 mm from U1.1 (limit 2.5 mm)\n"
           "      -> move C1 to within 2.5 mm of U1.1\n"
           "N2  (2 pads: U1.2, R1.1)\n"
           "  CONGESTION: copper fill 9%\n"
           "SUMMARY: 2 failed net(s) diagnosed\n  FAR_SUPPORT_PART   1\n  CONGESTION         1\n"
           "MOVE LIST (ranked by nets fixed):\n"
           "  1. C1 from (1.0, 2.0) toward U1.1  fixes 1: +3V3\n")
    txt, p = O.compact_text("route-diagnose", raw, 0)
    assert "FAR_SUPPORT_PART=1" in txt and "moves: 1" in txt
    assert p["items"][0]["net"] == "+3V3" and p["items"][0]["fix"].startswith("move C1")
    assert "/a/" not in txt.splitlines()[0]                  # path shortened to basename


def _run(argv, monkeypatch, fn_out, rc=0, tmp=None):
    def fake(a):
        print(fn_out)
        if rc:
            raise SystemExit(rc)
    ap = cli.build_parser()
    a = ap.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            O.run_compact(a.cmd, a, fake)
            code = 0
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), code


def test_exit_code_preserved_and_files_written(tmp_path, monkeypatch):
    b = tmp_path / "x.kicad_pcb"; b.write_text("")
    out, code = _run(["review", "--board", str(b)], monkeypatch, _review_text(), rc=1)
    assert code == 1
    assert (tmp_path / "x.review.txt").exists() and (tmp_path / "x.review.json").exists()
    assert len(out.encode()) <= O.MAX_BYTES


def test_full_restores_raw(tmp_path, monkeypatch):
    b = tmp_path / "x.kicad_pcb"; b.write_text("")
    raw = _review_text()
    out, code = _run(["review", "--board", str(b), "--full"], monkeypatch, raw)
    assert out.strip() == raw.strip()
    out2, _ = _run(["--full", "review", "--board", str(b)], monkeypatch, raw)
    assert out2.strip() == raw.strip()


def test_quiet_is_one_line(tmp_path, monkeypatch):
    b = tmp_path / "x.kicad_pcb"; b.write_text("")
    out, code = _run(["review", "--board", str(b), "-q"], monkeypatch, _review_text(), rc=1)
    assert len(out.strip().splitlines()) == 1 and code == 1


def test_small_output_passes_with_verdict(tmp_path, monkeypatch):
    b = tmp_path / "x.kicad_pcb"; b.write_text("")
    out, _ = _run(["eval", "--board", str(b)], monkeypatch, "parts=3 HPWL=5 mm")
    assert out.splitlines()[0].startswith("eval: ok") and "parts=3" in out
    assert not (tmp_path / "x.eval.txt").exists()


def test_systemexit_message_kept(tmp_path, monkeypatch):
    b = tmp_path / "x.kicad_pcb"; b.write_text("")
    def fake(a):
        raise SystemExit("ABORT: nope")
    a = cli.build_parser().parse_args(["lint", "--board", str(b)])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as e:
        O.run_compact("lint", a, fake)
    assert e.value.code == 1 and "ABORT: nope" in buf.getvalue()


def test_drc_scope_keeps_its_own_full():
    a = cli.build_parser().parse_args(["drc-scope", "--board", "b", "--full", "--top", "3"])
    assert a.full is True and a.top == 3 and not getattr(a, "full_output", False)
