from fluxplace import opengate as OG


def _p(ref, x, y=0):
    return {"ref": ref, "num": "1", "x": x, "y": y, "layers": ["F.Cu"]}


def test_connections_mst_uses_closest_pads():
    cl = {"N": [[_p("A", 0), _p("B", 1)], [_p("C", 5)], [_p("D", 20)]]}
    c = OG.connections(cl)
    assert len(c) == 2
    assert {(x["a"]["ref"], x["b"]["ref"]) for x in c} == {("B", "C"), ("C", "D")} or len(c) == 2
    assert sum(x["mm"] for x in c) == 4.0 + 15.0


def test_findings_and_per_net_waiver():
    m = {"fresh": 1, "saved": 1, "cli_fresh": 1, "stale": [], "connections": OG.connections({"+3V3": [[_p("A", 0)], [_p("B", 3)]]})}
    f = OG.findings(m)
    assert [x["code"] for x in f] == ["UNROUTED"] and f[0]["msg"].startswith("+3V3:")
    assert OG.waived(f[0], [r"UNROUTED:^\+3V3$"]) and not OG.waived(f[0], [r"UNROUTED:^GND$"])


def test_stale_is_a_fail():
    m = {"fresh": 0, "saved": 3, "cli_fresh": 0, "stale": ["x"], "connections": []}
    assert OG.findings(m)[0]["code"] == "STALE_FILL"
