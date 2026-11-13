"""Rails routed as a tree, differential pairs as one job with a whole-pair audit, plain copper, against a FAKE router."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import routeconn as RC, routediag as RD, routepar as RP, routerules as RR, routeaudit as RA, pairaudit as PA
from fluxplace import railroute as RL, batchroute as BR, routestatus as RS

RC.PY = "/nonexistent/python3"
REPO = os.path.join(os.path.dirname(__file__), "..")
CONS = {"fab": {"min_track_mm": 0.127, "min_clearance_mm": 0.127, "min_via_dia_mm": 0.4, "min_drill_mm": 0.2}}

FAKE = r'''
import sys, os, json
sys.path.insert(0, %r)
from fluxplace import routediag as RD
a = sys.argv
src, out = a[1], a[a.index("--output") + 1]
import itertools
nets = list(itertools.takewhile(lambda t: not t.startswith("--"), a[a.index("--nets") + 1:]))
cfg = json.load(open(os.environ["FAKE_CFG"]))
B = RD.load(src)
txt = open(src).read().rstrip()
add = ""
rec = {"nets": nets, "pads": {n: len([p for p in B["pads"] if p["net"] == n]) for n in nets},
       "width": a[a.index("--power-nets-widths") + 1] if "--power-nets-widths" in a else None}
open(cfg["log"], "a").write(json.dumps(rec) + "\n")
for n in nets:
    ps = [p for p in B["pads"] if p["net"] == n]
    if len(ps) < 2 or n in cfg.get("fail", []):
        continue
    par = list(range(len(ps)))
    def find(i):
        while par[i] != i:
            i = par[i]
        return i
    ctr = {(round(p["x"], 3), round(p["y"], 3)): i for i, p in enumerate(ps)}
    for sg in B["segs"]:
        ia, ib = ctr.get((round(sg["a"][0], 3), round(sg["a"][1], 3))), ctr.get((round(sg["b"][0], 3), round(sg["b"][1], 3)))
        if sg["net"] == n and ia is not None and ib is not None:
            par[find(ia)] = find(ib)
    best = None
    for i in range(len(ps)):
        for j in range(i):
            if find(i) != find(j):
                d = ((ps[i]["x"] - ps[j]["x"]) ** 2 + (ps[i]["y"] - ps[j]["y"]) ** 2) ** 0.5
                if best is None or d < best[0]:
                    best = (d, i, j)
    if best is None:
        continue
    _d, i, j = best
    (x0, y0), (x1, y1) = (ps[i]["x"], ps[i]["y"]), (ps[j]["x"], ps[j]["y"])
    if n in cfg.get("via", []):
        m1, m2 = x0 + (x1 - x0) / 3, x0 + 2 * (x1 - x0) / 3
        sg = lambda xa, xb, ly: '(segment (start %%g %%g) (end %%g %%g) (width 0.15) (layer "%%s") (net "%%s"))\n' %% (xa, y0, xb, y0, ly, n)
        vi = lambda xv: '(via (at %%g %%g) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "%%s"))\n' %% (xv, y0, n)
        add += sg(x0, m1, "F.Cu") + vi(m1) + sg(m1, m2, "In1.Cu") + vi(m2) + sg(m2, x1, "F.Cu")
    else:
        add += '(segment (start %%g %%g) (end %%g %%g) (width 0.15) (layer "F.Cu") (net "%%s"))\n' %% (x0, y0, x1, y1, n)
open(out, "w").write(txt[:-1] + add + ")\n")
''' % REPO


def setup(tmp_path, body, **cfg):
    tmp_path.mkdir(parents=True, exist_ok=True)
    board = G.board(tmp_path, body, outline=(0, 0, 100, 100))
    (tmp_path / "rfake.py").write_text(FAKE)
    cfg["log"] = str(tmp_path / "calls.jsonl")
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    os.environ["FAKE_CFG"] = str(tmp_path / "cfg.json")
    W = tmp_path / "W"
    W.mkdir()
    spath = str(W / "s.json")
    extra = {}
    build = lambda src, dst, only, limited: [sys.executable, str(tmp_path / "rfake.py"), src, "--output", dst, "--nets", *only]
    run = BR.make_runner(build, str(tmp_path), extra, spath, BR.settings({}))
    return board, str(W), build, run, extra, spath


def calls(tmp_path):
    p = tmp_path / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def go(tmp_path, body, nets, cons, workers=3, **cfg):
    board, W, build, run, extra, spath = setup(tmp_path, body, **cfg)
    c = dict(CONS)
    c.update(cons)
    R = RR.rules(c, board, 0.15, 0.15)
    r = RP.route_parallel(board, W, nets, build, run, cons=c, rules=R, workers=workers, job_timeout=10, status_path=spath,
                          extra=extra, log=lambda m: None, cwd=str(tmp_path))
    return board, r, R, spath


def sp(ref, x, y, net):
    return G.fp(ref, x, y, [G.pad("1", 0, 0, net)])


def rail_body(n=6):
    return "".join(G.fp(f"P{i}", 10 + 5 * i, 20, [G.pad("1", 0, 0, "VX")]) for i in range(n)) + \
        sp("RA", 10, 40, "N0") + sp("RB", 30, 40, "N0")


def test_rail_is_a_tree_of_two_pad_jobs(tmp_path):
    board, r, R, spath = go(tmp_path, rail_body(), ["VX", "N0"], {"route": {"rail_min_pads": 4, "rail_edges_per_round": 2},
                                                                  "power": {"VX": {"max_current_ma": 1000}}})
    cs = calls(tmp_path)
    vx = [c for c in cs if c["nets"] == ["VX"]]
    assert len(vx) == 5 and vx[0]["pads"]["VX"] == 2                         # one job per tree edge (the first sees two pads, later ones a joined cluster)
    assert all(c["pads"]["VX"] < 6 for c in vx)                              # never the whole rail
    assert all(c["width"] for c in vx) and not any(len(c["nets"]) > 1 for c in cs)
    assert r["open"] == 0 and r["rails"]["complete"] == 1 and r["rails"]["edges_merged"] == 5
    assert RA.audit(board, r["board"], R) == []
    st = RS.load(spath)
    assert st["rails"]["total"] == 1 and st["rails"]["open_connections"] == 0


def test_rail_below_threshold_or_poured_is_not_a_tree(tmp_path):
    B = RD.load(G.board(tmp_path, rail_body(), outline=(0, 0, 100, 100)))
    assert RL.rail_nets(B, {"route": {"rail_min_pads": 4}})[0] == ["VX"]
    assert RL.rail_nets(B, {})[0] == []                                      # default 8 pads: a 6-pad net is an ordinary net
    rails, poured = RL.rail_nets(B, {"route": {"rail_min_pads": 4}, "plan": {"pour": [{"net": "VX", "layers": ["In1.Cu"]}]}})
    assert rails == [] and poured == ["VX"]
    cl = RL.clusters(B, ["VX"])["VX"]
    es = RL.tree_edges(cl)
    assert len(es) == 5 and all(e["d"] == 5.0 for e in es)                   # nearest cluster first along the row
    assert RL.tree_edges(cl, dead={e["key"] for e in es[:1]})[0]["key"] != es[0]["key"]


def pair_body():
    return sp("RA", 10, 10, "D_P") + sp("RB", 30, 10, "D_P") + sp("RC", 10, 20, "D_N") + sp("RD", 30, 20, "D_N")


def test_pair_is_one_job_and_complete_only_when_both_legs_match(tmp_path):
    board, r, R, spath = go(tmp_path / "ok", pair_body(), ["D_P", "D_N"], {})
    assert [c["nets"] for c in calls(tmp_path / "ok")] == [["D_N", "D_P"]]
    assert r["open"] == 0 and r["pairs"]["complete"] == ["D_N/D_P"]
    # one leg fails: the half result is not merged, never left half
    board, r, R, spath = go(tmp_path / "half", pair_body(), ["D_P", "D_N"], {}, fail=["D_N"])
    assert r["open"] == 2 and r["pairs"]["half-removed"] == ["D_N/D_P"] and not RD.load(r["board"])["segs"]
    # both legs closed but N takes a via: mismatch, not merged
    board, r, R, spath = go(tmp_path / "mm", pair_body(), ["D_P", "D_N"], {}, via=["D_N"])
    assert r["open"] == 2 and not RD.load(r["board"])["segs"] and r["dropped"]


def test_half_pair_in_the_input_is_removed_first(tmp_path):
    body = pair_body()
    p = G.board(tmp_path, body, outline=(0, 0, 100, 100))
    seg = '(segment (start 10 10) (end 30 10) (width 0.15) (layer "F.Cu") (net "D_P"))\n'
    lk = '(segment (start 10 20) (end 12 20) (width 0.15) (layer "F.Cu") (locked yes) (net "D_N"))\n'
    t = open(p).read().rstrip()[:-1] + seg + lk + ")\n"
    open(p, "w").write(t)
    B = RD.load(p)
    cls = {"D_P": {"class": "pair", "partner": "D_N"}, "D_N": {"class": "pair", "partner": "D_P"}}
    res = PA.audit(B, PA.pair_list(cls), {}, {"D_N"})
    assert res[("D_N", "D_P")]["status"] == "half"
    txt, k = PA.strip_legs(t, ["D_P", "D_N"])
    assert k == 1 and "locked yes" in txt and "10 10" not in txt.split("footprint")[-1].split("segment")[-1]
    pr = PA.profile(RD.load(p), "D_P")
    assert pr["path"] and pr["layers"] == ["F.Cu"] and pr["vias"] == 0 and abs(pr["length"] - 20) < 1e-6


def test_plain_copper_net_is_routed_first_and_verified(tmp_path):
    body = sp("RA", 10, 10, "N0") + sp("RB", 30, 10, "N0") + sp("RC", 10, 20, "PL") + sp("RD", 30, 20, "PL")
    cons = {"nets": {"PL": {"straight": True}}}
    board, r, R, spath = go(tmp_path / "a", body, ["N0", "PL"], cons, workers=1)
    assert [c["nets"] for c in calls(tmp_path / "a")][0] == ["PL"] and r["open"] == 0
    board, r, R, spath = go(tmp_path / "b", body, ["N0", "PL"], cons, via=["PL"])
    assert r["leftovers"] == ["PL"] and r["reasons"] == {"PL": "plain copper violated"} and r["dropped"] == {"other": 1}


def test_rail_job_input_has_only_the_two_clusters_copper(tmp_path):
    stub = '(segment (start 35 20) (end 35 22) (width 0.15) (layer "F.Cu") (net "VX"))\n' + \
        '(via (at 35 22) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "VX"))\n'                      # pad P5's fan-out stub
    p = G.board(tmp_path, rail_body() + stub, outline=(0, 0, 100, 100))
    B = RD.load(p)
    mem = {}
    cl = RL.clusters(B, ["VX"], mem)["VX"]
    assert len(cl) == 6 and sum(len(m[1]) for m in mem["VX"]) == 1
    edge = next(e for e in RL.tree_edges(cl) if ("P0", "1") in e["keep"])
    t = RL.job_text(open(p).read(), B, "VX", edge, mem)
    q = tmp_path / "j.kicad_pcb"
    q.write_text(t)
    J = RD.load(str(q))
    assert sorted((x["ref"]) for x in J["pads"] if x["net"] == "VX") == sorted(r for r, _n in edge["keep"])
    assert all(s["net"] == "" for s in J["segs"]) and all(v["net"] == "" for v in J["vias"])                 # the other cluster's stub is an obstacle
    far = next(e for e in RL.tree_edges(cl) if ("P5", "1") in e["keep"])
    q.write_text(RL.job_text(open(p).read(), B, "VX", far, mem))
    J2 = RD.load(str(q))
    assert [s["net"] for s in J2["segs"]] == ["VX"] and [v["net"] for v in J2["vias"]] == ["VX"]            # the stub of a kept cluster stays
