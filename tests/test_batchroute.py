"""Batch routing with checkpoints, against a FAKE router (no real py_router involved)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import batchroute as BR, routediag as RD, routestatus as RS, routeconn as RC

RC.PY = "/nonexistent/python3"       # no pcbnew helper in tests: the text connectivity answers

# argv: src --output dst [--max-ripup N] --nets a b c ; behaviour from $FAKE_CFG (json)
FAKE = r'''
import sys, os, json, time, signal
a = sys.argv
src, out = a[1], a[a.index("--output") + 1]
nets = [n for n in a[a.index("--nets") + 1:] if not n.startswith("--")]
cfg = json.load(open(os.environ["FAKE_CFG"]))
open(cfg["log"], "a").write(json.dumps({"nets": nets, "rescue": os.environ.get("KICAD_NET_RESCUE"),
    "esc": os.environ.get("KICAD_TERMINAL_ESCALATION"), "ripup": a[a.index("--max-ripup") + 1] if "--max-ripup" in a else None}) + "\n")
if cfg.get("ignore_signals"):
    signal.signal(signal.SIGINT, signal.SIG_IGN); signal.signal(signal.SIGTERM, signal.SIG_IGN)
if any(n in nets for n in cfg.get("hang_always", [])) or \
        (len(nets) > 1 and any(n in nets for n in cfg.get("hang_batch", []))):
    for i, n in enumerate(nets):
        print("[%d/%d] Routing %s (id=%d)" % (i + 1, len(nets), n, i), flush=True)
    time.sleep(1000)
if len(nets) > 1 and any(n in nets for n in cfg.get("nothing_batch", [])):
    sys.exit(0)                                   # exits 0, writes nothing
txt = open(src).read().rstrip()
if cfg.get("corrupt"):
    open(out, "w").write(txt[: len(txt) // 2]); sys.exit(0)
add = ""
for k in cfg.get("rip", {}).get(nets[0], []) if len(nets) == 1 else []:       # a job that rips another net's copper
    txt = "\n".join(l for l in txt.split("\n") if not (l.startswith("(segment") and '(net "%s")' % k in l))
for n in nets:
    i = int(n[1:])
    print("[%d/%d] Routing %s (id=%d)" % (i + 1, len(nets), n, i), flush=True)
    if n in cfg.get("fail", []):
        print("  FAILED: Could not find route", flush=True); continue
    y = 8 + 8 * i
    row = y + 1
    if n in cfg.get("conflict_row", {}) and '(layer "F.Cu") (net "N0"))' not in txt:
        row = cfg["conflict_row"][n]                       # runs beside N0's future track (first round only)
    w = 0.05 if n in cfg.get("thin", []) else 0.15
    mid = [(9.5, row), (20, row), (29.5, row)] if n in cfg.get("snap", []) else [(9.5, row), (29.5, row)]
    pts = [(9.5, y)] + mid + [(29.5, y)]              # detour above the (foreign) second pad of the left part
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        add += '(segment (start %g %g) (end %g %g) (width %g) (layer "F.Cu") (net "%s"))\n' % (x0, y0, x1, y1, w, n)
    if n in cfg.get("snap", []):
        add += '(via (at 20.01 %g) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "%s"))\n' % (row, n)
    if n in cfg.get("via_in_pad", []):
        add += '(via (at 9.5 %g) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "%s"))\n' % (y, n)
    print("  SUCCESS: 1 segments", flush=True)
open(out, "w").write(txt[:-1] + add + ")\n")
'''


def setup(tmp_path, n=8, **cfg):
    body = ""
    for i in range(n):
        body += G.two(f"RA{i}", 10, 8 + 8 * i, f"N{i}", f"P{i}") + G.two(f"RB{i}", 30, 8 + 8 * i, f"N{i}", f"Q{i}")
    board = G.board(tmp_path, body, outline=(0, 0, 100, 100))
    (tmp_path / "fake.py").write_text(FAKE)
    cfg["log"] = str(tmp_path / "calls.jsonl")
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    os.environ["FAKE_CFG"] = str(tmp_path / "cfg.json")
    W = tmp_path / "W"
    W.mkdir()
    extra = {}
    spath = str(W / "routed.kicad_pcb.status.json")
    S = BR.settings({})

    def build(src, dst, only, limited):
        c = [sys.executable, str(tmp_path / "fake.py"), src, "--output", dst]
        if limited:
            c += ["--max-ripup", "3"]
        return c + ["--nets", *only]
    run = BR.make_runner(build, str(tmp_path), extra, spath, S)
    return board, str(W), run, extra, spath


def calls(tmp_path):
    p = tmp_path / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def go(tmp_path, n=8, batch=4, timeout=1.5, **cfg):
    board, W, run, extra, spath = setup(tmp_path, n, **cfg)
    r = BR.route_batches(board, W, [f"N{i}" for i in range(n)], run, batch_nets=batch, batch_timeout=timeout,
                         status_path=spath, extra=extra, log=lambda m: None, leftover_timeout=timeout)
    return board, W, r, spath


def routed(path):
    op = RC.open_by_net(path, method="text")
    return {n for n in (f"N{i}" for i in range(8)) if n not in op}


def test_checkpoint_after_every_batch_and_exact_leftovers(tmp_path):
    board, W, r, spath = go(tmp_path, fail=["N5"])
    cks = r["checkpoints"]
    assert [os.path.basename(c) for c in cks[:2]] == ["ck-001.kicad_pcb", "ck-002.kicad_pcb"] and len(cks) >= 2
    assert os.path.dirname(cks[0]) == W
    # each checkpoint is at least as routed as the one before it, ck-001 holds exactly the first batch
    sets = [routed(c) for c in cks]
    assert sets[0] == {"N0", "N1", "N2", "N3"} and all(a <= b for a, b in zip(sets, sets[1:]))
    assert r["board"] == cks[-1] and routed(r["board"]) == {f"N{i}" for i in range(8)} - {"N5"}
    assert r["leftovers"] == ["N5"] and r["open"] == 1 and r["start_open"] == 8
    assert json.load(open(os.path.join(W, "leftovers.json")))["leftovers"] == ["N5"]
    # the batches ran with the retry phase limited and --max-ripup; the leftovers pass with it on
    cs = calls(tmp_path)
    assert cs[0]["rescue"] == "0" and cs[0]["esc"] == "0" and cs[0]["ripup"] == "3"
    assert cs[-1]["nets"] == ["N5"] and cs[-1]["rescue"] != "0" and cs[-1]["ripup"] is None
    # status file: one file for the whole run, last good checkpoint recorded, state done
    st = RS.load(spath)
    assert st["state"] == "done" and st["mode"] == "batch" and st["last_good"] == r["board"]
    assert st["checkpoint"] == r["board"] and st["checkpoint_n"] == len(cks) and st["leftovers"] == 1
    line = RS.one_line(st)
    assert "last good ck-" in line and "1 leftover" in line


def test_timeout_halves_the_batch_and_loses_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(RS.KILL_WAIT, "INT", 2.0)
    board, W, r, spath = go(tmp_path, hang_batch=["N3"])        # batch [N0..N3] hangs; singles are fine
    assert r["leftovers"] == [] and routed(r["board"]) == {f"N{i}" for i in range(8)}
    assert r["timeouts"] == 2 and r["halvings"] == 2
    ns = [c["nets"] for c in calls(tmp_path)]
    assert ns[:4] == [["N0", "N1", "N2", "N3"], ["N0", "N1"], ["N2", "N3"], ["N2"]]
    sets = [routed(c) for c in r["checkpoints"]]
    assert sets[0] == {"N0", "N1"} and all(a <= b for a, b in zip(sets, sets[1:]))   # nothing lost, ever
    assert RS.load(spath)["state"] == "done"


def test_router_that_writes_nothing_is_halved_and_a_hanging_net_is_a_leftover(tmp_path, monkeypatch):
    monkeypatch.setitem(RS.KILL_WAIT, "INT", 2.0)
    board, W, r, spath = go(tmp_path, nothing_batch=["N5"], hang_always=["N6"])
    assert r["leftovers"] == ["N6"] and r["reasons"] == {"N6": "timeout"}
    assert routed(r["board"]) == {f"N{i}" for i in range(8)} - {"N6"}
    ns = [c["nets"] for c in calls(tmp_path)]
    assert ["N4", "N5", "N6", "N7"] in ns and ["N4", "N5"] in ns and ["N6", "N7"] in ns and ["N6"] in ns
    assert r["halvings"] >= 2 and len(r["checkpoints"]) >= 4


def test_never_worse_than_the_input(tmp_path):
    board, W, r, spath = go(tmp_path, corrupt=True)              # every call writes a truncated board
    assert r["board"] == board and r["checkpoints"] == [] and r["open"] == r["start_open"] == 8
    assert r["leftovers"] == [f"N{i}" for i in range(8)]
    assert not [f for f in os.listdir(W) if f.startswith("ck-")]


def test_total_budget_leaves_the_rest_as_leftovers(tmp_path):
    board, W, run, extra, spath = setup(tmp_path, 8)
    r = BR.route_batches(board, W, [f"N{i}" for i in range(8)], run, batch_nets=4, total_budget=0.0,
                         status_path=spath, extra=extra, log=lambda m: None)
    assert r["board"] == board and len(r["leftovers"]) == 8 and set(r["reasons"].values()) == {"budget"}
    assert calls(tmp_path) == []


def test_kill_ladder_reaches_sigkill(tmp_path, monkeypatch):
    monkeypatch.setitem(RS.KILL_WAIT, "INT", 0.3)
    monkeypatch.setitem(RS.KILL_WAIT, "TERM", 0.3)
    board, W, run, extra, spath = setup(tmp_path, 2, hang_always=["N0"], ignore_signals=True)
    t = time.time()
    res = run(board, os.path.join(W, "x.kicad_pcb"), ["N0"], 0.5)
    assert res.timed_out and time.time() - t < 5 and not os.path.exists(os.path.join(W, "x.kicad_pcb"))


def test_order_is_fanout_power_critical_then_local_first(tmp_path):
    body = ""
    for n, (x, ln) in {"LONG": (0, 40), "SHORT": (0, 5), "MID": (0, 20), "FAN": (0, 60), "RAIL": (0, 3), "RFN": (0, 50)}.items():
        y = 10 + 8 * len(body)
        body += G.two(f"A{n}", 10, y, n, f"X{n}") + G.two(f"B{n}", 10 + ln, y, n, f"Y{n}")
    B = RD.load(G.board(tmp_path, body, outline=(0, 0, 120, 120)))
    cls = {"LONG": {"class": "signal"}, "SHORT": {"class": "signal"}, "MID": {"class": "signal"},
           "FAN": {"class": "signal"}, "RAIL": {"class": "rail"}, "RFN": {"class": "rf"}}
    got = BR.order_nets(B, list(cls), cls, first=["FAN"])
    assert got == ["FAN", "RAIL", "RFN", "SHORT", "MID", "LONG"]


def test_settings_defaults_and_overrides():
    s = BR.settings({})
    assert s["batch_nets"] == 35 and s["batch_timeout_s"] == 240 and s["env"]["KICAD_NET_RESCUE"] == "0"
    s = BR.settings({"route": {"batch_nets": 10, "batch_timeout_s": 60, "batch_env": {"KICAD_X": 1}}})
    assert s["batch_nets"] == 10 and s["batch_timeout_s"] == 60 and s["env"]["KICAD_X"] == "1"
