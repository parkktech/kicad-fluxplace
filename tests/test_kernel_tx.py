"""K10: transactions through the ops, and emitted copper accepted by kicad-cli."""
import shutil
import subprocess

import pytest

from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
         '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts") (uuid "12121212-1212-4121-8121-121212121212"))\n')
SEG = {"kind": "seg", "net": "N", "layer": "F.Cu", "width": 0.2, "a": [10, 10], "b": [30, 10], "uuid": "seg-1"}
VIA = {"kind": "via", "net": "N", "at": [20, 10], "dia": 0.6, "drill": 0.3, "layers": ["F.Cu", "B.Cu"], "uuid": "via-1"}


def test_rollback_restores_items_and_index(tmp_path):
    path = tmp_path / "t.kicad_pcb"
    path.write_text(HEAD4 + ")\n")
    with K.Kernel(str(path)) as k:
        before = k.call("items")["items"]
        k.call("begin")
        k.call("begin")
        ids = k.call("add", items=[SEG, VIA])["ids"]
        assert k.call("remove", ids=[ids[0]])["removed"] == 1
        assert k.call("rollback")["depth"] == 1
        assert k.call("rollback")["depth"] == 0
        assert k.call("items")["items"] == before
        with pytest.raises(K.KernelError):
            k.call("commit")


def test_committed_edits_survive_and_uuid_remove(tmp_path):
    path = tmp_path / "c.kicad_pcb"
    path.write_text(HEAD4 + ")\n")
    with K.Kernel(str(path)) as k:
        k.call("begin")
        k.call("add", items=[SEG])
        k.call("commit")
        assert [i["kind"] for i in k.call("items", kinds=["track"])["items"]] == ["track"]
        assert k.call("remove", uuids=["seg-1"])["removed"] == 1
        assert k.call("items", kinds=["track"])["items"] == []


def test_emitted_copper_loads_in_kicad_cli(tmp_path):
    cli = shutil.which("kicad-cli") or "/usr/bin/kicad-cli"
    base = tmp_path / "base.kicad_pcb"
    base.write_text(HEAD4 + ")\n")
    with K.Kernel(str(base)) as k:
        added = k.call("add", items=[SEG, VIA])["ids"]       # segment, via, the via's hole
        text = k.call("emit", ids=[added[0], added[1]])["blocks"]
    text = "".join(b + "\n" for b in text)
    out = tmp_path / "emitted.kicad_pcb"
    out.write_text(HEAD4 + text + ")\n")
    res = subprocess.run([cli, "pcb", "drc", "--output", str(tmp_path / "drc.json"), str(out)],
                         capture_output=True, text=True, timeout=110)
    assert "Failed to load" not in (res.stdout + res.stderr), res.stderr[-500:]
    assert "unconnected items" in res.stdout, res.stdout[-300:]   # kicad-cli loaded and reported DRC
