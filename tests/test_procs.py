import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from fluxplace import procs

GC = textwrap.dedent("""
    import subprocess, sys, time
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    print(g.pid, flush=True)
    time.sleep(300)
""")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                  # a zombie is dead
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _gone(pid, t=10):
    end = time.time() + t
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_run_timeout_kills_grandchild(monkeypatch):
    monkeypatch.setattr(procs, "GRACE_S", 1.0)
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        procs.run([sys.executable, "-u", "-c", GC], capture_output=True, text=True, timeout=2)
    gpid = int((ei.value.output or "").split()[0])
    assert _gone(gpid)


def test_run_returns_completed_process():
    r = procs.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout.strip() == "hi"
    with pytest.raises(subprocess.CalledProcessError):
        procs.run([sys.executable, "-c", "raise SystemExit(3)"], check=True)


def test_reap_all_kills_registered_groups():
    p = procs.popen([sys.executable, "-u", "-c", GC], stdout=subprocess.PIPE, text=True)
    gpid = int(p.stdout.readline())
    assert _alive(gpid)
    procs.reap_all(grace=1.0)
    assert _gone(gpid) and p.poll() is not None


def test_sigterm_with_cli_install_kills_grandchild(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); from fluxplace import procs; procs.install(); "
            "procs.run([sys.executable, '-u', '-c', %r], timeout=300)" % (os.path.dirname(os.path.dirname(procs.__file__)), GC))
    out = tmp_path / "o"
    with open(out, "w") as fh:
        # the grandchild pid goes to the inner stdout, which procs.run leaves inherited
        p = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=fh)
        end = time.time() + 15
        while time.time() < end and not out.read_text().strip():
            time.sleep(0.1)
        gpid = int(out.read_text().split()[0])
        assert _alive(gpid)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=20) == 128 + signal.SIGTERM
    assert _gone(gpid)
