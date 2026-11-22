"""K12: path checks against probe_prims (random paths) and the slack raster against _free_fields (same grid)."""
import math
import random

import pytest

import test_kernel_clear as C
from fluxplace import handroute as H
from fluxplace import hrgeom
from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

BODY = ('(segment (start 10 30) (end 40 30) (width 0.2) (layer "F.Cu") (net "B") '
        '(uuid "b1111111-1111-4111-8111-111111111111"))\n'
        '(via (at 25 45) (size 0.6) (drill 0.3) (net "C") (uuid "c1111111-1111-4111-8111-111111111111"))\n'
        '(footprint "R" (layer "F.Cu") (at 60 20 0) (property "Reference" "R1") '
        '(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "A") '
        '(uuid "d1111111-1111-4111-8111-111111111111")))\n')


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    p = tmp_path_factory.mktemp("path") / "p.kicad_pcb"
    p.write_text(C.HEAD4 + BODY + ")\n")
    return str(p)


def _random_paths(seed, n):
    rng = random.Random(seed)
    def rp():
        return [round(rng.uniform(5, 90), 2), round(rng.uniform(5, 70), 2)]
    for _ in range(n):
        prims, a = [], rp()
        for _ in range(rng.randint(1, 3)):
            if rng.random() < 0.3:
                prims.append(["via", a])
                continue
            b = rp()
            if rng.random() < 0.5:
                b = [a[0] + round(rng.choice([-1, 1]) * rng.uniform(0, 20), 2), a[1]]
            prims.append(["seg", "F.Cu", a, b])
            a = b
        yield prims, rng.choice(["A", "B", "C", "N9"])


def test_path_checks_agree_with_probe_prims(board):
    M = hrgeom.Model(board, refill=False)
    agree, explained = 0, 0
    with K.Kernel(board) as k:
        for prims, net in _random_paths(7, 300):
            legacy = [("seg", p[1], tuple(p[2]), tuple(p[3])) if p[0] == "seg" else ("via", tuple(p[1])) for p in prims]
            leg = H.probe_prims(M, net, legacy)
            ker = k.call("check_path", prims=prims, net=net)["probe"]
            lk = sorted({v["kind"] for v in leg["violations"]})
            kk = sorted({v["kind"] for v in ker["violations"]})
            if bool(leg["ok"]) == ker["ok"] and lk == kk:
                agree += 1
                continue
            # the only known difference: a via hole inside the 0.25 mm hole clearance, which probe_prims does not
            # require (it uses 0 for non-NPTH holes); K9 and KiCad do. Anything else is a failure.
            extra = [v for v in ker["violations"] if v["kind"] not in lk]
            assert set(kk) - set(lk) <= {"clearance"}, (prims, net, lk, kk)
            assert all(v.get("item_kind") == "hole" for v in extra), (prims, net, extra)
            explained += 1
    assert agree + explained == 300
    assert explained <= 10, f"{explained} explained differences"


def test_raster_equals_free_fields_on_the_legacy_grid(board):
    M = hrgeom.Model(board, refill=False)
    bbox = (5.0, 5.0, 40.0, 40.0)
    pitch = 0.5
    xs, ys, _p, slack_t, _sv, _sh = H._free_fields(M, "A", ["F.Cu"], bbox, 0.2, pitch)
    with K.Kernel(board) as k:
        nx, ny, vals = k.raster(bbox, pitch, "F.Cu", "A", "track")
    assert (nx, ny) == (len(xs), len(ys))
    leg = slack_t["F.Cu"]
    worst = 0.0
    for j in range(ny):
        for i in range(nx):
            a, b = vals[j * nx + i], float(leg[j][i])
            if abs(b) > 8.9 and abs(a) > 8.9:
                continue
            worst = max(worst, abs(a - b))
    assert worst < 1e-3, worst
