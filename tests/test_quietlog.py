import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import quietlog as Q

NOISE = './kicad/include/properties/property.h(607): assert "m_choices.GetCount() > 0" failed in PROPERTY_ENUM(): No enum choices defined'
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
