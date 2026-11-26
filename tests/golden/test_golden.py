"""Golden microboards (ticket B2): one board per ledger mechanism, run through pipeline._exec_stage = stage function -> commit point ->
pcbnew state guard -> stage gate, the loop body `fluxplace pipeline` runs.  No --force, no lock bypass, no seeded state: the manifest is
seeded by the `pairs` stage's own commit point, as in a run.  Whole set < 3 min (kicad-cli DRC dominates: ~6 s per measure).

Mechanism -> ledger row (docs/ONE-SHOT-LEDGER.md) -> fix commit:
  swig_remove_then_load      2026-11-25 pour-tie SWIG types            w28delete 876dde8
  mask_window_via            2026-11-19/20 thermal mask window         w27maskwin 5377b72 (foreign-net via part: B7, skipped below)
  locked_power_widen         2026-11-26 PROTECTED_CHANGED on rails     w37protect 4b294de
  power_branch_tree          2026-11-26 R6 power_mag / current solve   w40flow 69d60e3
  footprint_order_shuffle    2026-11-25 ghost judge order-sensitive    (metamorphic)
  stale_pair_report          board changed after the pair report       (OUTSIDE-REVIEW 7.2)
  rejected_leaves_no_change  refused candidate leaves the committed board byte-identical
  no_kicad_pro               2026-11-25 input without .kicad_pro       open (B3): strict xfail
  datasheet_dir_cwd          2026-11-24/25 [docs] datasheets vs cwd    P5 M1 / w22rail
Owned by other tickets (placeholders below): rf_fallback B1, sealed_pin_trade B6, pair_escape_conflict / twin_receptacle /
pair_far_end_conflict B5, foreign-net via in the mask window B7."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
import pcbgen as g  # noqa: E402

pcbnew = pytest.importorskip("pcbnew")
pytestmark = pytest.mark.skipif(not shutil.which("kicad-cli"), reason="the stage gates measure with kicad-cli")
from fluxplace import pipeline as P  # noqa: E402

PY = "/usr/bin/python3"


# ------------------------------------------------------------------ boards
def with_value(ref, x, y, n1, n2, value):
    s = g.two(ref, x, y, n1, n2)
    return s.replace(f'(property "Reference" "{ref}")', f'(property "Reference" "{ref}") (property "Value" "{value}")')


def seg(a, b, w, net, layer="F.Cu", locked=False):
    return (f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}")'
            + (" (locked yes)" if locked else "") + ")\n")


def duo(ref, x, y, p, n, pitch=0.5):
    return g.fp(ref, x, y, [g.pad("1", 0, 0, p, 0.3, 0.8), g.pad("2", pitch, 0, n, 0.3, 0.8)])


MASK = '(gr_poly (pts (xy 10 10) (xy 25 10) (xy 25 25) (xy 10 25)) (stroke (width 0.1) (type solid)) (fill solid) (layer "B.Mask"))\n'
THERMAL = ('[thermal.t1]\nrect = [5, 5, 35, 28]\nside = "B"\nnet = "GND"\nmask_open = true\n'
           '[thermal.t1.via]\ndiameter = 0.46\ndrill = 0.2\npitch = 2.0\n')


def thermal_board(tmp):
    return H.write_board(tmp, g.two("R1", 8, 8, "GND", "S1") + g.two("R2", 30, 20, "GND", "S1") + MASK)


def pair_board(tmp):
    return H.write_board(tmp, duo("U1", 8, 15, "USB_P", "USB_N") + duo("J1", 30, 15, "USB_P", "USB_N"))


PAIR_CONS = '[preroute]\nsignal_layers=["F.Cu","B.Cu"]\n[pairlay]\nworkers=1\n'


def board_types(path):
    """-> (type name of one LoadBoard, number of B.Mask shapes)."""
    b = pcbnew.LoadBoard(path)
    n = sum(1 for d in b.GetDrawings() if d.GetLayer() == pcbnew.B_Mask) if b is not None else None
    return type(b).__name__, n


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def tree_hashes(d):
    return {os.path.relpath(os.path.join(r, f), d): sha(os.path.join(r, f)) for r, _ds, fs in os.walk(d) for f in fs}


# ------------------------------------------------------------------ shared stage runs (module scope: one run feeds several tests)
@pytest.fixture(scope="module")
def fanned(tmp_path_factory):
    """fanout (strips the mask window in-process) then pour-tie (loads the board): two real stages in ONE interpreter."""
    tmp = tmp_path_factory.mktemp("thermal")
    b = thermal_board(tmp)
    c = H.context(tmp, b, THERMAL)
    return {"in": b, "cons": c.cons, "tmp": tmp, "res": H.chain(c, b, ["fanout", "pour-tie"])}


@pytest.fixture(scope="module")
def paired(tmp_path_factory):
    """pairs stage on one coupled pair: lays + locks it, the commit point seeds the manifest, the pairs gate passes."""
    tmp = tmp_path_factory.mktemp("pair")
    b = pair_board(tmp)
    c = H.context(tmp, b, PAIR_CONS)
    res = H.chain(c, b, ["pairs"])
    return {"in": b, "c": c, "tmp": tmp, "res": res[0], "report": os.path.join(c.work, "1-pairs", "board.pairs.json")}


# ------------------------------------------------------------------ swig_remove_then_load / mask_window_via
def test_swig_remove_then_load(fanned):
    """w28delete: a stage that discards items in-process must leave the next stage a loadable board (LoadBoard -> BOARD)."""
    assert [r["verdict"] for r in fanned["res"]] == ["pass", "pass"], [r["line"] for r in fanned["res"]]
    for r in fanned["res"]:
        assert board_types(r["board"])[0] == "BOARD"
    assert board_types(fanned["in"])[0] == "BOARD"


def test_mask_window_via(fanned):
    """w27maskwin: the [thermal.*] mask opening leaves the working board before the first copper stage; recut re-makes it afterwards."""
    from fluxplace import thermal as TH
    assert board_types(fanned["in"])[1] == 1                          # the fixture carries the opening
    out = fanned["res"][0]["board"]
    assert board_types(out)[1] == 0, "the opening is still on the board the router would see"
    rc = out.replace(".kicad_pcb", ".recut.kicad_pcb")
    r = TH.recut(out, fanned["cons"], rc)
    assert r["blocks"] == 1 and board_types(rc) == ("BOARD", 1)


@pytest.mark.skip(reason="owner: B7 (w55-forbid): one via/track-forbidden region model; no stage lays a foreign-net via in the window")
def test_mask_window_no_foreign_via():
    raise AssertionError("placeholder")


# ------------------------------------------------------------------ locked_power_widen
def test_locked_power_widen(tmp_path):
    """w37protect: the route stage's rail widen over LOCKED rail copper must pass the commit point: the locked original is untouched,
    a wider unlocked piece lies over it.  The manifest comes from the pairs stage's commit point (it protects locked items)."""
    b = H.write_board(tmp_path, g.fp("U1", 8, 15, [g.pad("1", 0, 0, "V5", 0.6, 0.6)]) + g.fp("J1", 32, 15, [g.pad("1", 0, 0, "V5", 0.6, 0.6)])
                      + seg((8, 15), (32, 15), 0.15, "V5", locked=True))
    c = H.use_krt(H.context(tmp_path, b, '[route]\nrouter = "krt-whole"\npower_pour_over_mm = 100\n[power.V5]\nmax_current_ma = 2000\nsource = "U1"\n'))
    seed, route = H.chain(c, b, ["tune", "route"])          # tune is a seeding stage (SEED_STAGES) and, unlike pairs, has no gate to measure
    assert seed["verdict"] in ("pass", "warn") and "PROTECTED_CHANGED" not in route["line"] and "commit refused" not in route["line"], route["line"]
    assert route["line"].startswith("gate route"), route["line"]       # the commit point passed: the gate (not a refusal) wrote the line
    tracks = [(t.GetWidth() / 1e6, t.IsLocked()) for t in pcbnew.LoadBoard(route["board"]).GetTracks() if t.GetNetname() == "V5"]
    assert (0.15, True) in [(round(w, 3), k) for w, k in tracks], tracks          # the locked original is intact
    assert any(w >= 0.5 and not k for w, k in tracks), tracks                      # and a wider unlocked piece lies over it


