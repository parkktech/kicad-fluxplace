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


# ---- review fixes (w16reap) ----
ROOT = os.path.dirname(os.path.dirname(procs.__file__))
SLEEP = [sys.executable, "-c", "import time; time.sleep(300)"]


def _child(code, **kw):
    """A fresh interpreter with fluxplace importable (plain subprocess: the test owns it)."""
    return subprocess.Popen([sys.executable, "-u", "-c", "import sys; sys.path.insert(0, %r)\n" % ROOT + textwrap.dedent(code)], **kw)


def test_timeout_does_not_wait_the_grace_for_a_dead_leader():
    """the SIGTERMed leader is a zombie until reaped; it must not hold the kill for the whole GRACE_S"""
    t = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run(SLEEP, timeout=0.3)
    assert time.time() - t < 3.0 and not procs._LIVE


def test_reap_all_does_not_wait_the_grace_for_dead_leaders():
    p = procs.popen(SLEEP)
    t = time.time()
    procs.reap_all()
    assert time.time() - t < 3.0 and p.poll() is not None and not procs._LIVE


def test_nested_child_reaps_its_own_session_on_outer_timeout():
    """a rung job is not the cli: it must still kill its router (own session) when the parent's group kill hits it"""
    inner = "import sys; sys.path.insert(0, %r); from fluxplace import procs; procs.run([sys.executable, '-u', '-c', %r])" % (ROOT, GC)
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        procs.run([sys.executable, "-u", "-c", inner], stdout=subprocess.PIPE, text=True, timeout=3)
    gpid = int((ei.value.output or "").split()[0])
    assert _gone(gpid)


def test_ignored_sighup_stays_ignored(tmp_path):
    """nohup: a detached run must survive the terminal closing"""
    p = _child("""
        import os, signal
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        from fluxplace import procs
        procs.install()
        procs.run([sys.executable, "-c", "pass"])
        os.kill(os.getpid(), signal.SIGHUP)
        print("alive", signal.getsignal(signal.SIGHUP) is signal.SIG_IGN)
    """, stdout=subprocess.PIPE, text=True)
    assert p.communicate(timeout=30)[0].split() == ["alive", "True"] and p.returncode == 0


def test_signal_inside_a_locked_block_does_not_deadlock():
    p = _child("""
        import os, signal
        from fluxplace import procs
        procs.install()
        with procs._LOCK:
            os.kill(os.getpid(), signal.SIGTERM)
    """)
    assert p.wait(timeout=20) == 128 + signal.SIGTERM


def test_second_signal_does_not_skip_the_sigkill_pass(tmp_path):
    """a child that ignores SIGTERM: two SIGTERMs in a row on the parent still end with its SIGKILL"""
    stub = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(300)"
    p = _child("""
        from fluxplace import procs
        procs.SIGNAL_GRACE_S = 1.5
        import subprocess
        c = procs.popen([sys.executable, "-u", "-c", %r], stdout=subprocess.PIPE, text=True)
        c.stdout.readline()
        print(c.pid, flush=True)
        import time; time.sleep(300)
    """ % stub, stdout=subprocess.PIPE, text=True)
    cpid = int(p.stdout.readline())
    p.send_signal(signal.SIGTERM)
    time.sleep(0.4)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 128 + signal.SIGTERM
    assert _gone(cpid, 3)


def test_no_new_child_after_sigterm():
    p = _child("""
        import os, signal
        from fluxplace import procs
        procs.install()
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except SystemExit:
            pass
        try:
            procs.run([sys.executable, "-c", "pass"])
        except procs.Closing:
            print("closing")
    """, stdout=subprocess.PIPE, text=True)
    assert p.communicate(timeout=30)[0].strip() == "closing"


def test_sigint_reaps_children_of_worker_threads():
    """children are out of the terminal's foreground group: Ctrl-C reaches them only through the handler"""
    p = _child("""
        import threading, time
        from fluxplace import procs
        procs.install()
        def work():
            try:
                procs.run([sys.executable, "-u", "-c", %r])
            except BaseException:
                pass
        t = threading.Thread(target=work); t.start()
        try:
            time.sleep(300)
        except KeyboardInterrupt:
            t.join(10)
            print("interrupted", t.is_alive(), flush=True)
    """ % GC, stdout=subprocess.PIPE, text=True)
    gpid = int(p.stdout.readline())
    p.send_signal(signal.SIGINT)
    assert p.communicate(timeout=30)[0].split() == ["interrupted", "False"]
    assert _gone(gpid)


def test_forked_worker_does_not_own_the_parents_children():
    """a fork pool worker inherits the handler; Pool.terminate() SIGTERMs it; the parent's router must survive"""
    p = procs.popen(SLEEP)
    try:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.write(w, str(len(procs._LIVE)).encode())
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        os.close(w)
        assert os.read(r, 16) == b"0"
        os.close(r)
        assert p.poll() is None and p.pid in procs._LIVE
    finally:
        procs.reap_all(grace=1.0)


def test_a_recycled_pid_is_not_killed():
    """a finished child nobody forgot: its number may name a stranger's group by the time reap_all runs"""
    stranger = subprocess.Popen(SLEEP, start_new_session=True)

    class Done:
        pid = stranger.pid
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0
    try:
        procs._LIVE[stranger.pid] = Done()
        procs.reap_all(grace=0.3)
        assert stranger.poll() is None and not procs._LIVE
    finally:
        procs._LIVE.pop(stranger.pid, None)
        stranger.kill()
        stranger.wait()


def test_popen_drops_finished_children():
    p = procs.popen([sys.executable, "-c", "pass"])
    p.wait(timeout=20)
    assert p.pid in procs._LIVE
    q = procs.popen([sys.executable, "-c", "pass"])
    q.wait(timeout=20)
    assert p.pid not in procs._LIVE
    procs.forget(q)
    assert not procs._LIVE


def test_run_argument_errors_match_subprocess_run():
    with pytest.raises(ValueError):
        procs.run([sys.executable, "-c", "pass"], capture_output=True, stdout=subprocess.PIPE)
    with pytest.raises(ValueError):
        procs.run([sys.executable, "-c", "pass"], input="x", stdin=subprocess.PIPE)
    r = procs.run([sys.executable, "-c", "import sys, os; print(sys.stdin.read() + os.environ['K'] + os.getcwd())"],
                  input="in ", env=dict(os.environ, K="env "), cwd="/", capture_output=True, text=True)
    assert r.stdout.strip() == "in env /" and r.stderr == "" and r.args[0] == sys.executable
    assert isinstance(procs.run([sys.executable, "-c", "print(1)"], capture_output=True).stdout, bytes)
    with pytest.raises(subprocess.CalledProcessError) as ei:
        procs.run([sys.executable, "-c", "import sys; print('o'); sys.exit(4)"], check=True, capture_output=True, text=True)
    assert ei.value.returncode == 4 and ei.value.output.strip() == "o"


def test_run_logged_child_is_registered_and_forgotten(tmp_path):
    """the long router process (routestatus.run_logged) is reaped with the caller like any other child"""
    from fluxplace import routestatus as RS
    seen = []
    real = procs.popen

    def spy(cmd, **kw):
        p = real(cmd, **kw)
        seen.append(p.pid in procs._LIVE)
        return p
    old, procs.popen = procs.popen, spy
    try:
        r = RS.run_logged([sys.executable, "-c", "print('hi')"], log_path=str(tmp_path / "r.log"),
                          status_path=str(tmp_path / "r.status.json"))
    finally:
        procs.popen = old
    assert r.returncode == 0 and seen == [True] and not procs._LIVE
