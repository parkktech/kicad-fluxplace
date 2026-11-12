"""No-poll routing: log parser, status file, route-status / route-wait."""
import json, os, sys, threading, time
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import routestatus as RS
import cli

KRT = """\
Routing 201 single-ended net(s)...
[1/201] Routing I2C_SDA (id=8)
  Detected multi-point net with 6 pads
  SUCCESS: 23 segments, 2 vias, 25771 iterations, length=29.70mm (0.09s)
[2/201] Routing IMU_INT1 (id=176)
  FAILED: Could not find route (58.40s)
[3/201 (1 failed)] Routing I2C_SCL (id=156)
    [1/0] Routing MST edge: pad 1 -> pad 0 (length=18.80mm)
  RETRY SUCCESS (N=1): 26 segments, 2 vias, length=23.02mm
[4/201 (1 failed) (5 ripped)] Routing ACC_IN (id=90)
  RETRY FAILED (N=1)
[REROUTE 5/9 (2 failed)] Re-routing ripped net CAN2_CS
  REROUTE SUCCESS: 4 segments
"""


def _parse(text):
    p = RS.LogParser()
    for l in text.splitlines():
        p.feed(l)
    return p


def test_parser_krt_counters():
    s = _parse(KRT).snapshot()
    assert s["nets_total"] == 9 and s["nets_done"] == 5 and s["phase"] == "reroute"
    assert s["nets_ripped"] == 5 and s["current"] == "CAN2_CS"
    p = _parse(KRT)
    assert p.net_status["I2C_SDA"] == "SUCCESS" and p.net_status["IMU_INT1"] == "FAILED"
    assert p.net_status["I2C_SCL"] == "SUCCESS" and p.net_status["ACC_IN"] == "FAILED"
    assert p.net_status["CAN2_CS"] == "SUCCESS"            # REROUTE SUCCESS counts
    assert s["nets_failed"] == 2


def test_parser_agrees_with_routediag(tmp_path):
    from fluxplace import routediag as RD
    f = tmp_path / "r.log"; f.write_text(KRT)
    assert RS.parse_log_file(str(f)).net_status == RD.parse_logs([str(f)])


def test_parser_freerouting():
    p = _parse("Starting routing pass #3\nAuto-router pass #3 done: 45 unrouted items\n")
    assert p.snapshot()["freerouting"] == {"pass": 3, "unrouted": 45}


def test_run_logged_writes_status_and_waits(tmp_path):
    script = tmp_path / "fake.py"
    lines = KRT.splitlines()
    script.write_text("import time,sys\nfor l in %r:\n    print(l, flush=True); time.sleep(0.15)\n"
                      "open(sys.argv[1],'w').write('x')\n" % lines)
    out = str(tmp_path / "out.kicad_pcb")
    res = {}
    t = threading.Thread(target=lambda: res.setdefault(
        "r", RS.run_logged([sys.executable, str(script), out], out=out, every=0.3)))
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(out + ".status.json") and time.time() < deadline:
        time.sleep(0.05)
    sp = RS.resolve(out)
    assert sp == out + ".status.json"
    st = RS.load(sp)
    assert st["state"] in ("running", "done") and st["pid"]
    reason, st = RS.wait(sp, 20, poll=0.1)
    t.join()
    assert reason == "done" and st["nets_total"] == 9 and st["rc"] == 0
    assert st["checkpoint"] == out
    assert res["r"].returncode == 0 and "RETRY FAILED" in res["r"].stdout
    assert "reroute" in RS.one_line(st) and "\n" not in RS.one_line(st)


def test_timeout_kills_and_raises(tmp_path):
    out = str(tmp_path / "o.kicad_pcb")
    with pytest.raises(__import__("subprocess").TimeoutExpired):
        RS.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], out=out,
                      timeout=1, every=0.2)
    assert RS.load(out + ".status.json")["state"] == "timeout"


def test_wait_stall_and_cli_exit_codes(tmp_path, capsys):
    sp = tmp_path / "x.status.json"
    sp.write_text(json.dumps({"state": "running", "pid": os.getpid(), "stalled_s": 700,
                              "stall_limit_s": 600, "elapsed_s": 900, "nets_total": 10,
                              "nets_done": 4, "nets_failed": 1, "out": str(tmp_path / "none.kicad_pcb")}))
    reason, _ = RS.wait(str(sp), 5, until="stall", poll=0.1)
    assert reason == "stall"
    with pytest.raises(SystemExit) as e:
        cli.main(["route-wait", str(sp), "--timeout", "2", "--until", "stall", "--no-diagnose"])
    assert e.value.code == 0
    assert capsys.readouterr().out.count("\n") == 1
    with pytest.raises(SystemExit) as e:
        cli.main(["route-wait", str(sp), "--timeout", "1", "--poll", "0.2", "--no-diagnose"])
    assert e.value.code == 2                                  # still running at the timeout
    with pytest.raises(SystemExit) as e:
        cli.main(["route-status", str(tmp_path / "nothing.kicad_pcb")])
    assert e.value.code == 3


def test_dead_writer_is_reported(tmp_path):
    sp = tmp_path / "d.status.json"
    sp.write_text(json.dumps({"state": "running", "pid": 2 ** 22 - 3}))
    assert RS.load(str(sp))["state"] == "dead"