# ------------------------------------------------------------------ power_branch_tree
def test_power_branch_tree(tmp_path):
    """w40flow: a branch carries what lies behind it, not the rail total.  Rail 2 A on paper (rail need 0.78 mm) but one 0.5 A load:
    0.15 mm tracks are enough; the power gate on the `pairs` stage ([flow.gates] override) must leave them alone."""
    from fluxplace import boardfile as BF, constraints as C, power as PWR
    body = (g.fp("U1", 8, 15, [g.pad("1", 0, 0, "V5", 0.6, 0.6)]) + with_value("R1", 32.5, 15, "V5", "GND", "10R")
            + with_value("R2", 20, 4.5, "V5", "GND", "500R")
            + seg((8, 15), (20, 15), 0.15, "V5") + seg((20, 15), (32, 15), 0.15, "V5") + seg((20, 15), (20, 5), 0.15, "V5"))
    b = H.write_board(tmp_path, body)
    cons = ('[route]\npower_pour_over_mm = 100\n[power.V5]\nmax_current_ma = 2000\nnominal_v = 5.0\nsource = "U1"\n'
            '[flow.gates]\npairs = ["power-width", "power-vias"]\n')
    c = H.context(tmp_path, b, cons)
    (r,) = H.chain(c, b, ["pairs"])
    assert r["verdict"] == "pass" and r["gate"]["stop"] == "clean", r["line"]
    assert sorted(round(t.GetWidth() / 1e6, 3) for t in pcbnew.LoadBoard(r["board"]).GetTracks() if t.GetNetname() == "V5") == [0.15] * 3
    d = {}
    cons_ = C.load(c.cons_path)
    PWR.check(BF.facts_from_file(r["board"]), cons_, PWR.dcdrop_status(r["board"], cons_), board=r["board"], detail=d)
    run = d["V5"][0]["run"]
    assert run["amps"] == pytest.approx(0.5, abs=0.01) and run["need_run"] < 0.15 < run["need"], run


