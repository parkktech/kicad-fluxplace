"""Power fixture (PLAN-POWER-PREPLAN section 6): 40 x 30 mm, 4 copper layers (Sig/GND/Sig/Sig style, 35 um), regulator U1 (8 pads)
with inductor L1 on VOUT, connector J1 (10 pads, 4 on VOUT), IC U2 (16 pads, 2 on VOUT), C1 100 nF at U2, C2 22 uF rail-only."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as g  # noqa: E402

CONS = {"power": {"VOUT": {"max_current_ma": 3000, "source": "U1"}},
        "dcdrop": [{"net": "VOUT", "current_a": 3.0, "drop_mv_max": 100.0, "drop_mv_target": 50.0, "from": "L1",
                    "to": ["J1", "U2"], "source": "fixture"}],
        "stackup": {"planes": ["In1.Cu", "In4.Cu"]}, "env": {"temp_max_c": 85}}


def rows(oz_um=0.035):
    cu = lambda n: {"name": n, "type": "copper", "thickness": oz_um}
    di = lambda t: {"name": None, "type": t, "thickness": 0.2, "epsilon_r": 4.4}
    return [cu("F.Cu"), di("prepreg"), cu("In1.Cu"), di("core"), cu("In2.Cu"), di("prepreg"), cu("In3.Cu"), di("core"),
            cu("In4.Cu"), di("prepreg"), cu("B.Cu")]


def cap(ref, x, y, n1, n2, value):
    s = g.two(ref, x, y, n1, n2)
    return s.replace(f'(property "Reference" "{ref}")', f'(property "Reference" "{ref}") (property "Value" "{value}")')


def board(tmp_path, c2=(30, 12), c1=(22, 20), extra=""):
    b = g.ic("U1", 6, 8, ["VIN", "SW", "GND", "FB", "EN", "BOOT", "GND", "PG"], pitch=1.0)
    b += g.two("L1", 12, 8, "SW", "VOUT")
    b += g.fp("J1", 34, 22, [g.pad(str(i + 1), 0, i * 1.0, "VOUT" if i < 4 else ("GND" if i < 8 else "S%d" % i), 0.6, 0.6) for i in range(10)])
    b += g.ic("U2", 22, 24, ["VOUT", "GND", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "VOUT", "GND"], pitch=0.5)
    b += cap("C1", c1[0], c1[1], "VOUT", "GND", "100nF")
    b += cap("C2", c2[0], c2[1], "VOUT", "GND", "22uF")
    for i in range(6):
        b += g.two(f"R{i + 1}", 4 + 5 * i, 28, f"S{i + 1}", "GND")
    return g.board(tmp_path, b + extra, outline=(0, 0, 40, 30))
