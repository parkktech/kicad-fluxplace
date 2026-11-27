"""w93: a pipeline run reads its own frozen copy of the code (<work>/.code), not the live checkout."""
import os
import shutil
import sys

import pytest

from fluxplace import codefreeze as CF
from fluxplace.freshproc import in_fresh_process


@pytest.fixture
def fake(tmp_path, monkeypatch):
    co = tmp_path / "checkout"
    (co / "fluxplace").mkdir(parents=True)
    (co / "fluxplace" / "__init__.py").write_text("")
    (co / "fluxplace" / "probe.py").write_text("def v():\n    return 'v1'\n")
    (co / "cli.py").write_text("")
    monkeypatch.setattr(CF, "_CHECKOUT", str(co))
    monkeypatch.delenv(CF.ENV, raising=False)
    saved = (os.environ.get("PYTHONPATH"), list(sys.path))
    yield co, tmp_path / "work"
    CF.release(*saved)


def test_child_rung_module_file_is_inside_the_frozen_copy(fake):
    co, work = fake
    info = CF.freeze(str(work))
    assert info["dir"] == str(work / ".code") and info["sha"] == "nogit"
    assert CF.root() == info["dir"]
    spec = in_fresh_process("importlib.util", "find_spec", "fluxplace.probe")
    assert str(work / ".code" / "fluxplace" / "probe.py") in spec and str(co) not in spec


def test_checkout_change_mid_run_is_not_seen_and_resume_reuses_the_copy(fake):
    co, work = fake
    CF.freeze(str(work))
    (co / "fluxplace" / "probe.py").write_text("def v():\n    return 'v2'\n")
    assert in_fresh_process("fluxplace.probe", "v") == "v1"
    os.environ.pop(CF.ENV)                                    # a resumed run: new process, same workdir
    CF.freeze(str(work))
    assert in_fresh_process("fluxplace.probe", "v") == "v1"
    os.environ.pop(CF.ENV)
    shutil.rmtree(work / ".code")                             # operator asks for new code
    CF.freeze(str(work))
    assert in_fresh_process("fluxplace.probe", "v") == "v2"
