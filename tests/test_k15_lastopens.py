"""k15: the last opens of W16 route-9 (k11/fin): sealed GND pour islands (rip-and-lay), pair re-lay through locked copper."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import gaps as GP  # noqa: E402


def test_pourisland_rung_relays_the_nets_a_rip_and_lay_opened(tmp_path, monkeypatch):
    from fluxplace import pourisland as P
    b = str(tmp_path / "b.kicad_pcb")
    open(b, "w").write("(kicad_pcb)")
    n = {"k": 0}

    def repair(cur, tmp, net, constraints=None):
        if n["k"] < 1:
            n["k"] += 1
            open(tmp, "w").write("tied")
            return {"accepted": True, "ripped": {"SIG": 1}}
        return {"accepted": False}
    seen = {}

    def relay(cur, nxt, nets, ctx):
        seen["nets"] = set(nets)
        open(nxt, "w").write("relaid")
        return nxt
    monkeypatch.setattr(P, "repair", repair)
    monkeypatch.setattr(GP, "_relay_open", relay)
    r = GP._r_pourisland(b, str(tmp_path / "o.kicad_pcb"), [{"kind": "open-pour", "nets": ["GND"]}], {"work": str(tmp_path), "cons_path": None})
    assert r["wrote"] and seen["nets"] == {"SIG"} and open(tmp_path / "o.kicad_pcb").read() == "relaid"
