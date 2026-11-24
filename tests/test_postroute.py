import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import postroute as PO  # noqa: E402


def test_dedupe_file_drops_stacked_duplicate_vias_and_keeps_one(tmp_path):
    via = '\t(via\n\t\t(at 155.07 110.95)\n\t\t(size 0.46)\n\t\t(drill 0.2)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(locked yes)\n\t\t(net "BT3_VDD")\n\t\t(uuid "%s")\n\t)\n'
    other = via.replace("155.07 110.95", "160 100") % "c"
    f = tmp_path / "b.kicad_pcb"
    f.write_text("(kicad_pcb\n" + via % "a" + via % "b" + other + ")\n")
    assert PO.dedupe_file(str(f)) == 1
    t = f.read_text()
    assert t.count("(at 155.07 110.95)") == 1 and t.count("(at 160 100)") == 1
    assert PO.dedupe_file(str(f)) == 0


def test_clean_hands_left_ends_to_the_via_move_then_the_joiner_with_the_manifest_nets_protected(tmp_path, monkeypatch):
    """Ends the snap leaves go to snapiter.via_run, and what that leaves to joiner_run; both skip the nets of the board's manifest
    (a change there is refused by the commit point as PROTECTED_CHANGED and would cost the whole clean-up).  A clean snap calls neither."""
    import json
    from fluxplace import snapiter as SI
    b = tmp_path / "b.kicad_pcb"
    b.write_text("board")
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_pcb.done.json").write_text(json.dumps({"nets": {"P_N": {}, "P_P": {}}}))
    monkeypatch.setattr(PO, "_unc", lambda board, cli: 3)
    calls = []
    left = {"snap": 2, "via": 1}
    monkeypatch.setattr(SI, "run", lambda s, d, log=None: {"moved": 5, "centred_left": left["snap"]})
    monkeypatch.setattr(SI, "via_run", lambda s, d, log=None, protect=(): calls.append(("via", list(protect))) or {"moved": 1, "centred_left": left["via"]})
    monkeypatch.setattr(SI, "joiner_run", lambda s, d, log=None, protect=(): calls.append(("joiner", list(protect))) or {"moved": 1, "centred_left": 0})
    rep = PO.clean(str(b), str(tmp_path / "o.kicad_pcb"))
    assert calls == [("via", ["P_N", "P_P"]), ("joiner", ["P_N", "P_P"])] and rep["joiner"]["centred_left"] == 0 and rep["kept"]
    left.update(via=0)
    calls.clear()
    assert "joiner" not in PO.clean(str(b), str(tmp_path / "o.kicad_pcb")) and calls == [("via", ["P_N", "P_P"])]
    left.update(snap=0)
    calls.clear()
    assert "via" not in PO.clean(str(b), str(tmp_path / "o.kicad_pcb")) and calls == []
