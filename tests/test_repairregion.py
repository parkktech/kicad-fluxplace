"""repair-region: sealed pocket kept, locked blocker failed with a sealing list, RF/pair targets never ripped,
margin growth, and region.run(keep_locked=True) leaving locked copper in place.

DRC is stubbed with a deterministic fake that reports the open clusters of routediag/routeconn; Freerouting is the
fake from test_routefr (through test_region's helpers)."""
import pytest

import pcbgen as G
from test_routefr import fake, seg, write, ses_text  # noqa: F401  (fixture re-export)
from test_region import _java, _import_fake_ses, _tracks
from fluxplace import region as RG, routediag as RD, routeconn as RC
from fluxplace import repairregion as RR


def _drc(path, kicad_cli="kicad-cli"):
    """Fake kicad-cli report: one unconnected item per open cluster, positioned on its pads."""
    B = RD.load(path)
    groups = {}
    RC.text_open(B, groups=groups)
    unc = []
    for net, clusters in groups.items():
        for c in clusters:
            unc.append({"type": "unconnected_items", "description": "Missing connection",
                        "items": [{"description": f"Pad {p.get('ref')}.{p.get('num')} [{net}]",
                                   "pos": {"x": p["x"], "y": p["y"]}} for p in c]})
    return {"violations": [], "unconnected_items": unc}


def _pocket(tmp_path, locked=False):
    """A has a pad on each of U1 and U2, both open. B connects its own pads diagonally and has a wall track
    above U1's A pad. With locked=True every B track is locked."""
    body = (G.ic("U1", 20, 20, ["A", "B", "C1", "D1"], pitch=1.27) +   # C1, D1, C2, E1: one pad each, never open
            G.ic("U2", 60, 40, ["A", "B", "C2", "E1"], pitch=1.27) +
            seg((18, 20.635), (58, 40.635), "B", locked=locked) +
            seg((16.5, 17.5), (30, 17.5), "B", locked=locked))
    return write(tmp_path, body, inner=False)


@pytest.fixture
def drc(monkeypatch):
    monkeypatch.setattr(RR, "_drc", _drc)


def test_sealed_pocket_is_ripped_rerouted_and_kept(tmp_path, fake, monkeypatch, drc):
    loc, set_cfg = fake
    _java(tmp_path, monkeypatch)
    _import_fake_ses(monkeypatch)
    base = _pocket(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    set_cfg(ses=ses_text([("A", "F.Cu", 0.2, [(18, 19.365), (58, 39.365)]),
                          ("B", "F.Cu", 0.2, [(18, 20.635), (58, 40.635)])]))
    res = RR.repair(base, out, jar=str(tmp_path / "freerouting-2.5.0.jar"), passes=1, timeout=60, planes=(),
                    log=lambda m: None)
    assert res["status"] == "kept", res
    assert res["opens_before"] == 2 and res["opens_after"] == 0 and res["tries"] == 1
    assert set(res["ripped_nets"]) == {"A", "B"}
    assert not RC.text_open(RD.load(out))                    # A and B both connected in the output
    assert _tracks(out, "B") == [("F.Cu", "B", (18.0, 20.635), (58.0, 40.635), 0.2)]


def test_locked_blocker_fails_and_is_named_in_sealing(tmp_path, fake, monkeypatch, drc):
    loc, set_cfg = fake
    _java(tmp_path, monkeypatch)
    _import_fake_ses(monkeypatch)
    base = _pocket(tmp_path, locked=True)                    # B's copper is all locked: nothing of B may move
    out = str(tmp_path / "out.kicad_pcb")
    set_cfg(ses=ses_text([]))                                # the router cannot get A through the wall
    res = RR.repair(base, out, tries=2, jar=str(tmp_path / "freerouting-2.5.0.jar"), passes=1, timeout=60,
                    planes=(), log=lambda m: None)
    assert res["status"] == "failed" and res["tries"] == 2
    assert not (tmp_path / "out.kicad_pcb").exists()         # out_path is never written on failure
    assert any(s.startswith("track B F.Cu locked") for s in res["sealing"]), res["sealing"]
    assert "B" not in res["ripped_nets"]
    log = (tmp_path / "out.repair-region.log").read_text().splitlines()
    assert len(log) == 2 and all(": " in l for l in log)


def test_rf_and_pair_targets_are_skipped_never_ripped(tmp_path, monkeypatch, drc):
    base = write(tmp_path, G.ic("U1", 20, 20, ["RF_GNSS", "USB_DP", "C1", "D1"], pitch=1.27) +
                 G.ic("U2", 60, 40, ["RF_GNSS", "USB_DP", "C2", "E1"], pitch=1.27), inner=False)
    out = str(tmp_path / "out.kicad_pcb")

    def no_route(*a, **k):
        raise AssertionError("region.run must not run for rf/pair targets")

    monkeypatch.setattr(RG, "run", no_route)
    res = RR.repair(base, out, constraints={"pairs": {"USB": {}}}, log=lambda m: None)
    assert res == {"status": "nothing", "skipped": {"RF_GNSS": "rf", "USB_DP": "pair"}}
    assert not (tmp_path / "out.kicad_pcb").exists()


def test_margin_grows_and_second_try_succeeds(tmp_path, monkeypatch, drc):
    base = _pocket(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    areas = []

    def fake_run(board, tmp, scope, **kw):
        areas.append(scope["area"])
        text = open(board).read()
        if len(areas) >= 2:                                  # the larger area lets the router join A
            text = text[:text.rindex(")")] + seg((18, 19.365), (58, 39.365), "A") + ")\n"
        open(tmp, "w").write(text)
        return {"ok": True, "violations": []}

    monkeypatch.setattr(RG, "run", fake_run)
    res = RR.repair(base, out, margin=2.0, tries=3, log=lambda m: None)
    assert res["status"] == "kept" and res["tries"] == 2
    assert areas[1][0] < areas[0][0] and areas[1][2] > areas[0][2]   # the box really grew
    assert "A" not in RC.text_open(RD.load(out))


def test_region_run_keep_locked_leaves_locked_track(tmp_path, fake, monkeypatch):
    loc, set_cfg = fake
    _java(tmp_path, monkeypatch)
    _import_fake_ses(monkeypatch)
    body = (G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) + seg((12, 12), (16, 12), "B", locked=True))
    base = write(tmp_path, body, inner=False)
    scope = {"nets": {"A", "B"}, "area": (10, 10, 30, 30)}
    set_cfg(ses=ses_text([]))
    jar = str(tmp_path / "freerouting-2.5.0.jar")
    out = str(tmp_path / "keep.kicad_pcb")
    res = RG.run(base, out, scope, jar=jar, passes=1, timeout=60, planes=(), rip=True, keep_locked=True)
    assert res["ok"], res["violations"]
    assert _tracks(out, "B") == _tracks(base, "B") and _tracks(out, "B")
    assert [s["locked"] for s in RD.load(out)["segs"] if s["net"] == "B"] == [True]
    # the default still rips it (contrast: the locked copper is only kept when asked)
    out2 = str(tmp_path / "rip.kicad_pcb")
    res2 = RG.run(base, out2, scope, jar=jar, passes=1, timeout=60, planes=(), rip=True)
    assert res2["ok"] and _tracks(out2, "B") == []
