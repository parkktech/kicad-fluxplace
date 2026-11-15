"""fix-c #8: the gate says what it verified; pinmap pad numbers are cross-checked against KiCad library data."""
import os
from fluxplace import schblocks as SB


def _lib(tmp_path):
    fp = tmp_path / "fp" / "Lib.pretty"
    fp.mkdir(parents=True)
    (fp / "Part.kicad_mod").write_text('(footprint "Part" (pad "1" smd rect) (pad "2" smd rect))\n')
    sym = tmp_path / "sym"
    sym.mkdir()
    (sym / "Lib.kicad_sym").write_text(
        '(kicad_symbol_lib (symbol "Sym" (symbol "Sym_0_1" '
        '(pin input line (at 0 0 0) (length 1) (name "VIN" (effects)) (number "1" (effects)))'
        '(pin input line (at 0 0 0) (length 1) (name "GND" (effects)) (number "2" (effects))))))\n')
    return str(tmp_path / "fp"), str(sym)


def _M(comps):
    return {"comps": comps, "nets": {}}


def test_pinmap_pad_missing_from_footprint_is_mismatch(tmp_path):
    fpd, syd = _lib(tmp_path)
    M = _M({"U1": {"ref": "U1", "fp": "Lib:Part", "pinmap": {"VIN": "1", "GND": "9"}, "nc": []}})
    r = SB.pinmap_crosscheck(M, fp_dirs=[fpd], sym_dirs=[syd])
    assert [m["code"] for m in r["findings"]] == ["SCH_PINMAP_MISMATCH"]
    assert r["findings"][0]["level"] == "WARN" and r["checked"] == 1 and r["unchecked"] == 0


def test_pinmap_symbol_name_to_number_mismatch(tmp_path):
    fpd, syd = _lib(tmp_path)
    M = _M({"U1": {"ref": "U1", "fp": "Lib:Part", "symbol": "Lib:Sym", "pinmap": {"VIN": "2"}, "nc": []}})
    r = SB.pinmap_crosscheck(M, fp_dirs=[fpd], sym_dirs=[syd])
    assert [m["code"] for m in r["findings"]] == ["SCH_PINMAP_MISMATCH"]


def test_unresolvable_part_counted_unchecked(tmp_path):
    fpd, syd = _lib(tmp_path)
    M = _M({"U2": {"ref": "U2", "fp": "Nope:Gone", "pinmap": {"A": "1"}, "nc": []}})
    r = SB.pinmap_crosscheck(M, fp_dirs=[fpd], sym_dirs=[syd])
    assert r["findings"] == [] and r["unchecked"] == 1


def test_gate_text_states_what_was_verified():
    txt = SB.verified_statement({"ok": True, "verdict": "PASS"}, {"checked": 0, "unchecked": 2, "findings": []})
    assert "drawing matches spec netlist" in txt
    assert "spec pinmap NOT checked against the part here" in txt and "PINMAP_" in txt
    assert "SCH_PINMAP_UNCHECKED 2" in txt
