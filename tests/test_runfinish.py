"""run-finish.sh (T1a item 10): a TERM to the recorded PID leaves a `killed` summary and no child."""
import os, subprocess, time
SH = "/var/kicad/utv-comms-bridge/docs/V2.0/tracefinish/run-finish.sh"


def test_term_kills_tree_and_logs_summary(tmp_path):
    (tmp_path / "cli.py").write_text("import time\ntime.sleep(120)\n")
    (tmp_path / "fluxplace").mkdir()
    (tmp_path / "fluxplace" / "__init__.py").write_text("")
    (tmp_path / "fluxplace" / "pipeline.py").write_text('CONSTRUCTIVE = ["a", "trace-finish"]\n')
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


def _run_fin(tmp_path, code_dir, cli_src):
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "cli.py").write_text(cli_src)
    (tmp_path / "in.kicad_pcb").write_text("(kicad_pcb)\n")
    (tmp_path / "c.toml").write_text("[fab]\n")
    (tmp_path / "s.json").write_text("{}\n")
    env = dict(os.environ, R=str(tmp_path), CODE=str(code_dir), OUTBASE=str(tmp_path / "F"), CONS=str(tmp_path / "c.toml"),
               SPEC=str(tmp_path / "s.json"), CWDOUT=str(tmp_path / "cwd.txt"), LIMIT="60")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    rc = subprocess.run(["bash", SH, str(tmp_path / "in.kicad_pcb")], env=env, cwd=str(elsewhere), timeout=120,
                        capture_output=True).returncode
    return rc, (tmp_path / "F" / "1" / "RUN.md").read_text()


def test_seed_failure_logs_and_exits_2(tmp_path):
    rc, run = _run_fin(tmp_path, tmp_path / "no_such_code", "open('ran.txt','w').write('x')\n")
    assert rc == 2 and "seed failed" in run
    assert not (tmp_path / "ran.txt").exists()                      # the pipeline never starts


def test_seed_ok_logs_gate_line_and_runs_from_repo_root(tmp_path):
    code = tmp_path / "code"
    (code / "fluxplace").mkdir(parents=True)
    (code / "fluxplace" / "__init__.py").write_text("")
    (code / "fluxplace" / "pipeline.py").write_text('CONSTRUCTIVE = ["a", "trace-finish"]\n')
    rc, run = _run_fin(tmp_path, code, "import os\nopen(os.environ['CWDOUT'], 'w').write(os.getcwd())\n")
    assert "gates seeded clean: tool acceptance, input is not a clean board" in run
    assert open(tmp_path / "cwd.txt").read() == str(tmp_path)


def test_code_default_is_mainline_checkout():
    src = open(SH).read()
    assert 'CODE=${CODE:-/var/www/html/kicad-fluxplace}' in src
    assert not [l for l in src.splitlines() if l.startswith("$PY - ") and "2>/dev/null" in l]
