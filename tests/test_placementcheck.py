
def test_gate_failures_honours_review_waive(monkeypatch):
    from fluxplace import placementcheck as PCK, legality as LY
    row = {"level": "FAIL", "code": "COLLISION", "pair": ["C1", "J1"], "msg": "C1 x J1: courtyards overlap"}
    monkeypatch.setattr(LY, "collision_gate", lambda B, cons: (False, [row]))
    assert len(PCK.gate_failures(None, {})) == 1
    assert PCK.gate_failures(None, {"review": {"waive": ["COLLISION:C1 x J1"]}}) == []
