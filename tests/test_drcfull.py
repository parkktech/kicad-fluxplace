"""fluxplace/drcfull.py: kicad-cli DRC caps `unconnected_items` at CAP (499); complete() restores per-net counts
from pcbnew connectivity. Unit and guard tests use fake DRC/connectivity data (no kicad-cli); the integration test
builds a real board with more opens than the cap and checks the completed total against pcbnew."""
from fluxplace import procs as _procs_mod
import json
import os
import shutil
import subprocess

import pytest

from fluxplace import drcfull as DF
from fluxplace import handroute as H


def _clusters(n_clusters, ref):
    return [[{"ref": f"{ref}{k}", "num": "1", "x": float(k), "y": 0.0, "layers": ["F.Cu"]}]
            for k in range(n_clusters)]


def _conn(counts):
    """pcbnew-style result for {net: opens}: a net with `opens` opens has opens+1 pad clusters."""
    nets = {n: c for n, c in counts.items() if c > 0}
    return {"nets": nets, "unconnected": sum(nets.values()),
            "clusters": {n: _clusters(c + 1, n) for n, c in nets.items()}}


def _entry(net, i):
    return {"type": "unconnected_items", "severity": "error", "description": "Missing connection between items",
            "items": [{"description": f"Pad 1 [{net}] of {net}{i} on F.Cu", "pos": {"x": 0.0, "y": float(i)}, "uuid": f"u{net}{i}"},
                      {"description": f"Pad 2 [{net}] of {net}{i}b on F.Cu", "pos": {"x": 1.0, "y": float(i)}, "uuid": f"v{net}{i}"}]}


def _listed(counts, cap=DF.CAP):
    """A capped kicad-cli list: entries in the order given by `counts` (dict insertion), cut at `cap`."""
    out = [_entry(n, i) for n, c in counts.items() for i in range(c)]
    return out[:cap]


def _net_counts(items):
    from collections import Counter
    return Counter(DF.net_of(u) for u in items)


def test_below_cap_unchanged(monkeypatch):
    monkeypatch.setattr(DF, "_conn", lambda *a: pytest.fail("pcbnew must not be asked below the cap"))
    d = {"unconnected_items": [_entry("X", i) for i in range(DF.CAP - 6)]}
    assert DF.complete("b.kicad_pcb", d) is d


def test_complete_appends_per_net(monkeypatch):
    listed = _listed({"BULK": DF.CAP - 2, "X": 2})  # 499 entries, X already at 2 (listing cut the rest)
    assert len(listed) == DF.CAP
    conn = _conn({"BULK": DF.CAP - 2, "X": 3, "Y": 1, "Z": 2})
    monkeypatch.setattr(DF, "_conn", lambda board, fill: conn)
    d = {"unconnected_items": listed}
    out = DF.complete("b.kicad_pcb", d, fill=True)
    assert out is not d and len(d["unconnected_items"]) == DF.CAP  # input untouched
    got = _net_counts(out["unconnected_items"])
    assert got["X"] == 3 and got["Y"] == 1 and got["Z"] == 2 and got["BULK"] == DF.CAP - 2
    assert len(out["unconnected_items"]) == conn["unconnected"] == 3 + 1 + 2 + DF.CAP - 2
    assert out["unconnected_capped"] is True and out["unconnected_true"] == conn["unconnected"]
    synth = out["unconnected_items"][DF.CAP:]
    assert all(u["uuid"] == "" for it in synth for u in it["items"])
    assert all("synthesised" in u["description"] for u in synth)


def test_synthetic_pads_are_nearest_pair(monkeypatch):
    clusters = {"Q": [[{"ref": "R1", "num": "1", "x": 0.0, "y": 0.0, "layers": ["F.Cu"]}],
                      [{"ref": "R2", "num": "2", "x": 9.0, "y": 0.0, "layers": ["F.Cu"]},
                       {"ref": "R3", "num": "1", "x": 1.0, "y": 0.0, "layers": ["F.Cu"]}]]}
    monkeypatch.setattr(DF, "_conn", lambda board, fill: {"nets": {"Q": 1}, "unconnected": 1, "clusters": clusters})
    out = DF.complete("b.kicad_pcb", {"unconnected_items": _listed({"BULK": DF.CAP})})
    syn = out["unconnected_items"][-1]["items"]
    assert [i["description"] for i in syn] == ["Pad 1 [Q] of R1 on F.Cu", "Pad 1 [Q] of R3 on F.Cu"]


