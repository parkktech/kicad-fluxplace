"""tools/merge-gate.sh: a killed gate must free its slot (no orphan suite holding the flock)."""
import contextlib, fcntl, os, signal, subprocess, sys, time

GATE = os.environ.get("GATE_SCRIPT") or os.path.join(os.path.dirname(__file__), "..", "tools", "merge-gate.sh")


def _free(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _wt(tmp_path, name="wt", body="def test_ok():\n    pass\n"):
    wt = tmp_path / name; (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "test_ok.py").write_text(body)
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(wt)] + c, check=True, capture_output=True)
    return wt


def _gate(tmp_path, wt, extra=None):
    (tmp_path / "base").write_text("")
    env = {k: v for k, v in os.environ.items() if k != "GATE_PRIORITY"}   # the outer mainline gate runs with GATE_PRIORITY=1
    env = dict(env, GATE_SLOTS="1", GATE_LOCKDIR=str(tmp_path / "lock"), GATE_WORKERS="0", GATE_POLL="0.3",
               PY=sys.executable, **(extra or {}))
    return subprocess.Popen(["bash", GATE, str(wt), str(tmp_path / "base")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _hold_slot(tmp_path):
    lock = tmp_path / "lock"; lock.mkdir(exist_ok=True)
    h = subprocess.Popen(["flock", str(lock / "slot1"), "sleep", "300"], start_new_session=True)
    t = time.time()
    while _free(str(lock / "slot1")) and time.time() - t < 10:
        time.sleep(0.1)
    return h


def _kill_holder(h):
    with contextlib.suppress(OSError):
        os.killpg(h.pid, signal.SIGKILL)


def _wait_queued(tmp_path, n):
    t = time.time()
    while time.time() - t < 20:
        if len([f for f in os.listdir(tmp_path / "lock") if f.startswith("wait.")]) >= n:
            return
        time.sleep(0.1)
    raise AssertionError("waiters never queued")


def _finish_order(procs, timeout=90):
    order, t = [], time.time()
    while len(order) < len(procs) and time.time() - t < timeout:
        for k, p in procs.items():
            if k not in order and p.poll() is not None:
                order.append(k)
        time.sleep(0.05)
    return order


def test_killed_gate_releases_its_slot(tmp_path):
    wt = _wt(tmp_path, body="import time\ndef test_ok():\n    time.sleep(120)\n")
    p = _gate(tmp_path, wt)
    slot = str(tmp_path / "lock" / "slot1")
    t = time.time()
    while time.time() - t < 30 and (not os.path.exists(slot) or _free(slot) or not _suite_running()):
        time.sleep(0.3)
    assert not _free(slot), "gate never took the slot"
    p.send_signal(signal.SIGTERM); p.wait(timeout=20)
    t = time.time()
    while time.time() - t < 10 and not _free(slot):
        time.sleep(0.3)
    assert _free(slot), "slot still held after the gate was killed (orphaned suite)"


def _suite_running():
    return subprocess.run(["pgrep", "-f", "pytest.*tests/"], capture_output=True).stdout.strip() != b""


def test_waiters_take_slots_in_arrival_order(tmp_path):
    wt = _wt(tmp_path); h = _hold_slot(tmp_path)
    try:
        procs = {}
        for k in ("a", "b", "c"):
            procs[k] = _gate(tmp_path, wt); _wait_queued(tmp_path, len(procs))
        _kill_holder(h); h.wait()
        assert _finish_order(procs) == ["a", "b", "c"]
    finally:
        _kill_holder(h)


def test_priority_gate_overtakes_earlier_waiters(tmp_path):
    wt = _wt(tmp_path); h = _hold_slot(tmp_path)
    try:
        procs = {"a": _gate(tmp_path, wt)}; _wait_queued(tmp_path, 1)
        procs["b"] = _gate(tmp_path, wt); _wait_queued(tmp_path, 2)
        procs["main"] = _gate(tmp_path, wt, {"GATE_PRIORITY": "1"}); _wait_queued(tmp_path, 3)
        _kill_holder(h); h.wait()
        assert _finish_order(procs) == ["main", "a", "b"]
    finally:
        _kill_holder(h)


def test_queue_listing_names_holder_and_waiters_in_order(tmp_path):
    wt = _wt(tmp_path, body="import time\ndef test_ok():\n    time.sleep(60)\n")
    a = _gate(tmp_path, wt); lock = tmp_path / "lock"
    t = time.time()
    while time.time() - t < 30 and not (lock / "slot1.info").exists():
        time.sleep(0.2)
    b = _gate(tmp_path, wt); _wait_queued(tmp_path, 1)
    c = _gate(tmp_path, wt, {"GATE_PRIORITY": "1"}); _wait_queued(tmp_path, 2)
    try:
        env = dict(os.environ, GATE_SLOTS="1", GATE_LOCKDIR=str(lock))
        out = subprocess.run(["bash", GATE, "--queue"], env=env, capture_output=True, text=True, timeout=20).stdout.splitlines()
        assert out[0].startswith("slot1:") and "started" in out[0] and "load" in out[0] and f"pid {a.pid}" in out[0]
        assert out[1].startswith("wait 1:") and "PRIORITY" in out[1] and f"pid {c.pid}" in out[1]
        assert out[2].startswith("wait 2:") and f"pid {b.pid}" in out[2]
    finally:
        for p in (b, c, a):
            p.send_signal(signal.SIGTERM); p.wait(timeout=20)
    assert not [f for f in os.listdir(lock) if f.startswith("wait.") or f.endswith(".info")], "queue files left behind"


def test_status_reports_stale_when_no_row_for_current_sha(tmp_path):
    """Owner 2026-11-27: an old gate output was reported as current. --status only calls a result CURRENT for REF's sha."""
    import os, subprocess, pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    gate = repo / "tools" / "merge-gate.sh"
    res = tmp_path / "r.tsv"
    res.write_text("2026-11-27 13:31:00\t000000000000\tbatch14\tFAIL\t4\n")
    env = {"GATE_RESULTS": str(res), "PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}
    p = subprocess.run(["bash", str(gate), "--status", "HEAD"], capture_output=True, text=True, env=env)
    assert p.returncode == 1 and "STALE" in p.stdout and "NO GATE RESULT" in p.stdout
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    res.write_text(res.read_text() + f"2026-11-27 15:00:00\t{sha[:12]}\tmain\tPASS\t0\n")
    p = subprocess.run(["bash", str(gate), "--status", "HEAD"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout.startswith("CURRENT") and "PASS" in p.stdout
