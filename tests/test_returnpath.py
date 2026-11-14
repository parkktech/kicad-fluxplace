"""RETURN PATH: REF_PLANE_GAP, RETURN_VIA_MISSING and EDGE_TRACE on boards with real saved zone fills."""
import os, sys
import pytest
pcbnew = pytest.importorskip("pcbnew")   # zone fills are made by pcbnew, then the file is read back
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import returnpath as RP, review as R

HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n')
# outline as four stroked lines: a stroke-less gr_rect outline stops pcbnew filling any zone
EDGE = "".join(f'(gr_line (start {a} {b}) (end {c} {d}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))\n'
               for a, b, c, d in ((0, 0, 100, 0), (100, 0, 100, 80), (100, 80, 0, 80), (0, 80, 0, 0)))
CONS = {"review": {"return_path_nets": ["^SIG$"]}}


def zone(net, layer, x0=0, y0=0, x1=100, y1=80):
    return (f'(zone (net "{net}") (net_name "{net}") (layer "{layer}") (hatch edge 0.5) '
            f'(connect_pads (clearance 0.2)) (min_thickness 0.2) (fill yes) '
            f'(polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))\n')


def trk(net, layer, x0, y0, x1, y1, w=0.2):
    return (f'(segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") (net "{net}"))\n')


def via(net, x, y, dia=0.6, drill=0.3):
    return f'(via (at {x} {y}) (size {dia}) (drill {drill}) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def filled_board(tmp_path, body, name="b.kicad_pcb"):
    """Text board -> pcbnew: zones filled, fills saved into the file."""
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HEAD4 + EDGE + body + ")\n")
    b = pcbnew.LoadBoard(str(p))
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(str(p), b)
    return str(p)


def _run(path, cons=CONS):
    facts = {"board_path": path, "net_pads": {"SIG": [], "GND": []}}
    return RP.check_return_path(facts, cons)


def _codes(findings, code):
    return [f for f in findings if f["code"] == code]


# ---- (a) and (b): a slot in the reference plane under a controlled track ----
def test_slot_under_track_is_ref_plane_gap(tmp_path):
    # GND plane on In1; a foreign track X runs under the SIG track for 20 mm and cuts the fill
    body = (zone("GND", "In1.Cu") + trk("X", "In1.Cu", 30, 40, 50, 40)
            + trk("SIG", "F.Cu", 10, 40, 90, 40))
    out = _run(filled_board(tmp_path, body))
    gaps = _codes(out, "REF_PLANE_GAP")
    assert len(gaps) == 1 and gaps[0]["level"] == "WARN"      # default level is WARN
    assert "SIG on F.Cu" in gaps[0]["msg"] and "from x=" in gaps[0]["msg"]
    # the run starts at the slot's left edge (x ~ 30 - clearance - half width ~ 29.4), 20+ mm long
    start_x = float(gaps[0]["msg"].split("x=")[1].split()[0])
    assert 28.5 < start_x < 31.0


def test_gap_is_fail_only_when_opted_in(tmp_path):
    body = (zone("GND", "In1.Cu") + trk("X", "In1.Cu", 30, 40, 50, 40)
            + trk("SIG", "F.Cu", 10, 40, 90, 40))
    cons = {"review": {"return_path_nets": ["^SIG$"], "return_path": "fail"}}
    gaps = _codes(_run(filled_board(tmp_path, body), cons), "REF_PLANE_GAP")
    assert len(gaps) == 1 and gaps[0]["level"] == "FAIL"


def test_gap_at_same_net_via_endpoint_is_exempt(tmp_path):
    # foreign track X cuts the plane from x~9 to ~11.5 under a SIG track that starts at x=10.
    # Unanchored, the unreferenced run is 10..11.5 = 1.5 mm (> 1 mm): reported.
    # Anchored by a SIG via at the start, the first 1 mm is the antipad and is exempt: the rest is 0.5 mm.
    body = (zone("GND", "In1.Cu") + trk("X", "In1.Cu", 9.5, 40, 11.2, 40)
            + trk("SIG", "F.Cu", 10, 40, 90, 40))
    assert len(_codes(_run(filled_board(tmp_path / "bare", body)), "REF_PLANE_GAP")) == 1
    anchored = body + via("SIG", 10, 40)
    assert _codes(_run(filled_board(tmp_path / "via", anchored)), "REF_PLANE_GAP") == []


def test_unslotted_plane_has_no_gap(tmp_path):
    body = zone("GND", "In1.Cu") + trk("SIG", "F.Cu", 10, 40, 90, 40)
    out = _run(filled_board(tmp_path, body))
    assert _codes(out, "REF_PLANE_GAP") == []


def test_gap_is_checked_only_for_controlled_nets(tmp_path):
    body = (zone("GND", "In1.Cu") + trk("X", "In1.Cu", 30, 40, 50, 40)
            + trk("PLAIN", "F.Cu", 10, 40, 90, 40))
    out = _run(filled_board(tmp_path, body))
    assert _codes(out, "REF_PLANE_GAP") == []


# ---- (c): a layer-changing via with and without a return via ----
def _via_board(tmp_path, ret_via, in2_net="GND", name="b.kicad_pcb"):
    body = (zone("GND", "In1.Cu") + zone(in2_net, "In2.Cu")
            + trk("SIG", "F.Cu", 10, 40, 50, 40) + trk("SIG", "B.Cu", 50, 40, 90, 40)
            + via("SIG", 50, 40)
            + (via("GND", 50, 41.5) if ret_via else ""))
    return filled_board(tmp_path, body, name=name)


def test_layer_change_without_return_via_warns(tmp_path):
    out = _run(_via_board(tmp_path / "n", ret_via=False))
    m = _codes(out, "RETURN_VIA_MISSING")
    assert len(m) == 1 and m[0]["level"] == "WARN"          # one finding per transition via
    assert "no return via" in m[0]["msg"] and "x=50.00 y=40.00" in m[0]["msg"]
    assert "In1.Cu" in m[0]["msg"] and "In2.Cu" in m[0]["msg"] and "none on that net" in m[0]["msg"]


def test_layer_change_with_return_via_is_clean(tmp_path):
    out = _run(_via_board(tmp_path / "y", ret_via=True))
    assert _codes(out, "RETURN_VIA_MISSING") == []


def test_different_reference_nets_are_named(tmp_path):
    out = _run(_via_board(tmp_path / "d", ret_via=True, in2_net="3V3"))
    m = _codes(out, "RETURN_VIA_MISSING")
    assert len(m) == 1 and "different nets" in m[0]["msg"] and "3V3" in m[0]["msg"]


# ---- (d): a controlled track too close to the outline ----
def test_edge_trace_warns_close_to_outline(tmp_path):
    body = zone("GND", "In1.Cu") + trk("SIG", "F.Cu", 10, 0.3, 90, 0.3)
    out = _run(filled_board(tmp_path, body))
    e = _codes(out, "EDGE_TRACE")
    assert len(e) == 1 and e[0]["level"] == "WARN" and "0.30 mm from the board outline" in e[0]["msg"]


def test_track_away_from_outline_no_edge_trace(tmp_path):
    body = zone("GND", "In1.Cu") + trk("SIG", "F.Cu", 10, 40, 90, 40)
    assert _codes(_run(filled_board(tmp_path, body)), "EDGE_TRACE") == []


def test_no_tracks_no_findings(tmp_path):
    assert _run(filled_board(tmp_path, zone("GND", "In1.Cu"))) == []
