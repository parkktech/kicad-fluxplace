"""fluxplace corpus: manifest, scorecard run on two pcbnew-built boards, compare rules, repeat probe, crash capture."""
import argparse, hashlib, json, os, sys

import pytest

from fluxplace import corpus as C

pcbnew = pytest.importorskip("pcbnew")


def _board(path, routed):
    """A tiny pcbnew-built board: 2 SMD parts on one net, optionally joined by a track."""
    b = pcbnew.BOARD()
    net = pcbnew.NETINFO_ITEM(b, "N1")
    b.Add(net)
    for ref, x in (("R1", 20.0), ("R2", 30.0)):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(int(x * 1e6), int(20e6)))
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetSize(pcbnew.VECTOR2I(int(1e6), int(1e6)))
        pad.SetPosition(pcbnew.VECTOR2I(int(x * 1e6), int(20e6)))
        pad.SetNet(net)
        fp.Add(pad)
        b.Add(fp)
    if routed:
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(int(20e6), int(20e6)))
        t.SetEnd(pcbnew.VECTOR2I(int(30e6), int(20e6)))
        t.SetWidth(int(0.25e6))
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(net)
        b.Add(t)
    corners = [(10, 10), (40, 10), (40, 30), (10, 30)]
    for i in range(4):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pcbnew.VECTOR2I(int(x0 * 1e6), int(y0 * 1e6)))
        s.SetEnd(pcbnew.VECTOR2I(int(x1 * 1e6), int(y1 * 1e6)))
        s.SetWidth(int(0.1e6))
        b.Add(s)
    b.Save(str(path))
    return str(path)


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _sc(boards):
    return {"schema": 1, "tool": {"git_sha": "t", "tier": "fast"}, "generated": "t", "boards": boards}


def _board_entry(metrics, runtime=None, name="b"):
    runtime = runtime or {}
    steps = {s: {"exit": 0, "crashed": False, "runtime_s": t} for s, t in runtime.items()}
    return {"name": name, "metrics": metrics, "runtime": runtime, "steps": steps, "runtime_s": 0,
            "inputs_unchanged": True}


RULES = dict(C.RULES)


def test_manifest_resolves_paths_and_rejects_bad_entries(tmp_path):
    (tmp_path / "x.kicad_pcb").write_text("")
    m = tmp_path / "corpus.toml"
    m.write_text('[[board]]\nname = "one"\nboard = "x.kicad_pcb"\ntier = ["fast"]\n')
    boards, rules = C.load_manifest(str(m))
    assert boards[0]["board"] == str(tmp_path / "x.kicad_pcb")
    assert rules["drc_noise"] == 3
    m.write_text('[[board]]\nname = "a"\nboard = "x.kicad_pcb"\n[[board]]\nname = "a"\nboard = "x.kicad_pcb"\n')
    with pytest.raises(ValueError, match="duplicate"):
        C.load_manifest(str(m))
    m.write_text('[[board]]\nname = "a"\nboard = "x.kicad_pcb"\ntier = ["slow"]\n')
    with pytest.raises(ValueError, match="tier"):
        C.load_manifest(str(m))
    m.write_text('[[board]]\nname = "a"\nboard = "missing.kicad_pcb"\n')
    with pytest.raises(FileNotFoundError):
        C.load_manifest(str(m))


def test_compare_flags_seeded_regressions_and_ignores_drc_noise():
    base = _sc({"b": _board_entry({"drc.errors": 10, "drc.unconnected": 0, "plan.verdict": "PASS",
                                   "review.FAIL": 3, "review.FAIL.SILK_TOO_SMALL": 3,
                                   "review.verdict": "FAIL", "lint.total": 2},
                                  {"review": 10.0})})
    # DRC +3 is noise; a new review code is a new check, not a regression
    noisy = _sc({"b": _board_entry({"drc.errors": 13, "drc.unconnected": 0, "plan.verdict": "PASS",
                                    "review.FAIL": 4, "review.FAIL.SILK_TOO_SMALL": 3,
                                    "review.FAIL.NEW_CHECK": 1, "review.verdict": "FAIL", "lint.total": 2},
                                   {"review": 10.0})})
    cmp = C.compare(noisy, base, RULES)
    assert cmp["verdict"] == "OK", cmp["regressions"]
    assert any("NEW_CHECK" in x for x in cmp["new_checks"])
    # seeded regressions: DRC beyond the margin, open connections, PASS->FAIL, FAIL-code count up, crash
    seeded = _sc({"b": _board_entry({"drc.errors": 14, "drc.unconnected": 2, "plan.verdict": "FAIL",
                                     "review.FAIL": 5, "review.FAIL.SILK_TOO_SMALL": 4,
                                     "review.verdict": "FAIL", "lint.total": 2, "crash.lint": 1},
                                    {"review": 25.0})})
    regs = " | ".join(C.compare(seeded, base, RULES)["regressions"])
    for needle in ("DRC errors 10 -> 14", "open connections 0 -> 2", "plan.verdict PASS -> FAIL",
                   "review.FAIL.SILK_TOO_SMALL 3 -> 4", "crash.lint", "runtime review"):
        assert needle in regs, (needle, regs)


