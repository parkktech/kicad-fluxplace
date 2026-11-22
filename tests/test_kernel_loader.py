"""K5 / K5b loader differential: kernel items and zones against the legacy model (hrgeom.Model, pcbnew, no refill).

The fixture board is written as text and then saved by pcbnew (authentic file); its footprint is also rotated to 37
degrees and flipped to the back. Real boards run when present (paths are test fixtures, not code).
"""
import copy
import math
import os
from collections import defaultdict
from pathlib import Path

import pytest

from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

REAL = [os.environ.get("FPK_W14_R5", "/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb"),
        "/usr/share/kicad/demos/cm5_minima/CM5_MINIMA_3.kicad_pcb"]
STICKHUB = "/usr/share/kicad/demos/stickhub/StickHub.kicad_pcb"
PAD_TOL = 0.0051          # mm: pcbnew approximates roundrect corners (max 4.72 um measured on r5)
BOX_TOL = 1.5e-6          # mm: holes, vias, tracks
KO_TOL = 1e-5             # mm: keepouts
ARC_TOL = 0.1             # mm: arc and edge union boxes (legacy arcs are fixed-step polylines)

# pcbnew needs an even copper-layer count (pcbgen's 3-layer header is for the kernel tests only)
HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
         '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts") (uuid "12121212-1212-4121-8121-121212121212"))\n')
ZONE = ('(zone (net "GND") (net_name "GND") (layers "F&B.Cu") (uuid "55555555-5555-4555-8555-555555555555") '
        '(name "z") (priority 1) (connect_pads (clearance 0.2)) (min_thickness 0.25) '
        '(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
        '(polygon (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40))) '
        '(filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40))))\n')
KEEPOUT = ('(zone (net 0) (net_name "") (layers "F.Cu") (uuid "66666666-6666-4666-8666-666666666666") '
           '(name "ko") (keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour not_allowed) '
           '(footprints allowed)) (polygon (pts (xy 60 0) (xy 70 0) (xy 70 10) (xy 60 10))))\n')
TRACK = ('(segment (start 10 10) (end 40 10) (width 0.2) (layer "F.Cu") (net "SIG") '
         '(uuid "77777777-7777-4777-8777-777777777777"))\n')
VIA_ARC = ('(via (at 30 30) (size 0.6) (drill 0.3) (net "SIG") (uuid "88888888-8888-4888-8888-888888888888"))\n'
           '(arc (start 40 30) (mid 42 32) (end 44 30) (width 0.2) (layer "B.Cu") (net "SIG") '
           '(uuid "99999999-9999-4999-8999-999999999999"))\n')
# the fixture footprint: one pad of every K4 shape class, drill offset, custom primitive
PADS = [
    '(pad "1" smd roundrect (at -2 0) (size 1.2 0.6) (roundrect_rratio 0.25) (layers "F.Cu") (net "A"))',
    '(pad "2" smd oval (at 0 0 30) (size 1.4 0.6) (layers "F.Cu") (net "B"))',
    '(pad "3" smd rect (at 2 0) (size 1 0.5) (layers "F.Cu") (net "C"))',
    '(pad "4" smd trapezoid (at 4 0) (size 1 0.8) (rect_delta 0 0.3) (layers "F.Cu") (net "D"))',
    '(pad "5" thru_hole rect (at 6 0) (size 2 1) (drill 0.4 (offset 0.5 0)) (layers "*.Cu") (net "E"))',
    '(pad "6" thru_hole oval (at 9 0) (size 1.2 2) (drill oval 0.5 1.4) (layers "*.Cu") (net "F"))',
    '(pad "7" smd custom (at 12 0) (size 0.4 0.4) (options (clearance outline) (anchor rect)) '
    '(primitives (gr_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (width 0.1) (fill yes))) (layers "F.Cu") (net "G"))',
]
FOOTPRINT = ('(footprint "U" (layer "F.Cu") (uuid "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") (at 20 20 0) '
             '(property "Reference" "U1") ' + " ".join(PADS) + ')\n')


def _legacy(path):
    from fluxplace import hrgeom
    return hrgeom.Model(path, refill=False)


def _poly_area(pts):
    n = len(pts)
    return abs(sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))) / 2


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    if L == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _inside(p, pts):
    inside, n = False, len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            inside = not inside
    return inside


def _sd_shape(p, s):
    """Signed distance from p to one kernel shape (negative inside a polygon core), minus its radius."""
    c, r = s["core"], s["r"]
    if c["t"] == "point":
        d = math.hypot(p[0] - c["p"][0], p[1] - c["p"][1])
    elif c["t"] == "seg":
        d = _seg_dist(p, c["a"], c["b"])
    else:
        pts = c["pts"]
        d = min(_seg_dist(p, pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))
        if _inside(p, pts):
            d = -d
    return d - r


def _pad_dev(ob, it, trapezoid):
    """Worst signed deviation of the legacy outline vertices from the kernel pad (mm)."""
    verts = ob.g[1]
    sd = [min(_sd_shape(p, s) for s in it["shapes"]) for p in verts]
    return max(sd) if trapezoid else max(abs(v) for v in sd)


def _diff(items, obs, zones_k, zones_l):
    """-> (problems, worst pad deviation in mm). Strict by (kind, uuid); see ROUTER-C1 section 13 K5b-3."""
    problems, worst = [], 0.0
    groups = defaultdict(list)
    for it in items:
        if it["kind"] != "graphic":
            groups[({"keepout": "keepout"}.get(it["kind"], it["kind"]), it["uuid"])].append(it)
    used = set()
    legacy_union = defaultdict(list)
    for o in obs:
        kind = "arc" if (o.kind == "track" and o.extra.get("arc")) else ("keepout" if o.kind == "ko" else o.kind)
        if kind in ("arc", "edge"):
            legacy_union[(kind, o.id)].append(tuple(o.bb))
            continue
        cands = [i for i, it in enumerate(groups.get((kind, o.id), [])) if (kind, o.id, i) not in used]
        if not cands:
            problems.append(f"{kind} {o.id} {o.ref or o.desc}: no kernel item")
            continue
        cx_, cy_ = (o.bb[0] + o.bb[2]) / 2, (o.bb[1] + o.bb[3]) / 2
        best = min(cands, key=lambda i: (
            (groups[(kind, o.id)][i]["bbox"][0] + groups[(kind, o.id)][i]["bbox"][2]) / 2 - cx_) ** 2
            + ((groups[(kind, o.id)][i]["bbox"][1] + groups[(kind, o.id)][i]["bbox"][3]) / 2 - cy_) ** 2)
        used.add((kind, o.id, best))
        it = groups[(kind, o.id)][best]
        tag = f"{kind} {o.id} {o.ref or ''}"
        if kind in ("pad", "track", "arc", "via") and it["net_name"] != (o.net or ""):
            problems.append(f"{tag}: net {it['net_name']!r} vs {o.net!r}")
        if kind in ("pad", "track", "arc") and set(it["layers"]) != set(o.layers):
            problems.append(f"{tag}: layers {it['layers']} vs {sorted(o.layers)}")
        if kind == "pad":
            if it["ref"] != o.ref:
                problems.append(f"{tag}: ref {it['ref']!r} vs {o.ref!r}")
            trap = it["extra"].get("shape") == "trapezoid"
            dev = _pad_dev(o, it, trap)
            worst = max(worst, dev)
            if dev > PAD_TOL:
                problems.append(f"{tag}: outline deviation {dev:.4f} mm > {PAD_TOL}")
        if kind in ("hole", "via", "track"):
            if max(abs(a - b) for a, b in zip(it["bbox"], o.bb)) > BOX_TOL:
                problems.append(f"{tag}: bbox {it['bbox']} vs {o.bb}")
        if kind == "keepout":
            if max(abs(a - b) for a, b in zip(it["bbox"], o.bb)) > KO_TOL:
                problems.append(f"{tag}: bbox {it['bbox']} vs {o.bb}")
            if set(it["layers"]) != set(o.layers):
                problems.append(f"{tag}: layers differ")
            for f in ("no_tracks", "no_vias", "no_pads"):
                if bool(it["extra"].get(f)) != bool(o.extra.get(f)):
                    problems.append(f"{tag}: {f} differs")
    kernel_union = defaultdict(list)
    for (kind, uuid), its in groups.items():
        if kind in ("arc", "edge"):
            kernel_union[(kind, uuid)] = [tuple(i["bbox"]) for i in its]
    for key in set(kernel_union) | set(legacy_union):
        ka, la = kernel_union.get(key), legacy_union.get(key)
        if not ka or not la:
            problems.append(f"{key[0]} {key[1]}: kernel {bool(ka)} legacy {bool(la)}")
        elif max(abs(a - b) for a, b in zip(_union(ka), _union(la))) > ARC_TOL:
            problems.append(f"{key[0]} {key[1]}: union {_union(ka)} vs {_union(la)}")
    for (kind, uuid), its in groups.items():
        if kind in ("pad", "hole", "via", "track", "keepout"):
            for i in range(len(its)):
                if (kind, uuid, i) not in used:
                    problems.append(f"{kind} {uuid}: kernel item without a legacy partner")
    zl = {z["id"]: z for z in zones_l}
    for z in zones_k:
        leg = zl.get(z["uuid"])
        if leg is None:
            problems.append(f"zone {z['uuid']} missing in legacy")
            continue
        for f in z["fills"]:
            area = sum(_poly_area(p["outline"]) - sum(_poly_area(h) for h in p["holes"])
                       for p in leg["fills"].get(f["layer"], []))
            if abs(f["area_mm2"] - area) > 1e-3 * max(area, 1e-9):
                problems.append(f"zone {z['uuid']} {f['layer']}: area {f['area_mm2']} vs {area}")
    return problems, worst


