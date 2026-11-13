"""Whole-board Freerouting stage (routefr.py) against a FAKE Freerouting: a script standing in for `java` that copies the
DSN it was given, prints Freerouting-style log lines and writes a canned session.  No JVM, no jar."""
import argparse
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as G
from fluxplace import pipeline as PL
from fluxplace import routeconn as RC
from fluxplace import routefr as FR
from fluxplace import routeladder as RLD

HAVE = os.path.exists("/usr/bin/python3") and subprocess.run(
    ["/usr/bin/python3", "-c", "import pcbnew"], capture_output=True).returncode == 0
need_pcbnew = pytest.mark.skipif(not HAVE, reason="pcbnew not available")

FAKE_JAVA = '''#!%s
import json, os, shutil, sys, time
a = sys.argv
cfg = json.load(open(os.environ["FAKE_FREEROUTING"]))
dsn, ses = a[a.index("-de") + 1], a[a.index("-do") + 1]
shutil.copy(dsn, cfg["dsn_copy"])
json.dump(a, open(cfg["argv_out"], "w"))
print("2026-01-01 10:00:00.000 INFO   Freerouting v2.5.0 (build-date: 2026-01-01)", flush=True)
print("2026-01-01 10:00:01.000 INFO   [J] Auto-routing stage started on board 'x' with baseline score 1.0 for 3 unrouted items.", flush=True)
print("2026-01-01 10:01:01.000 INFO   [J] Auto-routing pass #1 on board 'x' was completed in 60.00 seconds with score 5.00 (2 unrouted and 0 violations), using 1 CPU seconds", flush=True)
print("2026-01-01 10:03:01.000 INFO   [J] Auto-routing pass #2 on board 'x' was completed in 120.00 seconds with score 6.00 (1 unrouted and 0 violations), using 1 CPU seconds", flush=True)
if cfg.get("ses"):
    open(ses, "w").write(cfg["ses"])
    print("2026-01-01 10:03:02.000 INFO   [J] Job 'J' finished with state: TIMED_OUT (elapsed)", flush=True)
    print("2026-01-01 10:03:02.000 INFO   Successfully saved output file", flush=True)
if cfg.get("hang"):
    time.sleep(600)
''' % sys.executable


