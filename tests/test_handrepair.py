"""repair driver with the search / edit functions replaced by a small fake world (no pcbnew, no DRC)."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import handroute as H, handrepair as RP  # noqa: E402


def _conn(net, a, b):
    return {"net": net, "a": {"id": a, "pin": a}, "b": {"id": b, "pin": b}, "straight_mm": 5.0}


class World:
    def __init__(self, x_relays=True):
        self.open = [_conn("A", "a1", "a2"), _conn("B", "b1", "b2")]
        self.x_removed = False
        self.edits = 0
        self.stack = []
        self.x_relays = x_relays
        self.calls = []

    def push(self):
        self.stack.append((copy.deepcopy(self.open), self.x_removed))
        self.edits += 1

    def close(self, net):
        self.open = [c for c in self.open if c["net"] != net]

    # fakes
    def open_nets(self, board, cons=None, **k):
        return {"connections": list(self.open)}

    def suggest(self, board, net, conn=None, explore=False, **k):
        self.calls.append(("suggest", net, explore))
        if net == "A" and not self.x_removed:
            sb = {"limiting_obstacles": [{"net": "X", "id": "x1", "kind": "track", "locked": False}], "via_sites_in_window": 0, "reached_cells": {"F.Cu": {"cells": 10}}}
            if explore:
                return {"found": False, "reason": "no grid path", "sealed_by": sb, "explore": [{"remove": [{"net": "X", "id": "x1", "kind": "track"}], "then_found": True, "length_mm": 4.0}]}
            return {"found": False, "reason": "no grid path", "sealed_by": sb}
        if net == "X" and not self.x_relays:
            return {"found": False, "reason": "no grid path"}
        return {"found": True, "path": f"p{net}", "length_mm": 3.0}

    def lay(self, board, net, path, out=None, cons=None, *a, **k):
        self.push()
        self.close(net)
        return {"accepted": True}

    def shove(self, board, net, out, ids, window, chain, cons, **k):
        self.push()
        self.x_removed = True
        self.open.append(_conn("X", "x1", "x2"))
        return {"accepted": True}

    def copy_net(self, src, dst, net, out=None, cons=None, **k):
        return {"accepted": False, "reason": "illegal"}

    def status(self, work):
        by = {}
        for c in self.open:
            by[c["net"]] = by.get(c["net"], 0) + 1
        return {"edits_applied": self.edits, "now": {"by_net": by}}

    def undo(self, work):
        self.open, self.x_removed = self.stack.pop()
        self.edits -= 1
        return {"undone": True}


def _install(monkeypatch, w, tmp_path):
    for n in ("open_nets", "suggest", "lay", "shove", "copy_net"):
        monkeypatch.setattr(H, n, getattr(w, n))
    monkeypatch.setattr(H, "session_status", w.status)
    monkeypatch.setattr(H, "session_undo", w.undo)
    monkeypatch.setattr(H, "session_commit", lambda work, out, cons=None, **k: {"accepted": True, "reasons": []})
    sd = tmp_path / "w.kicad_pcb.session"
    sd.mkdir()
    (sd / "meta.json").write_text("{}")
    return str(tmp_path / "w.kicad_pcb")


def test_repair_removes_blocker_lays_blocked_net_and_relays_removed(monkeypatch, tmp_path):
    w = World()
    work = _install(monkeypatch, w, tmp_path)
    rep = str(tmp_path / "r.json")
    r = RP.repair(work, str(tmp_path / "o.kicad_pcb"), report=rep)
    st = {x["net"]: x["status"] for x in r["connections"]}
    assert st == {"A": "closed", "B": "closed"} and r["closed"] == 2 and not w.open
    a = next(x for x in r["connections"] if x["net"] == "A")
    assert a["removed_and_relaid"] == ["X"] and "seal" in a
    assert w.calls[0][1] == "A"                                   # most constrained (sealed) connection first
    assert json.load(open(rep))["closed"] == 2


def test_repair_undoes_attempt_when_removed_net_cannot_be_relaid(monkeypatch, tmp_path):
    w = World(x_relays=False)
    work = _install(monkeypatch, w, tmp_path)
    r = RP.repair(work, str(tmp_path / "o.kicad_pcb"), max_attempts=2)
    a = next(x for x in r["connections"] if x["net"] == "A")
    assert a["status"] == "blocked" and a["attempt_undone"]["removed"] == ["X"] and a["seal"]["reason"]
    assert [c["net"] for c in w.open] == ["A"] and not w.x_removed       # B closed, A left exactly as it was
    assert r["blocked"] == 1 and r["closed"] == 1


def test_repair_reports_pair_nets_without_touching_them(monkeypatch, tmp_path):
    w = World()
    work = _install(monkeypatch, w, tmp_path)
    monkeypatch.setattr(RP, "_pairs", lambda cons, names: {"A"})
    r = RP.repair(work, str(tmp_path / "o.kicad_pcb"), constraints="x.toml")
    assert next(x for x in r["connections"] if x["net"] == "A")["status"] == "pair"
    assert [c["net"] for c in w.open] == ["A"]


def test_repair_zero_budget_still_writes_report_and_commits(monkeypatch, tmp_path):
    w = World()
    work = _install(monkeypatch, w, tmp_path)
    report = tmp_path / "budget.json"
    r = RP.repair(work, str(tmp_path / "o.kicad_pcb"), budget=0, report=str(report))
    saved = json.load(open(report))
    assert saved["budget_exhausted"] and saved["commit"]["accepted"]
    assert saved["connections"] == [] and r["elapsed_s"] >= 0


def test_repair_exception_still_writes_partial_report(monkeypatch, tmp_path):
    w = World()
    work = _install(monkeypatch, w, tmp_path)
    monkeypatch.setattr(H, "open_nets", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    report = tmp_path / "error.json"
    try:
        RP.repair(work, str(tmp_path / "o.kicad_pcb"), report=str(report))
    except RuntimeError:
        pass
    saved = json.load(open(report))
    assert saved["error"]["message"] == "synthetic failure"