def _fake_drc(monkeypatch, boards):
    """kicad-cli stand-in: writes boards[board_path] as the -o report. Keeps the real drc_raw/summarize_drc path."""
    # k17 memo: drc_raw keys on the file content sha, and these board paths do not exist (every sha equal) -> key by path, fresh memo
    monkeypatch.setattr(H, "board_sha", lambda board: "fake:" + os.path.basename(board))
    monkeypatch.setattr(H, "_DRC_MEMO", {})
    def run(cmd, **kw):
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as fh:
            json.dump(boards[cmd[-1]], fh)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(_procs_mod, "run", run)


def test_guard_sees_no_net_increase(monkeypatch):
    """A = real 501 (X=2, Y=1, Z=1, BULK=497); B = real 500 after a lay closed one X open (X=1, Y=1, Z=1, BULK=497).
    Both lists are capped; B's list has Y where A's omitted it. Completed, the guard's per-net diff shows only X down."""
    conn = {"A.kicad_pcb": _conn({"X": 2, "Y": 1, "Z": 1, "BULK": 497}),
            "B.kicad_pcb": _conn({"X": 1, "Y": 1, "Z": 1, "BULK": 497})}
    listed = {"A.kicad_pcb": _listed({"X": 2, "BULK": 497}), "B.kicad_pcb": _listed({"X": 1, "Y": 1, "BULK": 497})}
    assert len(listed["A.kicad_pcb"]) == len(listed["B.kicad_pcb"]) == DF.CAP
    monkeypatch.setattr(DF, "_conn", lambda board, fill: conn[os.path.basename(board)])
    _fake_drc(monkeypatch, {k: {"violations": [], "unconnected_items": v} for k, v in listed.items()})

    base = H.summarize_drc(H.drc_raw("A.kicad_pcb"))["unc_by_net"]
    cand = H.summarize_drc(H.drc_raw("B.kicad_pcb"))["unc_by_net"]
    new_u = {n: (base.get(n, 0), c) for n, c in cand.items() if c > base.get(n, 0)}  # handroute lay guard
    assert new_u == {}
    assert base == {"X": 2, "Y": 1, "Z": 1, "BULK": 497} and cand == {"X": 1, "Y": 1, "Z": 1, "BULK": 497}


def test_uncapped_list_untouched_by_guard(monkeypatch):
    """Below the cap the guard sees exactly the kicad-cli list, nothing synthesised."""
    small = {"unconnected_items": [_entry("X", 0), _entry("Y", 0)], "violations": []}
    _fake_drc(monkeypatch, {"S.kicad_pcb": small})
    monkeypatch.setattr(DF, "_conn", lambda *a: pytest.fail("not asked below cap"))
    assert H.summarize_drc(H.drc_raw("S.kicad_pcb"))["unc_by_net"] == {"X": 1, "Y": 1}


pcbnew = pytest.importorskip("pcbnew")
if not shutil.which("kicad-cli"):
    pytest.skip("kicad-cli missing", allow_module_level=True)


def test_integration_real_board_total_matches_pcbnew(tmp_path):
    """520 two-pad nets, all unrouted: real kicad-cli DRC caps its list; complete() must total pcbnew's count."""
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(2)
    nets = []
    for i in range(520):
        ni = pcbnew.NETINFO_ITEM(b, f"N{i:03d}")
        b.Add(ni)
        nets.append(ni)
    for i, ni in enumerate(nets):
        for j in range(2):
            fp = pcbnew.FOOTPRINT(b)
            fp.SetReference(f"P{i:03d}_{j}")
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(2 + (i % 40) * 3), pcbnew.FromMM(2 + (i // 40) * 3 + j * 1.2)))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.5), pcbnew.FromMM(0.5)))
            pad.SetPosition(fp.GetPosition())
            pad.SetNet(ni)
            fp.Add(pad)
            b.Add(fp)
    path = str(tmp_path / "many.kicad_pcb")
    b.Save(path)
    out = str(tmp_path / "drc.json")
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "-o", out, path],
                   capture_output=True, text=True, timeout=300)
    d = json.load(open(out))
    assert len(d["unconnected_items"]) <= DF.CAP
    truth = json.loads([l for l in subprocess.run(
        [DF.PY, DF.HELPER, path], capture_output=True, text=True, timeout=300).stdout.splitlines() if l.startswith("{")][-1])
    got = DF.complete(path, d)
    assert got["unconnected_true"] == truth["unconnected"] == 520
    assert len(got["unconnected_items"]) == truth["unconnected"]