def _kernel_state(path):
    with K.Kernel(path) as k:
        return k.call("items")["items"], k.call("zones")["zones"], k.call("info")


def _compare(path):
    m = _legacy(path)
    items, zones, _info = _kernel_state(path)
    return _diff(items, m.obs, zones, m.zones)


@pytest.fixture(scope="module")
def fixture_boards(tmp_path_factory):
    import pcbnew
    d = tmp_path_factory.mktemp("loader")
    src = d / "fixture.kicad_pcb"
    src.write_text(HEAD4 + FOOTPRINT + ZONE + TRACK + KEEPOUT + VIA_ARC + ")\n")
    b = pcbnew.LoadBoard(str(src))
    assert b is not None, "pcbnew rejected the fixture"
    base = d / "base.kicad_pcb"
    pcbnew.SaveBoard(str(base), b)
    rot = d / "rot.kicad_pcb"
    fp = b.GetFootprints()[0]
    fp.SetOrientationDegrees(37)
    pcbnew.SaveBoard(str(rot), b)
    b2 = pcbnew.LoadBoard(str(base))
    fp2 = b2.GetFootprints()[0]
    fp2.Flip(fp2.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    flip = d / "flip.kicad_pcb"
    pcbnew.SaveBoard(str(flip), b2)
    return {"base": str(base), "rot": str(rot), "flip": str(flip)}


@pytest.mark.parametrize("which", ["base", "rot", "flip"])
def test_fixture_differential_is_empty(fixture_boards, which):
    assert _compare(fixture_boards[which])[0] == []


def test_comparison_bites(fixture_boards):
    path = fixture_boards["base"]
    m = _legacy(path)
    items, zones, _ = _kernel_state(path)
    assert _diff(items, m.obs, zones, m.zones)[0] == []
    renamed = copy.deepcopy(items)
    pad = next(i for i in renamed if i["kind"] == "pad" and i["net_name"] == "C")
    pad["net_name"] = "ZZ"
    assert any("net" in p for p in _diff(renamed, m.obs, zones, m.zones)[0])
    mirrored = copy.deepcopy(items)
    pad = next(i for i in mirrored if i["kind"] == "pad" and i["extra"]["shape"] == "custom")   # asymmetric
    cx = pad["extra"]["pos"][0]
    for s in pad["shapes"]:
        c = s["core"]
        pts = [c["p"]] if c["t"] == "point" else [c["a"], c["b"]] if c["t"] == "seg" else c["pts"]
        for p in pts:
            p[0] = 2 * cx - p[0]
    problems, worst = _diff(mirrored, m.obs, zones, m.zones)
    assert problems and worst > PAD_TOL


@pytest.mark.parametrize("path", REAL + [STICKHUB])
def test_loader_matches_legacy_on_real_board(path):
    if not os.path.isfile(path):
        pytest.skip("board not present")
    assert _compare(path)[0] == []


def test_load_time_and_fill_stale_on_real_boards():
    for path, limit_ms, stale in [(REAL[0], 2000, True), (REAL[1], 1000, False), (STICKHUB, 1000, False)]:
        if not os.path.isfile(path):
            continue
        with K.Kernel(path) as k:
            info = k.call("info")
        assert info["load_ms"] < limit_ms, info["load_ms"]
        assert info["fill_stale"] is stale


@pytest.mark.parametrize("path,expect_dup", [(REAL[0], False), (REAL[1], True)])
def test_dup_uuids_on_real_boards(path, expect_dup):
    if not os.path.isfile(path):
        pytest.skip("board not present")
    with K.Kernel(path) as k:
        n = k.call("info")["dup_uuids"]
    assert (n > 0) is expect_dup, n


def test_fill_stale_flags_a_foreign_track_on_a_fill(tmp_path):
    def board(body, name):
        p = tmp_path / name
        p.write_text(HEAD4 + body + ")\n")
        return str(p)
    assert K.Kernel(board(ZONE, "a.kicad_pcb")).call("info")["fill_stale"] is False
    assert K.Kernel(board(ZONE + TRACK, "b.kicad_pcb")).call("info")["fill_stale"] is True
    assert K.Kernel(board(ZONE + TRACK.replace('"SIG"', '"GND"'), "c.kicad_pcb")).call("info")["fill_stale"] is False


def test_keepout_zone_becomes_keepout_items_with_flags(tmp_path):
    p = tmp_path / "k.kicad_pcb"
    p.write_text(HEAD4 + KEEPOUT + ")\n")
    with K.Kernel(str(p)) as k:
        items = k.call("items", kinds=["keepout"])["items"]
        zones = k.call("zones")["zones"]
    assert len(items) == 1 and items[0]["extra"]["keepout"]["tracks"] == "not_allowed"
    assert items[0]["extra"]["no_tracks"] is True and items[0]["extra"]["no_vias"] is False
    assert items[0]["layers"] == ["F.Cu"] and zones == []
