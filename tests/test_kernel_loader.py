"""K5 loader differential: kernel items and zones against the legacy model (hrgeom.Model, pcbnew, no refill).

pcbgen boards always run; the real boards run when present (paths are test fixtures, not code).
"""
import bisect
import os
from collections import Counter, defaultdict

import pytest

import pcbgen as G
from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

REAL = [os.environ.get("FPK_W14_R5", "/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb"),
        "/usr/share/kicad/demos/cm5_minima/CM5_MINIMA_3.kicad_pcb"]

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


# pcbnew (and the legacy model) need an even copper-layer count; pcbgen's 3-layer header only suits the kernel tests
HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
         '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts") (uuid "12121212-1212-4121-8121-121212121212"))\n')


def _board(tmp_path, body, name="b.kicad_pcb"):
    p = tmp_path / name
    p.write_text(HEAD4 + body + ")\n")
    return str(p)


def _kernel_fill_stale(path):
    with K.Kernel(path) as k:
        return k.call("info")["fill_stale"]


def test_fill_stale_flags_a_foreign_track_on_a_fill(tmp_path):
    assert _kernel_fill_stale(_board(tmp_path, ZONE)) is False
    assert _kernel_fill_stale(_board(tmp_path, ZONE + TRACK, name="s.kicad_pcb")) is True
    same_net = TRACK.replace('"SIG"', '"GND"')
    assert _kernel_fill_stale(_board(tmp_path, ZONE + same_net, name="g.kicad_pcb")) is False


def test_keepout_zone_becomes_keepout_items_with_flags(tmp_path):
    path = _board(tmp_path, KEEPOUT)
    with K.Kernel(path) as k:
        items = k.call("items", kinds=["keepout"])["items"]
        zones = k.call("zones")["zones"]
    assert len(items) == 1 and items[0]["extra"]["keepout"]["tracks"] == "not_allowed"
    assert items[0]["layers"] == ["F.Cu"] and zones == []


def _legacy(path):
    from fluxplace import hrgeom
    return hrgeom.Model(path, refill=False)


def _poly_area(pts):
    n = len(pts)
    return abs(sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))) / 2


def _unmatched(kern, legacy, tol):
    """Legacy boxes with no unused kernel box within tol on every side (greedy, sorted by x0)."""
    ks = sorted(kern)
    xs = [b[0] for b in ks]
    used = [False] * len(ks)
    bad = []
    for c in legacy:
        lo, hi = bisect.bisect_left(xs, c[0] - tol), bisect.bisect_right(xs, c[0] + tol)
        for i in range(lo, hi):
            if not used[i] and max(abs(a - b) for a, b in zip(ks[i], c)) <= tol:
                used[i] = True
                break
        else:
            bad.append(c)
    return bad


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _compare(path, pad_tol=0.01, tol=1e-6, arc_tol=0.1):
    """-> list of differences (empty when the kernel loader agrees with the legacy model)."""
    m = _legacy(path)
    with K.Kernel(path) as k:
        items = [it for it in k.call("items")["items"] if it["kind"] != "graphic"]
        zones = k.call("zones")["zones"]
    kern = defaultdict(list)
    kern_by_uuid = defaultdict(list)                  # arcs, edges, keepouts: one kernel item may be many legacy pieces
    for it in items:
        kind = {"keepout": "ko"}.get(it["kind"], it["kind"])
        if kind in ("arc", "edge", "ko"):
            kern_by_uuid[(kind, it["uuid"])].append(tuple(it["bbox"]))
        else:
            kern[kind].append(tuple(it["bbox"]))
    legacy = defaultdict(list)
    leg_by_uuid = defaultdict(list)
    for o in m.obs:
        if o.kind == "track" and o.extra.get("arc"):
            leg_by_uuid[("arc", o.id)].append(tuple(o.bb))
        elif o.kind in ("edge", "ko"):
            leg_by_uuid[(o.kind, o.id)].append(tuple(o.bb))
        else:
            legacy[o.kind].append(tuple(o.bb))
    problems = []
    for kind in ("pad", "hole", "via", "track"):
        kc, lc = len(kern.get(kind, [])), len(legacy.get(kind, []))
        if kc != lc:
            problems.append(f"{kind}: kernel {kc} vs legacy {lc}")
            continue
        bad = _unmatched(kern.get(kind, []), legacy.get(kind, []), pad_tol if kind == "pad" else tol)
        if bad:
            problems.append(f"{kind}: {len(bad)} legacy boxes without a kernel box within tolerance, e.g. {bad[:2]}")
    for key in sorted(set(kern_by_uuid) | set(leg_by_uuid)):
        ka, la = kern_by_uuid.get(key), leg_by_uuid.get(key)
        if not ka or not la:
            problems.append(f"{key[0]} {key[1]}: kernel {bool(ka)} legacy {bool(la)}")
            continue
        ku, lu = _union(ka), _union(la)
        if max(abs(x - y) for x, y in zip(ku, lu)) > arc_tol:
            problems.append(f"{key[0]} {key[1]}: bbox {ku} vs {lu}")
    if not any(k[0] in ("arc", "edge", "ko") for k in list(kern_by_uuid) + list(leg_by_uuid)):
        pass
    lz = {z["id"]: z for z in m.zones}
    for z in zones:
        leg = lz.get(z["uuid"])
        if leg is None:
            problems.append(f"zone {z['uuid']} missing in legacy")
            continue
        for f in z["fills"]:
            lfs = leg["fills"].get(f["layer"], [])
            area = sum(_poly_area(p["outline"]) - sum(_poly_area(h) for h in p["holes"]) for p in lfs)
            if abs(f["area_mm2"] - area) > 1e-3 * max(area, 1e-9):
                problems.append(f"zone {z['uuid']} {f['layer']}: area {f['area_mm2']} vs {area}")
    return problems


def test_loader_matches_legacy_on_pcbgen_board(tmp_path):
    body = G.fp("R1", 20, 20, [G.pad("1", -0.5, 0, "A", w=0.6, h=0.6), G.pad("2", 0.5, 0, "B", w=0.6, h=0.6)],
                angle=90) + ZONE + TRACK + KEEPOUT
    body += ('(via (at 30 30) (size 0.6) (drill 0.3) (net "SIG") (uuid "88888888-8888-4888-8888-888888888888"))\n'
             '(arc (start 40 30) (mid 42 32) (end 44 30) (width 0.2) (layer "B.Cu") (net "SIG") '
             '(uuid "99999999-9999-4999-8999-999999999999"))\n')
    path = _board(tmp_path, body)
    assert _compare(path) == []


@pytest.mark.parametrize("path", REAL)
def test_loader_matches_legacy_on_real_board(path):
    if not os.path.isfile(path):
        pytest.skip("board not present")
    assert _compare(path) == []


STICKHUB = "/usr/share/kicad/demos/stickhub/StickHub.kicad_pcb"


@pytest.mark.parametrize("path,limit_ms,stale", [
    (REAL[0], 2000, True), (REAL[1], 1000, False), (STICKHUB, 1000, False)])
def test_load_time_and_fill_stale_on_real_boards(path, limit_ms, stale):
    if not os.path.isfile(path):
        pytest.skip("board not present")
    with K.Kernel(path) as k:
        info = k.call("info")
    assert info["load_ms"] < limit_ms, info["load_ms"]
    assert info["fill_stale"] is stale
