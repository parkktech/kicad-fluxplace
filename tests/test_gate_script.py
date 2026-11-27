"""tools/merge-gate.sh: a killed gate must free its slot (no orphan suite holding the flock)."""
import os, signal, subprocess, sys, time, fcntl
import pytest

GATE = os.path.join(os.path.dirname(__file__), "..", "tools", "merge-gate.sh")


def _free(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def test_killed_gate_releases_its_slot(tmp_path):
    wt = tmp_path / "wt"; (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(120)\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(wt)] + c, check=True, capture_output=True)
    lock = tmp_path / "lock"; (tmp_path / "base").write_text("")
    env = dict(os.environ, GATE_SLOTS="1", GATE_LOCKDIR=str(lock), GATE_WORKERS="0", PY=sys.executable)
    p = subprocess.Popen(["bash", GATE, str(wt), str(tmp_path / "base")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    slot = str(lock / "slot1")
    t = time.time()
    while time.time() - t < 30 and (not os.path.exists(slot) or _free(slot) or not _suite_running(wt)):
        time.sleep(0.3)
    assert not _free(slot), "gate never took the slot"
    p.send_signal(signal.SIGTERM); p.wait(timeout=20)
    t = time.time()
    while time.time() - t < 10 and not _free(slot):
        time.sleep(0.3)
    assert _free(slot), "slot still held after the gate was killed (orphaned suite)"


def _suite_running(wt):
    return subprocess.run(["pgrep", "-f", f"pytest.*tests/"], capture_output=True).stdout.strip() != b"" and \
        subprocess.run(["pgrep", "-f", "test_slow"], capture_output=True).returncode in (0, 1)
