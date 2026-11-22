"""K0: FFI contract. Needs kernel/build.sh (skipped with a reason when the library is absent)."""
import os
import re

import pytest

from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")


def _board(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb)\n")
    return str(p)


def test_ping(tmp_path):
    with K.Kernel(_board(tmp_path)) as k:
        assert k.call("ping")["pong"] is True


def test_unknown_op_raises(tmp_path):
    with K.Kernel(_board(tmp_path)) as k:
        with pytest.raises(K.KernelError, match="unknown op"):
            k.call("no_such_op")


def test_missing_file_raises_with_path(tmp_path):
    missing = str(tmp_path / "missing.kicad_pcb")
    with pytest.raises(K.KernelError) as e:
        K.Kernel(missing)
    assert missing in str(e.value)


def test_panic_is_an_error_and_next_call_works(tmp_path):
    with K.Kernel(_board(tmp_path)) as k:
        with pytest.raises(K.KernelError, match="panic"):
            k.call("panic")
        assert k.call("ping")["pong"] is True


def _rss_kb():
    with open("/proc/self/status") as fh:
        return int(re.search(r"VmRSS:\s+(\d+)", fh.read()).group(1))


def test_open_close_cycles_do_not_leak(tmp_path):
    path = _board(tmp_path)
    for _ in range(50):                      # warm-up: allocator and lib settle
        with K.Kernel(path) as k:
            k.call("ping")
    before = _rss_kb()
    for _ in range(1000):
        with K.Kernel(path) as k:
            k.call("ping")
    assert (_rss_kb() - before) / 1024 < 20
