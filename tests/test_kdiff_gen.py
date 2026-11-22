"""K14 unit tests for the differential tester: generator determinism, batch independence, attribution on a stub
report, planted wrong kernel verdict counted as a false accept and saved as a case. No kicad-cli, no kernel."""
import json
import math
import random

import kdiff


def _item(i, kind, net, bb, layers=("F.Cu",), locked=False):
    return {"id": i, "uuid": f"00000000-0000-4000-8000-{i:012d}", "kind": kind, "net": i, "net_name": net,
            "layers": list(layers), "bbox": bb, "locked": locked, "shapes": [], "extra": {}}


def _pool_items():
    items = []
    for i in range(60):
        x, y = (i % 10) * 12.0, (i // 10) * 12.0
        items.append(_item(i, "pad" if i % 2 else "track", f"N{i % 7}", [x, y, x + 1.0, y + 0.6]))
    return items


def _edits(seed, n=40):
    items = _pool_items()
    rng = random.Random(seed)
    return [kdiff.gen_edit(rng, i, items, ["F.Cu", "B.Cu"]) for i in range(n)], items


def test_generator_deterministic_per_seed():
    a, _ = _edits(7)
    b, _ = _edits(7)
    c, _ = _edits(8)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert json.dumps(a, sort_keys=True, default=str) != json.dumps(c, sort_keys=True, default=str)


def test_batches_are_independent():
    edits, items = _edits(3, 120)
    pool = kdiff._pool(items)
    batches = kdiff.pack(edits, pool, size=25)
    assert sum(len(b) for b in batches) == len(edits)
    for b in batches:
        assert len(b) <= 25
        for i, x in enumerate(b):
            for y in b[i + 1:]:
                dx = max(y["bbox"][0] - x["bbox"][2], x["bbox"][0] - y["bbox"][2], 0.0)
                dy = max(y["bbox"][1] - x["bbox"][3], x["bbox"][1] - y["bbox"][3], 0.0)
                assert math.hypot(dx, dy) >= kdiff.MARGIN
                assert not (x["netset"] & y["netset"])


def _stub_edit(eid, uuids, nets):
    return {"id": eid, "uuids": uuids, "removes": [], "nets": nets, "adds": []}


def test_attribution_on_stub_report():
    batch = [_stub_edit(0, ["u-a"], ["NA"]), _stub_edit(1, ["u-b"], ["NB"]), _stub_edit(2, ["u-c"], ["NC"])]
    base = {"violations": [], "unconnected_items": []}
    rep = {"violations": [
        {"type": "clearance", "items": [{"uuid": "u-b"}, {"uuid": "old"}]},
        {"type": "silk_overlap", "items": [{"uuid": "u-c"}]},
    ], "unconnected_items": [{"items": [{"description": "Track [NA] on F.Cu"}]}]}
    res = kdiff.classify(base, rep, batch)
    assert res[1]["oracle"] == "bad" and res[1]["types"] == ["clearance"]
    assert res[2]["oracle"] == "good" and res[2]["outside"] == ["silk_overlap"]
    assert res[0]["oracle"] == "bad" and res[0]["types"] == ["opens"]
    assert res[0]["types"] and res[1]["types"] == ["clearance"]


def test_unowned_violation_marks_batch_unattributed():
    batch = [_stub_edit(0, ["u-a"], ["NA"])]
    rep = {"violations": [{"type": "clearance", "items": [{"uuid": "stranger"}]}], "unconnected_items": []}
    res = kdiff.classify({"violations": [], "unconnected_items": []}, rep, batch)
    assert res[0]["oracle"] == "unattributed" and any("unowned" in d for d in res[0]["detail"])


def test_planted_wrong_verdict_is_false_accept_and_saved(tmp_path):
    edits = [{"id": 0, "sub_floor": False, "nominal_gap": None, "kind": "near"},
             {"id": 1, "sub_floor": False, "nominal_gap": None, "kind": "track"}]
    kern = {0: {"verdict": "accept", "violations": [], "ms": 1.0, "zone": False},
            1: {"verdict": "refuse", "violations": [{"type": "clearance", "need": 0.2, "have": 0.1}], "ms": 2.0, "zone": False}}
    oracle = {0: {"oracle": "bad", "types": ["clearance"], "outside": []},   # planted: kernel accepts a bad edit
              1: {"oracle": "good", "types": [], "outside": []}}             # kernel refuses a good edit (not band)
    m, rows, fa, fr, blind = kdiff.score(edits, kern, oracle)
    assert m["false_accepts"] == 1 and m["false_refusals"] == 1 and m["band"] == 0
    kdiff.save_cases(tmp_path, "deadbeef", "board.kicad_pcb", fa, fr, blind)
    saved = sorted(p.name for p in tmp_path.glob("*.json"))
    assert saved == ["deadbeef-1.json", "deadbeef-2.json"]
    assert json.loads((tmp_path / "deadbeef-1.json").read_text())["tag"] == "FA"


def test_kernel_short_oracle_silent_is_blind_not_false_refusal():
    edits = [{"id": 0, "sub_floor": False, "nominal_gap": None, "kind": "near"}]
    kern = {0: {"verdict": "refuse", "violations": [{"type": "short", "need": 0.0, "have": -0.1}], "ms": 1.0, "zone": False}}
    oracle = {0: {"oracle": "good", "types": [], "outside": []}}
    m, rows, fa, fr, blind = kdiff.score(edits, kern, oracle)
    assert m["oracle_blind"] == 1 and m["false_refusals"] == 0 and m["false_accepts"] == 0
