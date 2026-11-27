"""w73b: a re-net run on a routed board routes only the released nets (planes, rails, pairgroups never touch protected copper), the released
pair is laid coupled by the pair router, and the route commit is accepted."""
import json
import os
import shutil
import sys

import pytest

pcbnew = pytest.importorskip("pcbnew")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "golden"))
sys.path.insert(0, os.path.dirname(__file__))
from golden import harness as H                                            # noqa: E402
from golden.test_golden import duo                                         # noqa: E402
from fluxplace import boardwrite as BW, pipeline as PL, routeconn as RC   # noqa: E402

CONS = ('[preroute]\nsignal_layers=["F.Cu","B.Cu"]\n[pairlay]\nworkers=1\n[route]\nrouter = "krt-whole"\n'
        '[pairs.USB_]\nimpedance_diff = 90\nskew_mm = 0.1\n[pairs.ETH_]\nimpedance_diff = 100\n')


def _router():
    from fluxplace import routerloc as RL
    try:
        RL.resolve(None, None)
    except RuntimeError:
        pytest.skip("KiCadRoutingTools not located")


def _seg(a, b, net):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) (layer "F.Cu") (net "{net}") (locked yes))\n'


def _usb_unrouted_to_spec():
    """An unrelated, protected, locked USB pair whose legs are far apart and 8 mm out of skew: a whole-board pairgroups pass would
    re-lay it (the audit calls it incomplete), which the manifest forbids."""
    parts = duo("U1", 8, 8, "USB_P", "USB_N") + duo("J1", 30, 8, "USB_P", "USB_N")
    cu = "".join(_seg(a, b, n) for a, b, n in [((8, 8), (8, 6), "USB_P"), ((8, 6), (30, 6), "USB_P"), ((30, 6), (30, 8), "USB_P"), ((8.5, 8), (8.5, 12), "USB_N"),
                                                 ((8.5, 12), (30.5, 12), "USB_N"), ((30.5, 12), (30.5, 8), "USB_N")])
    return parts + cu


def _renetted(tmp_path):
    """ETH pair laid by the pairs stage, an unrelated locked USB pair spliced in; then both ETH pads of J2 swap nets (a re-net)."""
    b = H.write_board(tmp_path, duo("U2", 8, 22, "ETH_P", "ETH_N") + duo("J2", 30, 22, "ETH_P", "ETH_N"))
    c = H.context(tmp_path, b, CONS)
    (r,) = H.chain(c, b, ["pairs"])
    assert r["verdict"] in ("pass", "warn"), r["line"]
    routed = os.path.join(str(tmp_path), "routed.kicad_pcb")
    txt = open(r["board"]).read()
    i = txt.index('(property "Reference" "J2"')
    j = txt.index("(footprint", i) if "(footprint" in txt[i:] else len(txt)
    blk = txt[i:j].replace('(net "ETH_P")', "@@").replace('(net "ETH_N")', '(net "ETH_P")').replace("@@", '(net "ETH_N")')
    txt = txt[:i] + blk + txt[j:]
    open(routed, "w").write(txt.rstrip().rstrip(")") + _usb_unrouted_to_spec() + ")\n")
    src, dst = os.path.splitext(r["board"])[0], os.path.splitext(routed)[0]
    for ext in (".kicad_pro", ".kicad_dru"):
        if os.path.exists(src + ext):
            shutil.copy(src + ext, dst + ext)
    BW.seed(routed)
    return routed


def test_renet_run_routes_only_the_released_pair_and_commit_is_accepted(tmp_path):
    _router()
    routed = _renetted(tmp_path)
    c = H.use_krt(H.context(tmp_path, routed, CONS, argv=["--route-budget", "300"]))
    usb0 = BW.net_items(routed, {"USB_P", "USB_N"})
    out = PL._renet_entry(c, c.work, routed, c.state)
    assert out != routed and c.state["route_scope"]["nets"] == ["ETH_N", "ETH_P"]
    res = PL._exec_stage(c, 7, "route", out, ())
    assert "commit refused" not in res["line"] and res["verdict"] in ("pass", "warn"), res["line"]
    assert BW.net_items(res["board"], {"USB_P", "USB_N"}) == usb0            # the protected pair is byte-for-byte the same copper
    assert not (set(RC.open_by_net(res["board"])) & {"ETH_P", "ETH_N"}), res["line"]
    rep = json.load(open(os.path.join(c.work, "7-route", "1-pairgroups.json")))      # the pairs machinery laid the released pair, scoped to it
    assert not rep.get("failed") and "ETH_P" in json.dumps(rep, default=list), rep
    assert "USB_P" not in json.dumps(rep, default=list)
