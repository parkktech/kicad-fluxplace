import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import hrcli


def test_timeout_restores_existing_destination(tmp_path):
    out = tmp_path / "board.kicad_pcb"
    out.write_bytes(b"original board")
    r = hrcli._timed("time:sleep", (2,), {}, 0.05, [str(out)])
    assert r["timed_out"] and not r["accepted"]
    assert r["reason"] == "no verdict: timed out after 0.05 s, nothing written"
    assert out.read_bytes() == b"original board"


def test_timeout_leaves_absent_destination_absent(tmp_path):
    out = tmp_path / "new.kicad_pcb"
    r = hrcli._timed("time:sleep", (2,), {}, 0.05, [str(out)])
    assert r["timed_out"] and not out.exists()