def ses_text(wires):
    """wires: [(net, layer, width_mm, [(x, y) mm ...])] -> a Specctra session (unit: 0.1 um, y negated)."""
    out = "(session s\n (routes\n  (resolution um 10)\n  (network_out\n"
    for net, layer, w, pts in wires:
        xy = " ".join(f"{round(x * 10000)} {round(-y * 10000)}" for x, y in pts)
        out += f'   (net "{net}"\n    (wire\n     (path {layer} {round(w * 10000)} {xy})\n    )\n   )\n'
    return out + "  )\n )\n)\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """-> (loc, set_cfg): a located fake Freerouting; set_cfg(ses=..., hang=...) writes what the next run does."""
    monkeypatch.setattr(RC, "PY", "/usr/bin/python3")
    java = tmp_path / "fakejava"
    java.write_text(FAKE_JAVA)
    java.chmod(0o755)
    jar = tmp_path / "freerouting-2.5.0.jar"
    jar.write_text("")
    cfgp = tmp_path / "fake.json"
    monkeypatch.setenv("FAKE_FREEROUTING", str(cfgp))
    base = {"dsn_copy": str(tmp_path / "seen.dsn"), "argv_out": str(tmp_path / "argv.json")}

    def set_cfg(**kw):
        cfgp.write_text(json.dumps(dict(base, **kw)))
        return base
    set_cfg()
    return FR.locate(str(jar), str(java)), set_cfg


H = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) %s(2 "B.Cu" signal))\n(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))\n'


def write(tmp_path, body, inner=True):
    """a board pcbnew loads: 4 copper layers (In1 plane, In2 signal) or 2."""
    p = tmp_path / "b.kicad_pcb"
    p.write_text(H % ('(4 "In1.Cu" signal) (6 "In2.Cu" signal) ' if inner else "") + body + ")\n")
    return str(p)


def seg(a, b, net, w=0.2, layer="F.Cu", locked=False):
    lk = " (locked yes)" if locked else ""
    return f'(segment{lk} (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}"))\n'


def zone(net, layer, x0=1, y0=1, x1=99, y1=79):
    return (f'(zone (net "{net}") (layers "{layer}") (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.2) '
            f'(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3)) '
            f'(polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))\n')


def board(tmp_path):
    """N1, N2: one open connection each; LK: complete and locked; a GND plane on In1.Cu and a GND pour on F.Cu."""
    body = (G.two("R1", 10, 10, "N1", "GND") + G.two("R2", 30, 10, "N1", "") +
            G.two("R3", 10, 20, "", "N2") + G.two("R4", 30, 20, "N2", "") +
            G.two("R5", 10, 30, "LK", "") + G.two("R6", 30, 30, "LK", "") +
            seg((9.5, 30), (29.5, 30), "LK", locked=True) +
            seg((12, 20.3), (28, 20.3), "FOR") + seg((12, 19.7), (28, 19.7), "FOR") +   # N2's corridor: 0.3 mm to each side
            zone("GND", "In1.Cu") + zone("GND", "F.Cu", 40, 40, 60, 60))
    return write(tmp_path, body)


def run(tmp_path, fake, wires, floor=0.127, clr=0.127, **kw):
    loc, set_cfg = fake
    base = set_cfg(ses=ses_text(wires) if wires is not None else None, **{k: kw.pop(k) for k in ("hang",) if k in kw})
    b = board(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    S = FR.settings(None, threads=1, **{k: kw.pop(k) for k in ("grace_s",) if k in kw})
    rec = FR.route_board(b, out, str(tmp_path / "fr"), loc=loc, planes=["In1.Cu"], floor_track=floor, floor_clearance=clr,
                         budget_s=kw.pop("budget_s", 20), S=S, conn=lambda p: RC.open_by_net(p, fill=True), poll=0.2, **kw)
    return rec, b, out, base


N1 = ("N1", "F.Cu", 0.09, [(9.5, 10), (9.5, 12), (29.5, 12), (29.5, 10)])
N2 = ("N2", "F.Cu", 0.09, [(10.5, 20), (29.5, 20)])


# ------------------------------------------------------------------ pure text
def test_fix_dsn_declares_planes_raises_rules_and_fixes_wiring():
    dsn = ('(pcb x\n  (resolution um 10)\n  (unit um)\n  (structure\n    (layer F.Cu\n      (type signal)\n    )\n'
           '    (layer In1.Cu\n      (type signal)\n    )\n    (rule\n      (width 90)\n      (clearance 100)\n'
           '      (clearance 31.75 (type smd_smd))\n    )\n  )\n  (network\n    (class c A\n      (rule\n        (width 200)\n        (clearance 127)\n      )\n    )\n  )\n'
           '  (wiring\n    (wire (path F.Cu 90  1 1  2 2)(net A)(type route))\n    (wire (path F.Cu 150  1 1  2 2)(net A)(type fix))\n'
           '    (via "V" 1 1 (net A)(type route))\n  )\n)\n')
    out, rep = FR.fix_dsn(dsn, ["In1.Cu", "In9.Cu"], 0.127, 0.127)
    assert rep["planes"] == ["In1.Cu"] and rep["planes_missed"] == ["In9.Cu"]
    assert "(layer In1.Cu\n      (type power)" in out and "(layer F.Cu\n      (type signal)" in out
    assert rep["widths_raised"] == 1 and "(width 127)" in out and "(width 200)" in out
    assert rep["clearances_raised"] == 1 and "(clearance 31.75 (type smd_smd))" in out      # typed clearances are KiCad's own
    assert rep["fixed"] == 3 and "(type route)" not in out
    assert "(path F.Cu 90 " in out                                                            # existing copper is not rewritten
    dsn2 = dsn.replace("(network\n", '(network\n    (net A\n      (pins R1-1 R2-1)\n    )\n    (net "B 1"\n      (pins R1-2 R2-2)\n    )\n')
    out2, rep2 = FR.fix_dsn(dsn2, [], 0, 0, skip_nets=["B 1", "ZZ"])
    assert rep2["nets_excluded"] == 1 and "(pins R1-1 R2-1)" in out2 and "R1-2" not in out2   # the excluded net has nothing to connect


def test_log_parse_gives_the_pass_curve():
    log = ("2026-01-01 10:00:00.000 INFO   Freerouting v2.5.0 (build-date: x)\n"
           "2026-01-01 10:00:30.000 INFO   [J] Fanout pass #1 on board 'b' completed in 20.0 seconds with 17 SMD pins fanouted, 3 not routed, 0 insert errors\n"
           "2026-01-01 10:01:00.000 INFO   [J] Auto-routing stage started on board 'b' with baseline score 17.64 for 1029 unrouted items.\n"
           + "".join(f"2026-01-01 10:{2 * i:02d}:00.000 INFO   [J] Auto-routing pass #{i} on board 'b' was completed in 1.5 seconds with score 4.2 "
                     f"({500 - 40 * i} unrouted and {i} violations), using 1 CPU seconds\n" for i in range(1, 11))
           + "2026-01-01 10:21:00.000 INFO   [J] Job 'J' finished with state: TIMED_OUT (elapsed: 1)\n")
    p = FR.parse_log(log)
    assert p["version"] == "2.5.0" and p["start_unrouted"] == 1029 and p["state"] == "TIMED_OUT"
    assert p["fanout"] == [{"pass": 1, "min": 0.5, "done": 17, "left": 3}]
    assert p["passes"][0] == {"pass": 1, "min": 2.0, "unrouted": 460, "violations": 1, "score": 4.2} and len(p["passes"]) == 10
    c = FR.curve(p["passes"], 5)
    assert len(c) == 5 and c[0] == [1, 2.0, 460, 1] and c[-1] == [10, 20.0, 100, 10]
    assert FR.hms(1500) == "00:25:00"


def test_flags_fanout_switch_and_neckdown():
    S = FR.settings({"route": {"freerouting": {"threads": 6, "passes": 40}}})
    off = FR.router_flags(S, 1500, False)
    assert "--router.fanout.enabled=false" in off and "--router.automatic_neckdown=false" in off
    assert "--router.job_timeout=00:25:00" in off and "--router.autorouter.max_passes=40" in off and off[off.index("-mt") + 1] == "6"
    assert "--router.fanout.enabled=false" not in FR.router_flags(S, 60, True)
    with pytest.raises(ValueError, match="auto, on or off"):
        FR.settings({"route": {"freerouting": {"fanout": "maybe"}}})


# ------------------------------------------------------------------ locate / select
def test_locate_order_version_check_and_loud_failure(tmp_path, monkeypatch):
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    java.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    for v in ("2.3.0", "2.5.0", "2.6.1"):
        (tools / f"freerouting-{v}.jar").write_text("")
    monkeypatch.setattr(FR, "JAR_GLOBS", (str(tools / "freerouting*.jar"),))
    monkeypatch.setattr(FR, "JAVA_GLOBS", ())
    env = {FR.ENV_JAVA: str(java)}
    assert FR.locate(env=env)["jar"].endswith("freerouting-2.6.1.jar")                       # discovery: newest supported
    e2 = dict(env, **{FR.ENV_JAR: str(tools / "freerouting-2.5.0.jar")})
    assert FR.locate(env=e2)["version"] == "2.5.0" and FR.locate(env=e2)["source"].startswith("env")
    with pytest.raises(RuntimeError, match="2.3.0 .* too old: 2.5.0 or newer"):               # an explicit old jar: clear refusal
        FR.locate(str(tools / "freerouting-2.3.0.jar"), env=e2)
    with pytest.raises(RuntimeError, match="jar not found"):
        FR.locate(str(tmp_path / "nope.jar"), env=env)
    for v in ("2.5.0", "2.6.1"):
        (tools / f"freerouting-{v}.jar").unlink()
    with pytest.raises(RuntimeError, match="too old"):                                        # only an old jar installed
        FR.locate(env=env)
    name, loc, note = FR.choose("auto", env=env)
    assert (name, loc) == ("krt", None) and "auto -> krt" in note                             # auto never fails, it says why
    assert FR.choose("krt", env=env)[0] == "krt"
    with pytest.raises(RuntimeError):
        FR.choose("freerouting", env=env)
    (tools / "freerouting-2.5.0.jar").write_text("")
    assert FR.choose("auto", env=env)[0] == "freerouting"
    with pytest.raises(RuntimeError, match="Java runtime .* not found"):
        FR.locate(env={FR.ENV_JAVA: str(tmp_path / "nojava")})


def test_ladder_clock_guarantees_the_minimum_slice():
    t = [1000.0]
    left = RLD.clock(600, 0.0, 90, now=lambda: t[0])       # the rounds overran: 1000 s into a 600 s budget
    assert left() == 90
    t[0] += 60
    assert left() == 30
    t[0] += 60
    assert left() == 0
    t[0] = 100.0
    assert RLD.clock(600, 0.0, 90, now=lambda: t[0])() == 500   # budget left wins when it is more


# ------------------------------------------------------------------ the run (pcbnew + fake Freerouting)
@need_pcbnew
def test_run_planes_pours_fixed_copper_and_subfloor_repair(tmp_path, fake):
    rec, b, out, base = run(tmp_path, fake, [N1])
    assert rec["ok"], rec.get("error")
    dsn = open(base["dsn_copy"]).read()
    assert "(layer In1.Cu\n      (type power)" in dsn and "(layer F.Cu\n      (type signal)" in dsn     # plane declared
    assert rec["pours"] == {"zones_removed": 1, "zones_kept": 1, "zones_trimmed": 0}                    # F.Cu pour not in the DSN
    assert "(plane GND (polygon F.Cu" not in dsn.replace("\n", " ").replace("  ", " ") and "In1.Cu" in dsn.split("(network")[0]
    wiring = dsn[dsn.index("(wiring"):]
    assert wiring.count("(type fix)") == 3 and "(type route)" not in wiring                             # locked AND unlocked copper fixed
    txt = open(out).read()
    assert txt.count("(zone") == 2                                                                      # the board kept its pours
    assert rec["locked_same"] and rec["before"]["locked"] == 1 == rec["after"]["locked"]
    assert rec["import"]["tracks_added"] == 3 and rec["plane_tracks_added"] == 0
    assert rec["subfloor"]["subfloor"] == 3 and rec["subfloor"]["fixed"] == 3 and not rec["subfloor"]["failed"]
    assert rec["after"]["subfloor"] == 0 and rec["after"]["min_width"] >= 0.127
    assert rec["open"] == 1 and rec["open_cli"] is not None                                             # N1 closed, N2 open
    assert [p["unrouted"] for p in rec["passes"]] == [2, 1] and rec["state"] == "TIMED_OUT"
    argv = json.load(open(base["argv_out"]))
    assert "--router.fanout.enabled=false" not in argv                                                  # auto: the board has no fan-out
    assert any(x.startswith("-Djava.io.tmpdir=" + str(tmp_path / "fr")) for x in argv)                  # its own tmp dir
    assert "sub-floor 3: 3 widened" in FR.summary(rec)


@need_pcbnew
def test_subfloor_that_cannot_be_widened_is_listed(tmp_path, fake):
    rec, _b, _out, _ = run(tmp_path, fake, [N1, N2], floor=0.2, clr=0.15)     # N2 sits 0.3 mm from foreign tracks on both sides
    assert rec["ok"]
    sf = rec["subfloor"]
    assert sf["fixed"] == 3 and len(sf["failed"]) == 1
    bad = sf["failed"][0]
    assert bad["net"] == "N2" and bad["from"] == 0.09 and "FOR" in bad["blocked_by"]
    assert rec["after"]["subfloor"] == 1 and "1 NOT fixable" in FR.summary(rec)


@need_pcbnew
def test_skip_nets_are_not_imported_and_fanout_switch(tmp_path, fake):
    rec, _b, out, base = run(tmp_path, fake, [N1, N2], skip_nets=["N2"], has_fanout=True)
    assert rec["ok"] and rec["import"]["tracks_added"] == 3 and rec["import"]["skipped_items"] == 1
    assert rec["open"] == 1 and rec["fanout"] == "off"
    seen = open(base["dsn_copy"]).read()
    assert rec["dsn"]["nets_excluded"] == 1                                                # N2 has no pins in the DSN
    assert "R3-2" not in seen[seen.index("(network"):seen.index("(wiring")].split("(class")[0]
    assert "--router.fanout.enabled=false" in json.load(open(base["argv_out"]))


@need_pcbnew
def test_watchdog_kills_a_hung_jvm_and_keeps_the_session(tmp_path, fake):
    rec, _b, out, _ = run(tmp_path, fake, [N1], hang=True, grace_s=1.0)
    assert rec["ok"] and rec["killed"] and rec["hung_after_save"] and rec["open"] == 1
    assert os.path.exists(out)


@need_pcbnew
def test_timeout_without_a_session_fails_loudly(tmp_path, fake):
    rec, _b, out, _ = run(tmp_path, fake, None, hang=True, grace_s=1.0, budget_s=1)
    assert not rec["ok"] and rec["killed"] and "no usable session" in rec["error"] and "killed by the watchdog" in rec["error"]
    assert not os.path.exists(out) and "FAILED" in FR.summary(rec)


# ------------------------------------------------------------------ pipeline: router selection, record, hybrid finish
KRT = '''
import sys
a = sys.argv
src, out = a[1], a[a.index("--output") + 1]
nets = [n for n in a[a.index("--nets") + 1:] if not n.startswith("--")]
txt = open(src).read().rstrip()
add = ""
for i, n in enumerate(nets):
    print("[%d/%d] Routing %s (id=%d)" % (i + 1, len(nets), n, i), flush=True)
    if n == "B":
        add += '(segment (start 18 20.635) (end 12 20.635) (width 0.2) (layer "F.Cu") (net "B"))\\n(segment (start 12 20.635) (end 12 52) (width 0.2) (layer "F.Cu") (net "B"))\\n'
        add += '(segment (start 12 52) (end 52 52) (width 0.2) (layer "F.Cu") (net "B"))\\n(segment (start 52 52) (end 52 40.635) (width 0.2) (layer "F.Cu") (net "B"))\\n'
        add += '(segment (start 52 40.635) (end 58 40.635) (width 0.2) (layer "F.Cu") (net "B"))\\n'
        print("  SUCCESS: 5 segments", flush=True)
    else:
        print("  FAILED: Could not find route (1.0s)", flush=True)
open(out, "w").write(txt[:-1] + add + ")\\n")
'''


def _pipe_args(tmp_path, b, fake_loc, **kw):
    router = tmp_path / "krt"
    (router / "py_router").mkdir(parents=True, exist_ok=True)
    (router / "py_router" / "route.py").write_text(KRT)
    d = dict(board=b, workdir=str(tmp_path / "W"), constraints=None, spec=None, from_stage="route", to_stage="route", status=False,
             waive=[], force=True, api=False, route_budget=120, route_timeout=30, stall_rounds=0, replace_on_stall=False,
             layers=["F.Cu", "B.Cu"], track=0.2, clearance=0.2, router_py=sys.executable, router_dir=str(router),
             answer=["rule-floors=standard"], router="freerouting", freerouting_jar=fake_loc["jar"], java=fake_loc["java"],
             freerouting_threads=1)
    d.update(kw)
    return argparse.Namespace(**d)


@need_pcbnew
def test_pipeline_freerouting_then_per_net_finish_and_stage_record(tmp_path, fake):
    loc, set_cfg = fake
    body = (G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) + G.ic("U2", 60, 40, ["A", "B", "C", "E"], pitch=1.27))
    b = write(tmp_path, body, inner=False)
    set_cfg(ses=ses_text([("A", "F.Cu", 0.2, [(18, 19.365), (10, 19.365), (10, 50), (50, 50), (50, 39.365), (58, 39.365)])]))
    a = _pipe_args(tmp_path, b, loc)
    assert PL.run(a) == 1                                   # C stays open: fail, with numbers
    raw = open(PL.state_path(a.workdir)).read()
    assert len(raw) <= PL.STATE_MAX
    r = json.loads(raw)["stages"]["route"]
    assert r["router"]["name"] == "freerouting" and r["router"]["dir"] == str(tmp_path / "krt")
    fr = r["fr"]
    assert fr["jar"] == loc["jar"] and fr["v"] == "2.5.0" and "--router.job_timeout=" in fr["flags"]
    assert fr["curve"] == [[1, 1.0, 2, 0], [2, 3.0, 1, 0]] and fr["open"][:2] == [3, 2] and fr["locked_same"] is True
    res = json.load(open(os.path.join(a.workdir, "1-route" if os.path.isdir(os.path.join(a.workdir, "1-route")) else "6-route", "result.json")))
    assert res["router"] == "freerouting" and res["open_start"] == 3 and res["open_end"] == 1   # A by Freerouting, B by the per-net router
    assert res["times"]["ladder_min"] > 0 and "freerouting 2.5.0 2 pass(es)" in r["line"] or "freerouting" in res["line"]
    # resumable: the same range again on the same workdir
    assert PL.run(a) == 1


@need_pcbnew
def test_pipeline_router_selection(tmp_path, fake, monkeypatch):
    loc, set_cfg = fake
    b = write(tmp_path, G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) + G.ic("U2", 60, 40, ["A", "B", "C", "E"], pitch=1.27), inner=False)
    monkeypatch.setattr(FR, "JAR_GLOBS", ())
    a = _pipe_args(tmp_path, b, loc, router="auto", freerouting_jar=None, java=None)      # nothing installed: auto -> krt
    PL.run(a)
    r = PL.load_state(a.workdir)["stages"]["route"]
    assert r["router"]["name"] == "krt" and "fr" not in r
    a = _pipe_args(tmp_path, b, loc, router="freerouting", freerouting_jar=str(tmp_path / "freerouting-2.3.0.jar"))
    (tmp_path / "freerouting-2.3.0.jar").write_text("")
    with pytest.raises(SystemExit, match="too old"):                                       # asked for by name: loud
        PL.run(a)
    cons = tmp_path / "c.toml"
    cons.write_text('[route]\nrouter = "freerouting"\n')
    a = _pipe_args(tmp_path, b, loc, router=None, constraints=str(cons), freerouting_jar=str(tmp_path / "nope.jar"))
    with pytest.raises(SystemExit, match="jar not found"):                                 # the [route] router key selects too
        PL.run(a)
