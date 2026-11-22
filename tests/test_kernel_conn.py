"""K11: connectivity (islands and opens) against the legacy model, and opens after edits."""
import os
from collections import defaultdict

import pytest

from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
         '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts") (uuid "12121212-1212-4121-8121-121212121212"))\n')
ZONE = ('(zone (net "GND") (net_name "GND") (layers "F&B.Cu") (uuid "55555555-5555-4555-8555-555555555555") '
        '(name "z") (priority 1) (connect_pads (clearance 0.2)) (min_thickness 0.25) '
        '(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
        '(polygon (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40))) '
        '(filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40))))\n')


def _uuid(tag):
    return f"00000000-0000-4000-8000-{abs(hash(tag)) % 10**12:012d}"


def _fp(ref, x, y, net):
    return (f'(footprint "R" (layer "F.Cu") (uuid "{_uuid("f" + ref)}") (at {x} {y} 0) (property "Reference" "{ref}") '
            f'(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "{net}") (uuid "{_uuid("p" + ref)}")))\n')


def _opens_total(k):
    nets = {i["net_name"] for i in k.call("items")["items"]} - {""}
    return sum(k.call("islands", net=n)["opens"] for n in nets)


def test_two_pads_one_open_then_a_closing_track_closes_it(tmp_path):
    p = tmp_path / "o.kicad_pcb"
    p.write_text(HEAD4 + _fp("R1", 10, 10, "N") + _fp("R2", 50, 10, "N") + ")\n")
    with K.Kernel(str(p)) as k:
        assert k.call("islands", net="N")["opens"] == 1
        k.call("begin")
        ids = k.call("add", items=[{"kind": "seg", "net": "N", "layer": "F.Cu", "width": 0.2,
                                    "a": [10, 10], "b": [50, 10], "uuid": "bridge"}])["ids"]
        assert k.call("islands", net="N")["opens"] == 0
        assert k.call("rollback")["depth"] == 0
        assert k.call("islands", net="N")["opens"] == 1
        k.call("begin")
        k.call("add", items=[{"kind": "seg", "net": "N", "layer": "F.Cu", "width": 0.2,
                              "a": [10, 10], "b": [50, 10], "uuid": "bridge2"}])
        k.call("commit")
        assert k.call("islands", net="N")["opens"] == 0
        k.call("remove", uuids=["bridge2"])
        assert k.call("islands", net="N")["opens"] == 1
        assert ids


def test_check_reports_opens_and_refuses_a_rise(tmp_path):
    p = tmp_path / "c.kicad_pcb"
    p.write_text(HEAD4 + _fp("R1", 10, 10, "N") + _fp("R2", 50, 10, "N") + ")\n")
    with K.Kernel(str(p)) as k:
        k.call("begin")
        ids = k.call("add", items=[{"kind": "seg", "net": "N", "layer": "F.Cu", "width": 0.2,
                                    "a": [10, 10], "b": [50, 10], "uuid": "bridge"}])["ids"]
        k.call("commit")
        k.call("begin")
        k.call("remove", ids=[ids[0]])
        r = k.call("check")
        assert r["opens"] == {"N": [0, 1]} and r["verdict"] == "refuse"
        assert k.call("check", allow_opens=True)["verdict"] == "accept"
        k.call("rollback")


@pytest.mark.parametrize("name", ["pcbgen", "r5"])
def test_islands_equal_legacy(tmp_path, name):
    from fluxplace import hrgeom
    if name == "pcbgen":
        p = tmp_path / "z.kicad_pcb"
        p.write_text(HEAD4 + ZONE + _fp("R1", 10, 10, "N") + _fp("R2", 30, 10, "N") + _fp("R3", 10, 30, "GND")
                     + '(segment (start 10 10) (end 30 10) (width 0.2) (layer "F.Cu") (net "N") (uuid "00000000-0000-4000-8000-0000000000aa"))\n)\n')
        path = str(p)
    else:
        path = "/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb"
        if not os.path.isfile(path):
            pytest.skip("W14 r5 not present")
    m = hrgeom.Model(path, refill=False)
    with K.Kernel(path) as k:
        items = {i["id"]: i["uuid"] for i in k.call("items")["items"]}
        nets = sorted({i["net_name"] for i in k.call("items")["items"]} - {""})
        total_k = 0
        for net in nets:
            r = k.call("islands", net=net)
            total_k += r["opens"]
            ours = sorted(sorted(items[i] for i in c["items"]) for c in r["islands"] if c["items"])
            isl, _ = m.islands(net)
            theirs = sorted(sorted(o.id for o in g["items"]) for g in isl if g["items"])
            assert ours == theirs, net
        legacy_open = sum(max(0, sum(1 for g in m.islands(n)[0] if any(o.kind == "pad" for o in g["items"])) - 1)
                          for n in nets)
    assert total_k == legacy_open
    if name == "r5":
        # kicad-cli reports 9 unconnected items here; the extra one is a GND zone island with no pad, which the
        # pad-holding rule does not count. K13 calibrates pad-less clusters (ROUTER-C1 section 11, K13).
        assert total_k == 8


def test_largest_net_rebuilds_fast_on_r5():
    path = "/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb"
    if not os.path.isfile(path):
        pytest.skip("W14 r5 not present")
    with K.Kernel(path) as k:
        sizes = defaultdict(int)
        for it in k.call("items")["items"]:
            if it["kind"] in ("pad", "track", "arc", "via"):
                sizes[it["net_name"]] += 1
        big = max(sizes, key=sizes.get)
        ms = min(k.call("islands", net=big)["ms"] for _ in range(3))
    assert ms < 20, ms
