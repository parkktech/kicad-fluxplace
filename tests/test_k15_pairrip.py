"""k15: the last opens of W16 route-9 (k11/fin): sealed GND pour islands (rip-and-lay), pair re-lay through locked copper."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import gaps as GP  # noqa: E402


def _brd(tmp_path):
    seg = lambda net, lk: (f'\t(segment\n\t\t(start 0 0)\n\t\t(end 1 0)\n\t\t(width 0.2)\n' + ("\t\t(locked yes)\n" if lk else "")
                           + f'\t\t(layer "F.Cu")\n\t\t(net "{net}")\n\t\t(uuid "00000000-0000-0000-0000-{abs(hash(net)) % 10**12:012d}")\n\t)\n')
    b = str(tmp_path / "b.kicad_pcb")
    open(b, "w").write("(kicad_pcb\n" + seg("D_P", True) + seg("D_N", True) + seg("LOCKED_SIG", True) + seg("RFX", True) + ")\n")
    return b


def test_pairrip_rips_the_locked_blocker_the_pair_layer_names_and_never_a_protected_net(tmp_path, monkeypatch):
    from fluxplace import pairrip as PRP, pairs as PR
    b = _brd(tmp_path)
    calls = []

    def lay(path, cons, only):
        text = open(path).read()
        calls.append(("D_P" in text, "LOCKED_SIG" in text, "RFX" in text))
        if "LOCKED_SIG" in text:
            return {"pairs": [{"pair": ["D_P", "D_N"], "status": "not-laid", "why": "no path",
                               "blockers": ["track LOCKED_SIG (x)", "via RFX", "pad U1.1 (GND)"]}], "B": {}}
        return {"pairs": [{"pair": ["D_P", "D_N"], "status": "laid"}], "B": {}}
    monkeypatch.setattr(PR, "write", lambda pre, out, res, drill: open(out, "w").write(open(pre).read()))
    monkeypatch.setattr(PR, "rules_for", lambda *a, **k: {"drill": 0.2})
    monkeypatch.setattr(PR, "params", lambda c: {})
    monkeypatch.setattr(PR, "project_classes", lambda b: None)
    from fluxplace import routerules as RR
    monkeypatch.setattr(RR, "rules", lambda *a, **k: {})
    out = str(tmp_path / "o.kicad_pcb")
    r = PRP.relay(b, out, ("D_P", "D_N"), {}, never={"RFX"}, lay=lay)
    assert r["status"] == "laid" and r["ripped"] == ["LOCKED_SIG"]
    assert calls[0] == (False, True, True) and calls[1] == (False, False, True)     # legs stripped first, then the locked blocker; RF kept
    assert "RFX" in open(out).read() and "LOCKED_SIG" not in open(out).read()


def test_pairrip_stops_when_only_protected_nets_block(tmp_path):
    from fluxplace import pairrip as PRP
    b = _brd(tmp_path)
    lay = lambda path, cons, only: {"pairs": [{"pair": ["D_P", "D_N"], "status": "not-laid", "why": "no path", "blockers": ["via RFX"]}], "B": {}}
    r = PRP.relay(b, str(tmp_path / "o.kicad_pcb"), ("D_P", "D_N"), {}, never={"RFX"}, lay=lay)
    assert r["status"] == "blocked" and r["ripped"] == [] and not os.path.exists(tmp_path / "o.kicad_pcb")


def test_rung_release_names_ripped_nets_but_never_a_hard_one(monkeypatch):
    from fluxplace import lockpolicy as LP, routediag as RD
    monkeypatch.setattr(RD, "load", lambda b: {"pads": [{"net": "A"}, {"net": "RF1"}]})
    monkeypatch.setattr(LP, "level", lambda n, c, names: LP.HARD if n == "RF1" else LP.FREE)
    assert GP._rung_release({"release": ["A", "RF1"]}, {}, "b") == {"A"}
    assert GP._rung_release({}, {}, "b") == set()
