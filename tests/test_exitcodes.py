"""A command that RETURNS its verdict code exits with it (cli.main used to drop it); --help renders for every command."""
import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import outputs as OUT


def _with_cmd(monkeypatch, ret, name="x-test"):
    def build():
        ap = argparse.ArgumentParser(prog="fluxplace")
        sub = ap.add_subparsers(dest="cmd", required=True)
        p = sub.add_parser(name)
        p.set_defaults(fn=lambda a: ret)
        return ap
    monkeypatch.setattr(cli, "build_parser", build)


@pytest.mark.parametrize("ret, code", [(1, 1), (3, 3)])
def test_returned_verdict_code_becomes_the_exit_code(monkeypatch, ret, code):
    _with_cmd(monkeypatch, ret)
    with pytest.raises(SystemExit) as e:
        cli.main(["x-test"])
    assert e.value.code == code


@pytest.mark.parametrize("ret", [0, None, True, {"moved": 3}, [1], "text"])
def test_zero_none_and_data_return_values_exit_zero(monkeypatch, ret):
    _with_cmd(monkeypatch, ret)
    assert cli.main(["x-test"]) is None                # no SystemExit: exit 0


def test_compact_wrapper_keeps_a_returned_code(capsys):
    a = argparse.Namespace(full_output=False, raw=False, json=False, quiet=True, top=None, board=None, out=None)
    with pytest.raises(SystemExit) as e:
        OUT.run_compact("lint", a, lambda _a: 2)
    assert e.value.code == 2
    assert OUT.run_compact("lint", a, lambda _a: 0) is None


def _subs(ap):
    for a in ap._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def test_help_renders_for_the_top_level_and_every_command():
    ap = cli.build_parser()
    assert "fanout" in ap.format_help()                # a bare `%` in a help string used to crash this
    for name, p in _subs(ap).items():
        assert p.format_help(), name


def test_fanout_fail_gate_reaches_the_exit_code(tmp_path, monkeypatch):
    """the real command: its FAIL verdict is `return 1`; the process must not exit 0 on it."""
    from fluxplace import fanout as FO
    seen = {}

    def fake(a):
        seen["ran"] = True
        print("fanout: FANOUT_FAIL 1 pin blocked")
        return 1
    monkeypatch.setattr(FO, "cmd_fanout", fake)
    ap = cli.build_parser()
    _subs(ap)["fanout"].set_defaults(fn=fake)
    monkeypatch.setattr(cli, "build_parser", lambda: ap)
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    with pytest.raises(SystemExit) as e:
        cli.main(["fanout", str(b), "--out", str(tmp_path / "o.kicad_pcb")])
    assert e.value.code == 1 and seen["ran"]
