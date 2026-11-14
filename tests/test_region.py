"""Region-scoped finish: scope parsing, DSN pin filter, the protection proof, and the run/CLI paths.

Freerouting is the fake from test_routefr (it writes the session it is configured with)."""
import json
import os
from collections import Counter

import pytest

import pcbgen as G
from test_routefr import fake, seg, write, ses_text  # noqa: F401  (fixture re-export)
from fluxplace import region as RG
from fluxplace import routediag as RD


def _board(tmp_path):
    """U1 (A,B,C,D) at (20,20), U2 (A,B,C,E) at (60,40): net A has pads on both. Existing copper:
    a closed track of net X far from U1 (must never move) and a closed track of net B inside U1's area."""
    body = (G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) +
            G.ic("U2", 60, 40, ["A", "B", "C", "E"], pitch=1.27) +
            seg((80, 10), (90, 10), "X") +
            seg((12, 12), (16, 12), "B"))
    return write(tmp_path, body, inner=False)


def _java(tmp_path, monkeypatch):
    """region.run execs `java` from PATH: put the fake Freerouting launcher there."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    (d / "java").write_text((tmp_path / "fakejava").read_text())
    (d / "java").chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")


def _import_fake_ses(monkeypatch):
    """KiCad's importer refuses the minimal sessions the fake Freerouting writes: stand in a reader for
    the wires ses_text() emits (units 0.1 um, y negated) so the copy-back path is exercised for real."""
    import pcbnew
    import re as _re

    def imp(board, ses_path):
        text = open(ses_path).read()
        for m in _re.finditer(r'\(net "([^"]+)"\s*\(wire\s*\(path (\S+) (\d+) ([^)]*)\)', text, _re.S):
            net, layer, w, coords = m.group(1), m.group(2), int(m.group(3)), m.group(4).split()
            pts = [(int(coords[i]) / 1e4, -int(coords[i + 1]) / 1e4) for i in range(0, len(coords), 2)]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(int(ax * 1e6), int(ay * 1e6)))
                t.SetEnd(pcbnew.VECTOR2I(int(bx * 1e6), int(by * 1e6)))
                t.SetLayer(board.GetLayerID(layer))
                t.SetWidth(int(w / 1e4 * 1e6))
                t.SetNet(board.FindNet(net))
                board.Add(t)
        return True

    monkeypatch.setattr(pcbnew, "ImportSpecctraSES", imp)


def _tracks(path, net=None):
    B = RD.load(path)
    return sorted((s["layer"], s["net"], s["a"], s["b"], s["w"]) for s in B["segs"]
                  if net is None or s["net"] == net)


# ----------------------------------------------------------------- parsing
def test_area_and_selection_parse(tmp_path):
    assert RG.parse_area("10,20,2,5") == (2.0, 5.0, 10.0, 20.0)
    with pytest.raises(SystemExit):
        RG.parse_area("1,2,3")
    sel = tmp_path / "s.json"
    sel.write_text(json.dumps({"nets": ["A"], "refs": ["U*"], "area": [0, 0, 10, 10]}))
    assert RG.parse_selection(str(sel)) == {"nets": ["A"], "refs": ["U*"], "area": (0.0, 0.0, 10.0, 10.0)}
    sel.write_text(json.dumps({"nets": ["A"], "bogus": 1}))
    with pytest.raises(SystemExit):
        RG.parse_selection(str(sel))


def test_scope_intersection_and_area_of(tmp_path):
    path = _board(tmp_path)
    B = RD.load(path)
    s = RG.scope_from_args(B, ["A"], None, None, None, 2, None)
    assert s["nets"] == {"A"} and s["area"] is None
    # --area-of U1 = U1's courtyard box grown by the margin
    s = RG.scope_from_args(B, None, None, None, ["U1"], 2.0, None)
    x0, y0, x1, y1 = s["area"]
    assert x0 < 20 < x1 and y0 < 20 < y1 and s["nets"] is None
    # --refs U1 with --nets B: nets touching U1 that match B only
    s = RG.scope_from_args(B, ["B", "X"], ["U1"], None, None, 2, None)
    assert s["nets"] == {"B"}
    # a regex is taken when the token is not a net name
    assert RG.scope_from_args(B, ["[ABC]"], None, None, None, 2, None)["nets"] == {"A", "B", "C"}
    # an area from the selection file intersects the --area one
    sel = tmp_path / "s.json"
    sel.write_text(json.dumps({"area": [0, 0, 30, 30]}))
    s = RG.scope_from_args(B, None, None, "10,10,100,100", None, 2, str(sel))
    assert s["area"] == (10.0, 10.0, 30.0, 30.0)
    with pytest.raises(SystemExit):          # --area does not reach U2's box: no overlap
        RG.scope_from_args(B, None, None, "0,0,1,1", ["U2"], 0, None)
    with pytest.raises(SystemExit):
        RG.scope_from_args(B, None, None, None, None, 2, None)


def test_dsn_pins_outside_area_are_dropped(tmp_path):
    dsn = tmp_path / "a.dsn"
    dsn.write_text('(network\n  (net A\n    (pins U1-1 U1-2 U2-1)\n  )\n  (net "B"\n    (pins U1-3 U2-2@1)\n  )\n)\n')
    pads = {("U1", "1"): [(20, 20)], ("U1", "2"): [(50, 50)], ("U2", "1"): [(60, 40)],
            ("U1", "3"): [(21, 21)], ("U2", "2"): [(80, 80)]}
    scope = {"nets": {"A"}, "area": (10, 10, 30, 30)}
    out = tmp_path / "f.dsn"
    dropped = RG.filter_dsn(str(dsn), str(out), scope, pads)
    t = out.read_text()
    assert dropped == 2                       # U1-2 and U2-1 are outside; A is the only net touched
    assert "U1-1" in t
    assert "U1-2" not in t and "U2-1" not in t
    assert "U1-3" in t and "U2-2@1" in t      # B is out of scope: untouched


# ------------------------------------------------------------------- proof
def test_check_identical_passes_and_each_breach_is_named(tmp_path):
    base = _board(tmp_path)
    scope = {"nets": {"A"}, "area": (10, 10, 30, 30)}
    assert RG.check(base, base, scope)["ok"]
    # a protected track removed (not ripped): FAIL with the key named
    B = open(base).read()
    gone = tmp_path / "gone.kicad_pcb"
    gone.write_text(B.replace(seg((80, 10), (90, 10), "X").strip(), ""))
    r = RG.check(base, str(gone), scope)
    assert not r["ok"] and any("X" in v for v in r["violations"])
    # the same removal, declared as rip: accepted
    rip = Counter(i["key"] for i in RG.fingerprint(base)[0] if i["net"] == "X")
    assert RG.check(base, str(gone), scope, ripped=rip)["ok"]
    # copper added on an out-of-scope net: FAIL
    add = tmp_path / "add.kicad_pcb"
    add.write_text(B[:B.rindex(")")] + seg((14, 14), (15, 14), "C") + ")\n")
    r = RG.check(base, str(add), scope)
    assert not r["ok"] and any("out-of-scope net 'C'" in v for v in r["violations"])
    # copper added on the in-scope net but outside the area: FAIL
    add.write_text(B[:B.rindex(")")] + seg((70, 60), (71, 60), "A") + ")\n")
    r = RG.check(base, str(add), scope)
    assert not r["ok"] and any("outside the area" in v for v in r["violations"])
    # copper added on the in-scope net inside the area: accepted
    add.write_text(B[:B.rindex(")")] + seg((14, 14), (15, 14), "A") + ")\n")
    assert RG.check(base, str(add), scope)["ok"]


# ---------------------------------------------------------- full run (fake jar)
def test_region_run_routes_only_the_area_and_keeps_the_rest(tmp_path, fake, monkeypatch):
    loc, set_cfg = fake
    _java(tmp_path, monkeypatch)
    _import_fake_ses(monkeypatch)
    base = _board(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    scope = RG.scope_from_args(RD.load(base), None, None, None, ["U1"], 2.0, None)
    scope["nets"] = {"A"}                     # --nets A --area-of U1
    set_cfg(ses=ses_text([("A", "F.Cu", 0.2, [(18, 19.365), (18, 19.065), (23, 19.065)]),
                          ("X", "F.Cu", 0.2, [(81, 9), (89, 9)])]))   # out-of-scope net: must be discarded
    jar = str(tmp_path / "freerouting-2.5.0.jar")
    res = RG.run(base, out, scope, jar=jar, passes=1, timeout=60, planes=())
    assert res["ok"], res["violations"]
    assert res["added_tracks"] == 2 and res["nets_kept"] == ["A"]
    assert _tracks(out, "X") == _tracks(base, "X")              # protected copper unchanged
    assert _tracks(out, "B") == _tracks(base, "B")
    assert res["hash_before"] == res["hash_after"]


def test_region_run_drops_a_net_whose_copper_leaves_the_area(tmp_path, fake, monkeypatch):
    loc, set_cfg = fake
    _java(tmp_path, monkeypatch)
    _import_fake_ses(monkeypatch)
    base = _board(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    scope = {"nets": {"A"}, "area": (10, 10, 30, 30)}
    set_cfg(ses=ses_text([("A", "F.Cu", 0.2, [(18, 19.365), (18, 40), (60, 40)])]))
    res = RG.run(base, out, scope, jar=str(tmp_path / "freerouting-2.5.0.jar"), passes=1, timeout=60, planes=())
    assert res["ok"] and res["nets_dropped"] == {"A": 2} and res["added_tracks"] == 0
    assert _tracks(out) == _tracks(base)


def test_region_run_fails_protection_when_output_is_tampered(tmp_path, fake, monkeypatch):
    loc, set_cfg = fake
    _java(tmp_path, monkeypatch)
    _import_fake_ses(monkeypatch)
    base = _board(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    scope = {"nets": {"A"}, "area": (10, 10, 30, 30)}
    set_cfg(ses=ses_text([]))

    def tamper(path):                         # move the protected X track: the proof must fail
        import pcbnew
        b = pcbnew.LoadBoard(path)
        for t in b.GetTracks():
            if t.GetNetname() == "X":
                t.Move(pcbnew.VECTOR2I(0, 1000000))
        pcbnew.SaveBoard(path, b)

    res = RG.run(base, out, scope, jar=str(tmp_path / "freerouting-2.5.0.jar"), passes=1, timeout=60, planes=(),
                 tamper=tamper)
    assert not res["ok"]
    assert any("X" in v for v in res["violations"]), res["violations"]


def test_cli_finish_fails_loudly_and_keeps_input(tmp_path, monkeypatch):
    import cli
    from fluxplace import tune as TU
    base = _board(tmp_path)
    before = open(base, "rb").read()
    fake_res = {"ok": False, "violations": ["protected copper changed/removed x1: S X"], "hash_before": "a",
                "hash_after": "b", "removed": Counter(), "added": Counter(), "in_scope_before": {},
                "in_scope_after": {}, "added_tracks": 0, "added_vias": 0, "added_mm": 0, "nets_kept": [],
                "nets_dropped": {}, "diagnose": None}

    def fake_run(board, out, scope, **kw):
        open(out, "w").write("tampered")
        return fake_res

    monkeypatch.setattr(RG, "run", fake_run)
    monkeypatch.setattr(TU, "drc_counts", lambda *a, **k: (0, 0))
    with pytest.raises(SystemExit) as e:
        cli.main(["finish", "--board", base, "--area-of", "U1", "--margin", "2"])
    assert e.value.code == 1
    assert os.path.exists(base[:-len(".kicad_pcb")] + ".rejected.kicad_pcb")
    assert open(base, "rb").read() == before