# ------------------------------------------------------------------ footprint_order_shuffle (metamorphic)
def _geometry(path):
    b = pcbnew.LoadBoard(path)
    k = lambda v: round(v / 1e6, 3)
    return sorted((t.GetClass(), t.GetNetname(), k(t.GetStart().x), k(t.GetStart().y), k(t.GetEnd().x), k(t.GetEnd().y), k(t.GetWidth()))
                  for t in b.GetTracks())


def test_footprint_order_shuffle(tmp_path):
    """The same board with its footprints in the reverse order of the file gives the same fan-out evaluation and copper."""
    pads = ["A", "B", "C", "D", "E", "F", "G", "H"]
    body = g.ic("U1", 20, 15, pads, pitch=0.5) + "".join(g.two(f"R{i}", 6 + 4 * (i % 3), 4 + 6 * (i // 3), pads[i], "GND") for i in range(8))
    out = []
    for tag, flip in (("a", False), ("b", True)):
        d = tmp_path / tag
        lines = body.splitlines(keepends=True)
        assert all(l.startswith("(footprint") for l in lines)           # pcbgen writes one footprint per line
        b = H.write_board(d, "".join(reversed(lines)) if flip else body)
        c = H.context(d, b, "")
        (r,) = H.chain(c, b, ["fanout"])
        out.append((r["verdict"], re.sub(r"[\d.]+ s\b|\(\d[\d.]* s\)", "", r["line"]).split("full report")[0], _geometry(r["board"])))
    assert out[0][0] == out[1][0] and out[0][1] == out[1][1], (out[0][:2], out[1][:2])
    assert out[0][2] == out[1][2]


# ------------------------------------------------------------------ stale_pair_report / rejected_leaves_no_change
def _rip_leg(src, dst, net):
    """a board change outside the flow: every track and via of `net` deleted (Delete, in its own interpreter)."""
    code = ("import sys, pcbnew\nb = pcbnew.LoadBoard(sys.argv[1])\n"
            "for t in list(b.GetTracks()):\n    if t.GetNetname() == sys.argv[3]:\n        b.Delete(t)\n"
            "pcbnew.SaveBoard(sys.argv[2], b)\n")
    subprocess.run([PY, "-c", code, src, dst, net], check=True, capture_output=True, timeout=120)
    shutil.copy(src[:-10] + ".kicad_pro", dst[:-10] + ".kicad_pro")


def test_pairs_stage_lays_and_reports_the_pair(paired):
    r = paired["res"]
    assert r["verdict"] == "pass" and r["complete_pairs"], r["line"]
    rep = json.load(open(paired["report"]))
    assert [x["status"] for x in rep["pairs"]] == ["complete"]


def test_stale_pair_report(paired):
    """The report on disk says the pair is complete; the board changed after it (a leg ripped): the gate judges the board, not the report."""
    from fluxplace import gaps as GP
    x = paired["res"]["board"]
    y = os.path.join(paired["tmp"], "changed.kicad_pcb")
    _rip_leg(x, y, "USB_P")
    shutil.copy(paired["report"], y.replace(".kicad_pcb", ".pairs.json"))      # the stale report sits beside the changed board
    assert json.load(open(y.replace(".kicad_pcb", ".pairs.json")))["pairs"][0]["status"] == "complete"
    g_ = P._gate_measure(paired["c"], 9, "pairs", y, GP.gate_kinds(paired["c"].cons, "pairs"), src=y)
    assert g_["verdict"] == "fail", g_["line"]
    assert any("USB" in " ".join(r.get("nets") or []) for r in g_["gate"]["left"]), g_["gate"]["left"]


def test_rejected_leaves_no_change(paired, monkeypatch):
    """A candidate that rips locked copper is refused at the commit point: the result names the last good board, and every byte of the
    committed board (file, project, manifest sidecar) is as before; the candidate is not adopted."""
    committed = paired["res"]["board"]
    before = tree_hashes(os.path.dirname(committed))
    cand_dir = os.path.join(paired["tmp"], "cand")

    def ripping_stage(c, n, board):                                   # a regressed stage: the executor, commit point and guard are the real ones
        os.makedirs(cand_dir, exist_ok=True)
        out = os.path.join(cand_dir, "board.kicad_pcb")
        shutil.copy(board, out)
        _rip_leg(board, out, "USB_N")
        return {"verdict": "pass", "line": "ripped", "board": out}
    monkeypatch.setitem(P.FUNCS, "tune", ripping_stage)
    r = H.stage(paired["c"], 2, "tune", committed)
    assert r["verdict"] == "fail" and "commit refused" in r["line"] and "PROTECTED_CHANGED" in r["line"], r["line"]
    assert r["board"] == committed
    assert tree_hashes(os.path.dirname(committed)) == before


# ------------------------------------------------------------------ no_kicad_pro / datasheet_dir_cwd
@pytest.mark.xfail(strict=True, reason="open ledger row 2026-11-25 'pipeline input without .kicad_pro': B3 (w51-measure) adds the refusal; "
                                       "strict: B3 must remove this marker when its refusal lands")
def test_no_kicad_pro(tmp_path):
    """A board with no sibling .kicad_pro must not run silently on default rules: the stage refuses or says so."""
    b = H.write_board(tmp_path, thermal_board_body(), pro=False)
    assert not os.path.exists(b[:-10] + ".kicad_pro")
    c = H.context(tmp_path, b, "[fab]\nmin_track_mm = 0.1\nmin_clearance_mm = 0.1\nmin_via_dia_mm = 0.4\nmin_drill_mm = 0.2\n")   # floors given: no owner question
    (r,) = H.chain(c, b, ["fanout"])
    text = (r["line"] + " ".join(r.get("next") or [])).replace(str(tmp_path), "")
    assert r["verdict"] != "questions" and ".kicad_pro" in text, r["line"]


def thermal_board_body():
    return g.two("R1", 8, 8, "GND", "S1") + g.two("R2", 30, 20, "GND", "S1")


def test_datasheet_dir_cwd(tmp_path, monkeypatch):
    """[docs] datasheets is relative to the constraints file's project, whatever the cwd; set but missing is an error, never 0 facts."""
    from fluxplace import constraints as C
    proj = tmp_path / "proj"
    (proj / "datasheets").mkdir(parents=True)
    b = H.write_board(proj, thermal_board_body())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    c = H.context(proj, b, '[docs]\ndatasheets = "datasheets"\n')
    assert c.cons["docs"]["datasheets"] == str(proj / "datasheets")
    (r,) = H.chain(c, b, ["floorplan-check"])                           # a real stage from the foreign cwd
    assert r["verdict"] in ("pass", "warn", "na", "fail") and "board" in r
    (proj / "c2.toml").write_text('[docs]\ndatasheets = "no-such-dir"\n')
    with pytest.raises(SystemExit) as e:
        C.load(str(proj / "c2.toml"))
    assert "no-such-dir" in str(e.value)


# ------------------------------------------------------------------ owned by other tickets
@pytest.mark.skip(reason="owner: B1 (w49-netcontract): plain router cannot route RF on an illegal layer")
def test_rf_fallback():
    raise AssertionError("placeholder")


@pytest.mark.skip(reason="owner: B6 (w54-closetx): compound rung closes a 2-net sealed pocket and re-routes the displaced net")
def test_sealed_pin_trade():
    raise AssertionError("placeholder")


@pytest.mark.skip(reason="owner: B5 (w53-pairescape): pair_escape_conflict, twin_receptacle, pair_far_end_conflict")
def test_pair_escape_fixtures():
    raise AssertionError("placeholder")
