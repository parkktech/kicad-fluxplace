"""fix-c #5/#6: the protection fingerprint sees arc midpoints, via layer spans, keepouts and pads;
removable items come only from the explicit --rip selection."""
from collections import Counter

from fluxplace import region as RG

BOARD = """(kicad_pcb (version 20240108) (generator "t")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "A")
 {arc}
 (segment (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))
 (via (at 2 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
 (zone (net 0) (net_name "") (layers "F.Cu") (hatch edge 0.5)
  (connect_pads (clearance 0.2))
  (keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
  (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
  (polygon (pts (xy {kx0} 0) (xy 5 0) (xy 5 5))))
 (footprint "R:R0603" (layer "F.Cu") (at {fx} 3)
  (property "Reference" "R1" (at 0 0) (layer "F.Fab"))
  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "A")))
)
"""
ARC = "(arc (start 0 0) (mid {mx} 0.5) (end 1 0) (width 0.2) (layer \"F.Cu\") (net 1))"


def _write(tmp_path, name, arc_mid=0.5, kx0=0, fx=3, arc=True):
    p = tmp_path / name
    p.write_text(BOARD.format(arc=ARC.format(mx=arc_mid) if arc else "", kx0=kx0, fx=fx))
    return str(p)


def _keys(path):
    items, _ = RG.fingerprint(path)
    return Counter(i["key"] for i in items), items


def test_arc_midpoint_is_fingerprinted(tmp_path):
    a = _write(tmp_path, "a.kicad_pcb", arc_mid=0.5)
    b = _write(tmp_path, "b.kicad_pcb", arc_mid=-0.5)
    ka, ia = _keys(a)
    kb, _ = _keys(b)
    assert ka != kb
    res = RG.check(a, b, {"nets": None, "area": (-99, -99, 99, 99)})
    assert not res["ok"]


def test_via_layer_span_is_fingerprinted(tmp_path):
    a = _write(tmp_path, "a.kicad_pcb")
    b = a + ".v.kicad_pcb"
    open(b, "w").write(open(a).read().replace('(layers "F.Cu" "B.Cu")', '(layers "F.Cu" "In1.Cu")'))
    ka, _ = _keys(a)
    kb, _ = _keys(b)
    assert ka != kb


def test_moved_keepout_is_fingerprinted(tmp_path):
    a = _write(tmp_path, "a.kicad_pcb", kx0=0)
    b = _write(tmp_path, "b.kicad_pcb", kx0=1)
    ka, _ = _keys(a)
    kb, _ = _keys(b)
    assert ka != kb


def test_moved_footprint_is_fingerprinted(tmp_path):
    a = _write(tmp_path, "a.kicad_pcb", fx=3)
    b = _write(tmp_path, "b.kicad_pcb", fx=4)
    ka, ia = _keys(a)
    kb, _ = _keys(b)
    assert ka != kb
    assert any(i["kind"] == "pad" for i in ia)


def test_check_flags_pcbnew_drop_not_in_rip_selection(tmp_path):
    # fix-c #6: an item that disappears without being in the --rip selection is a protection failure
    a = _write(tmp_path, "a.kicad_pcb")
    b = _write(tmp_path, "b.kicad_pcb", arc=False)
    scope = {"nets": ["A"], "area": (-99, -99, 99, 99)}
    res = RG.check(a, b, scope, ripped=Counter())
    assert not res["ok"] and any("protected copper changed" in v for v in res["violations"])
    items, _ = RG.fingerprint(a)
    arc_key = next(i["key"] for i in items if i["kind"] == "arc")
    res2 = RG.check(a, b, scope, ripped=Counter([arc_key]))
    assert res2["ok"], res2["violations"]
