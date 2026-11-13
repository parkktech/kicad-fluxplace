"""join-gaps on a synthetic board (pcbnew runs in /usr/bin/python3, like the fan-out tests)."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import joingaps as JG
from fluxplace import routeconn as RC

HAVE = os.path.exists("/usr/bin/python3") and subprocess.run(
    ["/usr/bin/python3", "-c", "import pcbnew"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not HAVE, reason="pcbnew not available")


def two(ref, x, y, n1, n2):
    return PB.fp(ref, x, y, [PB.pad("1", -0.5, 0, n1), PB.pad("2", 0.5, 0, n2)])


def seg(a, b, net, w=0.2, layer="F.Cu"):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}"))\n'


def row(y, net, gap, x0=9.5):
    """pad - track - gap - track - pad of one net at height y; the pads' other side is a foreign net."""
    xa, xb = 12.0, 12.0 + gap
    body = two(f"A{net}", x0 + 0.5, y, net, "Z" + net) + two(f"B{net}", 15.5, y, net, "Y" + net)
    return body + seg((x0, y), (xa, y), net) + seg((xb, y), (15, y), net)


def board(tmp_path):
    body = row(10, "T1", 0.2)                                          # closable
    body += row(20, "T2", 0.2) + seg((12.1, 17), (12.1, 23), "FOR")    # a foreign track across the gap
    body += row(30, "T3", 0.6)                                         # wider than the default gap
    body += row(50, "T5", 0.2) + PB.keepout(11.9, 49, 12.3, 51).replace('"F.Cu" "B.Cu"', '"F.Cu"')   # a keep-out over the gap
    # a track end 0.15 mm short of a pad of its own net
    body += two("C4", 12.9, 40, "T4", "Z4") + two("D4", 10, 40, "T4", "Y4") + seg((9.5, 40), (12.0, 40), "T4")
    return PB.write(tmp_path, body)


def test_join_gaps_closes_legal_gaps_and_reports_the_rest(tmp_path):
    b = board(tmp_path)
    before = RC.open_by_net(b, fill=False, method="pcbnew")
    assert {"T1", "T2", "T3", "T4", "T5"} <= set(before)
    out = str(tmp_path / "o.kicad_pcb")
    rep = JG.run(b, out)
    closed = {s["net"] for s in rep["add"]}
    assert closed == {"T1", "T4"}, rep
    why = {s["net"]: s["reason"] for s in rep["skipped"] if s["reason"] != "already connected"}
    assert "clearance" in why.get("T2", "") and "rule area" in why.get("T5", "")
    assert "T3" not in closed and "T3" not in why                      # 0.6 mm > 0.3 mm: not a candidate at all
    after = RC.open_by_net(out, fill=False, method="pcbnew")
    assert sum(after.values()) == sum(before.values()) - 2
    assert JG.summary(rep).startswith("join-gaps: closed 2 gap(s) <= 0.3 mm, skipped 2")
    # configurable: with --gap 1 the wide gap closes too
    rep2 = JG.run(b, str(tmp_path / "o2.kicad_pcb"), gap=1.0)
    assert {s["net"] for s in rep2["add"]} == {"T1", "T3", "T4"}


def test_join_gaps_cli_and_sidecars(tmp_path, capsys):
    import cli
    b = board(tmp_path)
    open(os.path.splitext(b)[0] + ".kicad_pro", "w").write("{}")
    out = str(tmp_path / "o.kicad_pcb")
    cli.main(["join-gaps", b, "--out", out])
    assert "closed 2 gap(s)" in capsys.readouterr().out
    assert os.path.exists(os.path.splitext(out)[0] + ".kicad_pro")
    with pytest.raises(SystemExit):
        cli.main(["join-gaps", b, "--out", b])
