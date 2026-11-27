"""spec-sync: the spec follows the schematic netlist and the board footprints; spec-only content stays; a net the
schematic and the board disagree on refuses the write."""
import json

from fluxplace import specsync as SY

BOARD = '''(kicad_pcb (version 20240108)
 (footprint "Lib:SOT-23" (property "Reference" "Q9") (property "Value" "NPN") (property "MPN" "MMBT3904LT1G")
   (property "Manufacturer" "onsemi") (attr smd)
   (pad "1" smd rect (at 0 0) (net "SIG")) (pad "2" smd rect (at 1 0) (net "GND")) (pad "3" smd rect (at 2 0) (net "")))
 (footprint "R_0603" (property "Reference" "R1") (property "Value" "10k") (property "MPN" "RC0603FR-0710KL") (attr smd dnp)
   (pad "1" smd rect (at 0 0) (net "SIG")) (pad "2" smd rect (at 1 0) (net "GND")))
 (footprint "SOIC-8" (property "Reference" "U1") (property "Value" "IC") (attr smd)
   (pad "1" smd rect (at 0 0) (net "SIG")) (pad "2" smd rect (at 1 0) (net "GND")) (pad "2" smd rect (at 1 1) (net "GND")))
)'''


def _xml(q_gnd="GND"):
    def node(r, p, n):
        return f'<node ref="{r}" pin="{p}"/>'
    return f'''<export><components>
<comp ref="Q9"><value>NPN</value><footprint>Lib:SOT-23</footprint><fields><field name="MPN">MMBT3904LT1G</field></fields></comp>
<comp ref="R1"><value>10k</value><footprint>R_0603</footprint></comp>
<comp ref="U1"><value>IC</value><footprint>SOIC-8</footprint></comp></components><nets>
<net code="1" name="/SIG">{node("Q9", 1, 0)}{node("R1", 1, 0)}{node("U1", "IN", 0)}</net>
<net code="2" name="{q_gnd}">{node("Q9", 2, 0)}{node("R1", 2, 0)}{node("U1", "GND", 0)}</net>
<net code="3" name="unconnected-(Q9-Pad3)">{node("Q9", 3, 0)}</net></nets></export>'''


SPEC = {"name": "t", "components": [
    {"ref": "R1", "value": "1k", "lib": "L", "fp": "R_0603", "note": "keep"},
    {"ref": "U1", "value": "IC", "lib": "L", "fp": "SOIC-8", "pinmap": {"IN": "1", "GND": "2"}, "pinmap_source": "x.pdf#p1"},
    {"ref": "X9", "value": "gone", "lib": "L", "fp": "F"}],
    "nets": {"SIG": [["R1", "2"], ["U1", "IN"], ["X9", "1"]], "OLD": [["R1", "1"], ["X9", "2"]]}}


def _setup(tmp_path, q_gnd="GND"):
    (tmp_path / "b.kicad_pcb").write_text(BOARD)
    (tmp_path / "n.xml").write_text(_xml(q_gnd))
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    return str(tmp_path / "b.kicad_pcb"), str(tmp_path / "n.xml"), str(tmp_path / "spec.json")


def test_sync_applies_derived_fields_only(tmp_path):
    b, x, sp = _setup(tmp_path)
    d, rc = SY.sync(b, x, sp, write=True, log=lambda m: None)
    assert rc == 0 and not d["errors"]
    out = json.load(open(sp))
    c = {c["ref"]: c for c in out["components"]}
    assert "X9" not in c and c["Q9"]["mpn"] == "MMBT3904LT1G" and c["Q9"]["mfr"] == "onsemi" and c["Q9"]["fp"] == "SOT-23"
    assert c["R1"]["value"] == "10k" and c["R1"]["dnp"] is True and c["R1"]["note"] == "keep"      # spec-only field kept
    assert c["U1"]["pinmap"] == SPEC["components"][1]["pinmap"] and c["U1"]["pinmap_source"] == "x.pdf#p1"
    n = {k: sorted(map(tuple, v)) for k, v in out["nets"].items()}
    assert "OLD" not in n and n["SIG"] == [("Q9", "1"), ("R1", "1"), ("U1", "IN")]               # pinmapped part keeps names
    assert ("U1", "GND") in n["GND"] and ("Q9", "2") in n["GND"] and len(n["GND"]) == 3          # duplicate pad "2" is one pin
    assert not any("unconnected" in k for k in n)
    assert (tmp_path / "spec-sync.log").exists() and list(tmp_path.glob("spec.json.bak-*"))


def test_idempotent(tmp_path):
    b, x, sp = _setup(tmp_path)
    SY.sync(b, x, sp, write=True, log=lambda m: None)
    d, rc = SY.sync(b, x, sp, write=True, log=lambda m: None)
    assert rc == 0 and not d["parts"]["added"] and not d["parts"]["changed"] and not d["nets"]["changed"] and not d["nets"]["added"]


