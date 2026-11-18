import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import quietlog as Q

NOISE = './kicad/include/properties/property.h(607): assert "m_choices.GetCount() > 0" failed in PROPERTY_ENUM(): No enum choices defined'
VIA_NOISE = './kicad/pcbnew/pcb_track.cpp(387): assert ""false"" failed in GetWidth(): Warning: PCB_VIA::GetWidth called without a layer argument'
ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_filter_drops_noise_keeps_rest_and_counts():
    f = Q.LineFilter()
    out = f.feed(f"a\n{NOISE}\nb\n{NOISE}\nc".encode()) + f.finish()
    assert out == b"a\nb\nc\n[fluxplace] dropped 2 repeated KiCad assert lines\n"


def test_filter_split_across_chunks():
    f = Q.LineFilter()
    data = f"x\n{NOISE}\ny\n".encode()
    out = b"".join(f.feed(data[i:i + 7]) for i in range(0, len(data), 7)) + f.finish()
    assert out.startswith(b"x\ny\n") and f.dropped == 1


def test_filter_drops_via_width_assertion():
    assert Q.filter_text(f"before\n{VIA_NOISE}\nafter\n") == "before\nafter\n[fluxplace] dropped 1 repeated KiCad assert lines\n"


def test_no_noise_no_count_line():
    assert Q.filter_text("a\nb\n") == "a\nb\n"


def test_filter_file(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("keep\n" + (NOISE + "\n") * 5 + "also\n")
    assert Q.filter_file(str(p)) == 5
    assert p.read_text() == "keep\nalso\n[fluxplace] dropped 5 repeated KiCad assert lines\n"
    q = tmp_path / "c.log"
    q.write_text("clean\n")
    assert Q.filter_file(str(q)) == 0 and q.read_text() == "clean\n"


def test_filter_file_raw_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_RAW_STDERR", "1")
    p = tmp_path / "l.log"
    p.write_text(NOISE + "\n")
    assert Q.filter_file(str(p)) == 0 and NOISE in p.read_text()


CHILD = r"""
import os, sys
sys.path.insert(0, %r)
from fluxplace import quietlog
quietlog.install_stderr(force=True)
noise = %r
for i in range(10000):
    os.write(2, (noise + "\n").encode())
    if i %% 2500 == 0:
        os.write(2, ("real %%d\n" %% i).encode())
sys.stderr.write("py line\n")
raise SystemExit(7)
"""


def test_subprocess_fd2_filtered_exit_code_kept():
    r = subprocess.run([sys.executable, "-c", CHILD % (ROOT, NOISE)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 7
    lines = r.stderr.splitlines()
    assert [l for l in lines if l.startswith("real")] == [f"real {i}" for i in (0, 2500, 5000, 7500)]
    assert "py line" in lines
    assert lines[-1] == "[fluxplace] dropped 10000 repeated KiCad assert lines"
    assert NOISE not in r.stderr


def test_subprocess_raw_env_keeps_noise():
    r = subprocess.run([sys.executable, "-c", CHILD % (ROOT, NOISE)], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, FLUXPLACE_RAW_STDERR="1"))
    assert r.returncode == 7 and r.stderr.count("m_choices") == 10000


def test_install_disabled_under_pytest():
    assert Q.install_stderr() is None


def test_console_filter_requires_opt_in_outside_pytest():
    code = "import os,sys; sys.path.insert(0,%r); from fluxplace import quietlog; assert quietlog.install_stderr() is None; os.write(2,(%r+'\\n').encode())" % (ROOT, NOISE)
    env = dict(os.environ)
    for key in ("FLUXPLACE_RAW_STDERR", "FLUXPLACE_QUIET_STDERR", "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and NOISE in r.stderr


PCBNEW_CHILD = r"""
import sys
sys.path.insert(0, %r)
from fluxplace import quietlog
handle = quietlog.install_stderr(force=True)
import pcbnew
assert handle.p.poll() is None, "stderr pump exited during pcbnew import"
board = pcbnew.CreateEmptyBoard()
via = pcbnew.PCB_VIA(board)
for _ in range(2000):
    via.GetWidth()
assert handle.p.poll() is None, "stderr pump exited while native code was writing"
print("completed")
"""


def test_native_warning_writer_does_not_deadlock_reader():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("pcbnew missing")
    r = subprocess.run([sys.executable, "-c", PCBNEW_CHILD % ROOT], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr[-1000:]
    assert r.stdout.strip() == "completed"
    assert "GetWidth called without a layer argument" not in r.stderr
    assert "repeated KiCad assert lines" in r.stderr


def test_native_warning_filter_opt_in_is_deadlock_free():
    code = (PCBNEW_CHILD % ROOT).replace("install_stderr(force=True)", "install_stderr()")
    env = dict(os.environ, FLUXPLACE_QUIET_STDERR="1")
    env.pop("FLUXPLACE_RAW_STDERR", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20, env=env)
    assert r.returncode == 0, r.stderr[-1000:]
    assert r.stdout.strip() == "completed"
    assert "GetWidth called without a layer argument" not in r.stderr
    assert "repeated KiCad assert lines" in r.stderr
