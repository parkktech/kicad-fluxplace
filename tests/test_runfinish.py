"""run-finish.sh (T1a item 10): a TERM to the recorded PID leaves a `killed` summary and no child."""
import os, subprocess, time
SH = "/var/kicad/utv-comms-bridge/docs/V2.0/tracefinish/run-finish.sh"


def test_term_kills_tree_and_logs_summary(tmp_path):
    (tmp_path / "cli.py").write_text("import time\ntime.sleep(120)\n")
    (tmp_path / "in.kicad_pcb").write_text("(kicad_pcb)\n")
    (tmp_path / "c.toml").write_text("[fab]\n")
    (tmp_path / "s.json").write_text("{}\n")
    env = dict(os.environ, R=str(tmp_path), CODE=str(tmp_path), OUTBASE=str(tmp_path / "F"), CONS=str(tmp_path / "c.toml"),
               SPEC=str(tmp_path / "s.json"))
    p = subprocess.Popen(["setsid", "bash", SH, str(tmp_path / "in.kicad_pcb")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pidf = tmp_path / "F" / "1" / "run.pid"
    for _ in range(100):
        if pidf.exists() and (tmp_path / "F" / "1" / "child.pid").exists():
            break
        time.sleep(0.1)
    child = int((tmp_path / "F" / "1" / "child.pid").read_text())
    pid = int(pidf.read_text())
    os.kill(pid, 15)
    p.wait(timeout=10)
    time.sleep(0.5)
    run = (tmp_path / "F" / "1" / "RUN.md").read_text()
    assert "finish killed" in run and "rc=143" in run
    assert not pidf.exists()
    leftovers = subprocess.run(["pgrep", "-f", str(tmp_path) + "/cli.py"], capture_output=True, text=True).stdout.split()
    assert leftovers == []
    try:
        os.kill(child, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive
