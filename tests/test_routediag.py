"""route-diagnose: log parsing and each cause on small synthetic boards."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import routediag as RD

HEAD = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'


def pad(num, x, y, net, w=0.5, h=0.5, typ="smd"):
    n = f' (net "{net}")' if net else ""
    return f'(pad "{num}" {typ} rect (at {x} {y}) (size {w} {h}) (layers "F.Cu"){n})'


def fp(ref, x, y, pads):
    return (f'(footprint "X" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}")'
            f' {" ".join(pads)})\n')


def seg(a, b, net, w=0.15):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "F.Cu") (net "{net}"))\n'


def board(tmp_path, body, name="b.kicad_pcb"):
    edge = '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))\n'
    p = tmp_path / name
    p.write_text(HEAD + edge + body + ")\n")
    return str(p)


def diag(path, nets):
    B = RD.load(path)
    return RD.diagnose(B, nets)


def causes(res, net):
    return {c["cause"] for n in res["nets"] if n["net"] == net for c in n["causes"]}


# ---------------------------------------------------------------- log
def test_log_last_attempt_wins(tmp_path):
    log = tmp_path / "r.log"
    log.write_text(
        "[1/3] Routing A (id=1)\n  FAILED: Could not find route (1.0s)\n\n"
        "[2/3 (1 failed)] Routing B (id=2)\n  SUCCESS: 3 segments\n\n"
        "[3/3 (1 failed)] Routing C (id=3)\n  FAILED: Could not find route\n"
        "  RETRY FAILED (N=1)\n  RETRY SUCCESS (N=10): 5 segments\n\n"
        "[REROUTE 1/2 (1 failed)] Re-routing ripped net B\n  REROUTE FAILED: (2s) - attempting rip-up\n"
        "  REROUTE RETRY FAILED (N=1)\n\n")
    log2 = tmp_path / "r2.log"
    log2.write_text("[1/1] Routing A (id=1)\n  SUCCESS: ok\n")
    assert RD.parse_logs([str(log)]) == {"A": "FAILED", "B": "FAILED", "C": "SUCCESS"}
    assert RD.parse_logs([str(log), str(log2)])["A"] == "SUCCESS"


# ---------------------------------------------------------------- causes
def test_far_support_part_and_tight_decap(tmp_path):
    p = board(tmp_path,
              fp("U1", 20, 20, [pad(1, 0, 0, "N"), pad(2, 2, 0, "G"), pad(3, 4, 0, "H")]) +
              fp("C1", 28, 20, [pad(1, 0, 0, "N"), pad(2, 1, 0, "GND")]) +
              fp("R1", 24, 20, [pad(1, 0, 0, "M"), pad(2, 1, 0, "N")]))
    res = diag(p, ["N"])
    far = {c["part"]: c for c in res["nets"][0]["causes"] if c["cause"] == "FAR_SUPPORT_PART"}
    assert set(far) == {"C1", "R1"} or set(far) == {"C1"}
    assert far["C1"]["limit"] == 2.5 and far["C1"]["toward"] == "U1.1"      # cap to GND: tight
    assert "move C1 to within 2.5 mm of U1.1" in far["C1"]["suggestion"]
    assert "R1" not in far or far["R1"]["limit"] == 5.0
    assert res["summary"]["moves"][0]["part"] in ("C1", "R1")


def test_long_haul_moves_smaller_part(tmp_path):
    p = board(tmp_path,
              fp("U1", 10, 10, [pad(i, i * 1.0, 0, "N" if i == 1 else f"x{i}") for i in range(1, 7)]) +
              fp("Q1", 80, 70, [pad(1, 0, 0, "N"), pad(2, 1, 0, "y"), pad(3, 2, 0, "z")]))
    res = diag(p, ["N"])
    c = [c for c in res["nets"][0]["causes"] if c["cause"] == "LONG_HAUL"][0]
    assert c["part"] == "Q1" and c["toward"] == "U1.1" and c["dist"] > 80
    assert res["summary"]["counts"]["LONG_HAUL"] == 1
    assert res["summary"]["moves"][0]["from"] == [80.0, 70.0]


def test_move_list_ranks_part_fixing_most_nets_first(tmp_path):
    big = fp("U1", 10, 10, [pad(i, i, 0, n) for i, n in enumerate(["A", "B", "C", "D", "E"], 1)])
    small = fp("U2", 80, 10, [pad(i, i, 0, n) for i, n in enumerate(["A", "B"], 1)] + [pad(3, 3, 0, "z")])
    other = fp("U3", 10, 70, [pad(1, 0, 0, "C"), pad(2, 1, 0, "q"), pad(3, 2, 0, "r")])
    res = diag(board(tmp_path, big + small + other), ["A", "B", "C"])
    mv = res["summary"]["moves"]
    assert mv[0]["part"] == "U2" and sorted(mv[0]["nets"]) == ["A", "B"]


def test_sealed_pad_names_blocker(tmp_path):
    # 3-pad fine-pitch part (gap 0.2); pad 1's outward (left) side is closed by a track
    u = fp("U1", 30, 30, [pad(1, 0, 0, "N", 0.6, 0.3), pad(2, 0, 0.5, "P", 0.6, 0.3),
                          pad(3, 0, 1.0, "Q", 0.6, 0.3), pad(4, 2, 0, "R", 0.6, 0.3),
                          pad(5, 2, 0.5, "S", 0.6, 0.3), pad(6, 2, 1.0, "T", 0.6, 0.3)])
    far = fp("U2", 40, 30, [pad(1, 0, 0, "N"), pad(2, 1, 0, "k"), pad(3, 2, 0, "l")])
    blocker = seg((29.55, 28), (29.55, 32), "+3V3")      # 0.15 mm left of the pad edge (29.7)
    res = diag(board(tmp_path, u + far + blocker), ["N"])
    c = [c for c in res["nets"][0]["causes"] if c["cause"] == "SEALED_PAD"]
    assert c and c[0]["part"] == "U1" and c[0]["pad"] == "1" and c[0]["blockers"][0] == "+3V3"
    assert "fan out every pin of U1" in c[0]["suggestion"]
    # without the blocker the pad is not sealed
    res = diag(board(tmp_path, u + far, "c.kicad_pcb"), ["N"])
    assert "SEALED_PAD" not in causes(res, "N")


def test_keepout_trapped(tmp_path):
    z = ('(zone (net 0) (net_name "") (layers "F.Cu") (name "ANT_KO") (hatch edge 0.5) '
         '(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed)) '
         '(polygon (pts (xy 8 8) (xy 12 8) (xy 12 12) (xy 8 12))))\n')
    body = (fp("L1", 10, 10, [pad(1, 0, 0, "N"), pad(2, 1, 0, "a")]) +
            fp("C1", 20, 10, [pad(1, 0, 0, "N"), pad(2, 1, 0, "b")]) + z)
    res = diag(board(tmp_path, body), ["N"])
    c = [c for c in res["nets"][0]["causes"] if c["cause"] == "KEEPOUT_TRAPPED"]
    assert [x["part"] for x in c] == ["L1"] and "ANT_KO" in c[0]["evidence"]


def test_sub_rule_pad_neckdown_never_below_floor(tmp_path):
    body = (fp("U1", 10, 10, [pad(1, 0, 0, "N", 0.1, 0.5), pad(2, 2, 0, "a"), pad(3, 4, 0, "b")]) +
            fp("U2", 14, 10, [pad(1, 0, 0, "N"), pad(2, 2, 0, "c"), pad(3, 4, 0, "d")]))
    res = diag(board(tmp_path, body), ["N"])
    c = [c for c in res["nets"][0]["causes"] if c["cause"] == "SUB_RULE_PAD"][0]
    assert "track 0.127 mm" in c["suggestion"]


def test_congestion_fallback_and_missing_net(tmp_path):
    body = (fp("U1", 10, 10, [pad(1, 0, 0, "N"), pad(2, 2, 0, "a"), pad(3, 4, 0, "b")]) +
            fp("U2", 14, 10, [pad(1, 0, 0, "N"), pad(2, 2, 0, "c"), pad(3, 4, 0, "d")]))
    res = diag(board(tmp_path, body), ["N", "NOPE"])
    assert causes(res, "N") == {"CONGESTION"} and res["missing"] == ["NOPE"]
    assert 0 <= res["nets"][0]["causes"][0]["fill"] <= 1


def test_unrouted_nets_from_board(tmp_path):
    body = (fp("U1", 10, 10, [pad(1, 0, 0, "A"), pad(2, 2, 0, "B"), pad(3, 4, 0, "ONE")]) +
            fp("U2", 14, 10, [pad(1, 0, 0, "A"), pad(2, 2, 0, "B"), pad(3, 4, 0, "")]) +
            seg((12, 10), (14, 10), "B"))
    assert RD.unrouted_nets(RD.load(board(tmp_path, body))) == ["A"]


# ---------------------------------------------------------------- CLI
def test_cli_read_only_exit0_and_json(tmp_path):
    body = (fp("U1", 10, 10, [pad(1, 0, 0, "N"), pad(2, 1, 0, "a"), pad(3, 2, 0, "b")]) +
            fp("Q1", 90, 70, [pad(1, 0, 0, "N"), pad(2, 1, 0, "c"), pad(3, 2, 0, "d")]))
    p = board(tmp_path, body)
    before = open(p).read()
    out = tmp_path / "o.json"
    cli = os.path.join(os.path.dirname(__file__), "..", "cli.py")
    r = subprocess.run([sys.executable, cli, "route-diagnose", p, "--nets", "N", "--json", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "LONG_HAUL" in r.stdout and "MOVE LIST" in r.stdout
    d = json.load(open(out))
    assert d["nets"][0]["net"] == "N" and d["summary"]["counts"]["LONG_HAUL"] == 1
    assert open(p).read() == before
