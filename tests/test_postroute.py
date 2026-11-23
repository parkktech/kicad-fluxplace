import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import postroute as PO  # noqa: E402


def test_dedupe_file_drops_stacked_duplicate_vias_and_keeps_one(tmp_path):
    via = '\t(via\n\t\t(at 155.07 110.95)\n\t\t(size 0.46)\n\t\t(drill 0.2)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(locked yes)\n\t\t(net "BT3_VDD")\n\t\t(uuid "%s")\n\t)\n'
    other = via.replace("155.07 110.95", "160 100") % "c"
    f = tmp_path / "b.kicad_pcb"
    f.write_text("(kicad_pcb\n" + via % "a" + via % "b" + other + ")\n")
    assert PO.dedupe_file(str(f)) == 1
    t = f.read_text()
    assert t.count("(at 155.07 110.95)") == 1 and t.count("(at 160 100)") == 1
    assert PO.dedupe_file(str(f)) == 0