def test_compare_runtime_needs_factor_and_floor():
    base = _sc({"b": _board_entry({}, {"review": 10.0})})
    assert C.compare(_sc({"b": _board_entry({}, {"review": 14.0})}), base, RULES)["verdict"] == "OK"
    assert C.compare(_sc({"b": _board_entry({}, {"review": 16.0})}), base, RULES)["verdict"] == "REGRESSION"
    small = _sc({"b": _board_entry({}, {"lint": 1.0})})
    assert C.compare(_sc({"b": _board_entry({}, {"lint": 2.6})}), _sc({"b": _board_entry({}, {"lint": 1.0})}),
                     RULES)["verdict"] == "OK" and small  # +1.6 s under the 2 s floor


def test_runtime_not_compared_for_steps_that_errored_in_baseline():
    base = _sc({"b": {**_board_entry({"error.lint": 1}, {"lint": 0.2}), "steps": {
        "lint": {"exit": 2, "crashed": False, "runtime_s": 0.2}}}})
    cur = _sc({"b": _board_entry({}, {"lint": 3.0})})
    cmp = C.compare(cur, base, RULES)
    assert cmp["verdict"] == "OK" and any("lint not comparable" in n for n in cmp["notes"])


def test_repeat_diff_reports_only_differing_metrics():
    r1 = _sc({"b": _board_entry({"plan.score": 262.0, "drc.errors": 5})})
    r2 = _sc({"b": _board_entry({"plan.score": 262.0, "drc.errors": 6})})
    assert C.repeat_diff([r1, r2]) == {"b": {"drc.errors": [5, 6]}}
    assert C.repeat_diff([r1, r1]) == {}


def test_failed_tool_run_is_crash_and_traceback_is_recorded(tmp_path):
    log = tmp_path / "log.txt"
    r = C._run([sys.executable, "-c", "raise ValueError('seeded')"], str(tmp_path), str(log), 60)
    assert r["crashed"] and r["exit"] == 1
    assert "seeded" in log.read_text()


def test_end_to_end_scorecard_and_baseline_compare(tmp_path):
    ok = _board(tmp_path / "routed.kicad_pcb", routed=True)
    bare = _board(tmp_path / "bare.kicad_pcb", routed=False)
    manifest = tmp_path / "corpus.toml"
    manifest.write_text(
        f'[[board]]\nname = "routed"\nboard = "{ok}"\n'
        f'[[board]]\nname = "bare"\nboard = "{bare}"\n')
    before = {p: _sha(p) for p in (ok, bare)}
    from fluxplace.corpus import add_parsers
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers(dest="cmd"))
    out, work = tmp_path / "out", tmp_path / "work"
    a = p.parse_args(["corpus", "--manifest", str(manifest), "--out", str(out), "--work", str(work),
                      "--jobs", "2"])
    assert C.main(a) == 0
    sc = json.loads((out / "fast" / "scorecard.json").read_text())
    assert set(sc["boards"]) == {"routed", "bare"}
    assert all(b["inputs_unchanged"] for b in sc["boards"].values())
    assert {p: _sha(p) for p in (ok, bare)} == before, "originals were modified"
    routed = sc["boards"]["routed"]["metrics"]
    assert routed["board.tracks"] == 1 and routed["board.vias"] == 0
    assert "fab.gerber" in routed or "error.fab" in routed, "fully routed board should reach fab"
    assert "fab.gerber" not in sc["boards"]["bare"]["metrics"], "unrouted board must not reach fab"
    assert not any(k.startswith("crash.") for b in sc["boards"].values() for k in b["metrics"])
    # same scorecard as its own baseline: no regression, exit 0
    base = tmp_path / "base.json"
    base.write_text((out / "fast" / "scorecard.json").read_text())
    a2 = p.parse_args(["corpus", "--manifest", str(manifest), "--out", str(tmp_path / "out2"),
                       "--work", str(tmp_path / "work2"), "--baseline", str(base), "--jobs", "2"])
    assert C.main(a2) == 0
    assert (tmp_path / "out2" / "fast" / "compare.md").exists()
