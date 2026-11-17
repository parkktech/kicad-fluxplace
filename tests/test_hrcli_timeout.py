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


def test_suggest_internal_error_is_normal_not_found(monkeypatch, capsys):
    import json
    import types
    from fluxplace import handroute
    monkeypatch.setattr(handroute, "suggest", lambda *a, **k: (_ for _ in ()).throw(TypeError("synthetic geometry error")))
    monkeypatch.setattr(hrcli, "_rev", lambda a: "rev")
    a = types.SimpleNamespace(board="b", net="N", src=None, dst=None, conn=None, constraints=None, width=None, via=None,
                              pitch=0.05, margin=2.0, via_cost=2.0, layers=None, relax=False, relax_cost=1.5,
                              explore=False, brief=False, json=True)
    assert hrcli.cmd_suggest(a) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["found"] and "TypeError: synthetic geometry error" in result["reason"]
    assert result["summary"].startswith("suggest none:")


def test_suggest_brief_json_emits_path_and_full_object(monkeypatch, capsys):
    import json
    import types
    from fluxplace import handroute
    path = "F.Cu: 1,1 2,2"
    monkeypatch.setattr(handroute, "suggest", lambda *a, **k: {"found": True, "path": path, "length_mm": 1.4, "vias": 0})
    monkeypatch.setattr(hrcli, "_rev", lambda a: "rev")
    a = types.SimpleNamespace(board="b", net="N", src="1,1", dst="2,2", conn=None, constraints=None, width=None, via=None,
                              pitch=0.05, margin=2.0, via_cost=2.0, layers=None, relax=False, relax_cost=1.5,
                              explore=False, brief=True, json=True)
    assert hrcli.cmd_suggest(a) == 0
    captured = capsys.readouterr()
    assert path in captured.err
    result = json.loads(captured.out)
    assert result["path"] == path and result["board_rev"] == "rev"
    assert path in result["summary"]