def test_net_mismatch_refuses_write(tmp_path):
    b, x, sp = _setup(tmp_path, q_gnd="OTHER")
    before = open(sp).read()
    d, rc = SY.sync(b, x, sp, write=True, log=lambda m: None)
    assert rc == 2 and any(code == "PAD_NET_MISMATCH" for code, _ in d["errors"])
    assert open(sp).read() == before and not list(tmp_path.glob("spec.json.bak-*"))


def test_dry_run_writes_nothing(tmp_path):
    b, x, sp = _setup(tmp_path)
    before = open(sp).read()
    _d, rc = SY.sync(b, x, sp, write=False, log=lambda m: None)
    assert rc == 0 and open(sp).read() == before


def test_pipeline_entry_syncs_and_stops_on_mismatch(tmp_path):
    import types
    from fluxplace import pipeline as PL
    b, x, sp = _setup(tmp_path)
    c = types.SimpleNamespace(cons={"specsync": {"sch": "n.xml"}}, cons_path=str(tmp_path / "c.toml"), spec_path=sp, spec=None)
    st = {}
    assert PL._specsync_entry(c, str(tmp_path), b, st) == 0 and c.spec["components"][0]["ref"] in ("R1", "U1", "Q9")
    assert st["specsync"]["errors"] == []
    b2, x2, sp2 = _setup(tmp_path, q_gnd="OTHER")
    assert PL._specsync_entry(c, str(tmp_path), b, {}) == 2


def _swap_setup(tmp_path, monkeypatch, dossier):
    """U1's footprint changed (SOIC-8 spec -> board SOIC-8 pads 1,2 but spec said fp SOT): a swap with a pinmap."""
    from fluxplace import dossier as DO
    spec = json.loads(json.dumps(SPEC))
    spec["components"][1]["fp"] = "OLD-FP"
    spec["components"][1]["mpn"] = "OLDPART"
    spec["components"][1]["pinmap"] = {"IN": "2", "GND": "1"}          # stale: the old part had the pins the other way round
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "NEWPART.pdf").write_bytes(b"%PDF")
    (ds / "datasheets.json").write_text(json.dumps({"NEWPART": {"file": "NEWPART.pdf", "sha256": "x"}}))
    board = BOARD.replace('(property "Value" "IC")', '(property "Value" "IC") (property "MPN" "NEWPART")')
    (tmp_path / "b.kicad_pcb").write_text(board)
    (tmp_path / "n.xml").write_text(_xml())
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    monkeypatch.setattr(DO, "load", lambda mpn, sha, *a, **k: dossier)
    return str(tmp_path / "b.kicad_pcb"), str(tmp_path / "n.xml"), str(tmp_path / "spec.json"), str(ds)


FULL = {"pins": [{"num": ["1"], "name": "IN", "alt": ["INPUT"], "p": 3}, {"num": ["2"], "name": "VSS", "alt": ["GND"], "p": 3}]}


def test_swap_rebuilds_pinmap_from_dossier(tmp_path, monkeypatch):
    b, x, sp, ds = _swap_setup(tmp_path, monkeypatch, FULL)
    d, rc = SY.sync(b, x, sp, write=True, log=lambda m: None, ds_dir=ds)
    u1 = next(c for c in json.load(open(sp))["components"] if c["ref"] == "U1")
    assert rc == 0 and not d["fails"]
    assert u1["pinmap"] == {"IN": "1", "GND": "2"} and u1["pinmap_source"] == "NEWPART.pdf#p3" and u1["mpn"] == "NEWPART"
    nets = json.load(open(sp))["nets"]
    assert ["U1", "IN"] in nets["SIG"] and ["U1", "GND"] in nets["GND"]   # schematic pin names resolve through the NEW pinmap


def test_swap_without_pin_table_is_a_fail_not_a_silent_keep(tmp_path, monkeypatch):
    b, x, sp, ds = _swap_setup(tmp_path, monkeypatch, None)
    d, rc = SY.sync(b, x, sp, write=True, log=lambda m: None, ds_dir=ds)
    assert d["fails"] and rc in (1, 2) and d["fails"][0][0] == "SWAP_PINMAP_REVIEW" and "U1" in d["fails"][0][1]
    partial = {"pins": [FULL["pins"][0]]}
    monkeypatch.setattr(__import__("fluxplace.dossier", fromlist=["x"]), "load", lambda *a, **k: partial)
    d, rc = SY.sync(b, x, sp, write=False, log=lambda m: None, ds_dir=ds)
    assert "no pad 2" in d["fails"][0][1] and rc in (1, 2)       # 2: the kept stale pinmap now also disagrees with the board
