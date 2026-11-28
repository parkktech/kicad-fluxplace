"""2026-11-27 W16 route-11: one new clearance error from fan-out copper failed the whole fanout stage (FANOUT_DRC).
The fan-out pieces in a NEW violation are dropped (their pins stay open for the router); copper the input had stays."""
import os
import shutil
import sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planboards as PB
from fluxplace import fanout as FO, drcguard as DG

pytestmark = pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")
KEEP = lambda v: v.get("severity") == "error"


def _seg(net, a, b):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) (layer "F.Cu") (net "{net}") (uuid "{"%08x" % abs(hash((net, a, b)) % 2**32)}-0000-4000-8000-000000000000"))\n'


def test_new_fanout_copper_in_a_new_violation_is_dropped_and_old_copper_stays(tmp_path):
    body = PB.fp("U1", 0, 0, [PB.pad("1", 0, 0, "A"), PB.pad("2", 0, 2, "B")])
    old = _seg("A", (0, 0), (-3, 0))
    src = PB.write(tmp_path, body + old, name="in.kicad_pcb")
    bad = _seg("B", (0, 2), (0.1, 0.3))                  # fan-out stub of B running into pad A
    out = PB.write(tmp_path, body + old + bad, name="out.kicad_pcb")
    d0, d1 = DG.drc_json(src), DG.drc_json(out)
    assert FO.drop_new_drc_copper(src, out, d0, d1, KEEP) == 1
    text = open(out).read()                               # read the file: a second LoadBoard in one process is unreliable
    assert text.count("(segment") == 1 and '(net "B")' not in text.split("(segment", 1)[1]
