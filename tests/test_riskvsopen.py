import json
from fluxplace import planroute as PR


def test_risk_vs_open_counts():
    d, line = PR.risk_vs_open(["A", "B", "C"], {"B": 2, "D": 1})
    assert d["both"] == ["B"] and d["at_risk_routed"] == 2 and d["open_not_at_risk"] == 1
    assert "at-risk 3, open 2, both 1" in line


def test_ledger_line(tmp_path):
    m = tmp_path / "b.fluxmap.json"
    m.write_text(json.dumps({"verdict": {"risk_nets": ["A", "B"]}}))
    led = tmp_path / "LEDGER.txt"
    d = PR.risk_vs_open_ledger(str(led), str(m), "x.kicad_pcb", unc={"B": 1, "C": 3, "Z": 0})
    assert d["both"] == ["B"] and d["open_not_at_risk"] == 1
    assert "at-risk-but-routed 1" in led.read_text()


def test_old_map_noop(tmp_path):
    m = tmp_path / "b.fluxmap.json"
    m.write_text(json.dumps({"verdict": {}}))
    assert PR.risk_vs_open_ledger(str(tmp_path / "L"), str(m), "x", unc={}) is None
    assert not (tmp_path / "L").exists()
