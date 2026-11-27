"""w95: per-MPN component dossier - verification against the page, cache, elec facts, pinmap cross-check."""
import json
import os

from fluxplace import dossier as DO, elec as EL, partdocs as PD

PAGES = ["TPS0000 buck converter\nPin Functions\n1 VIN  2 GND  3 SW  4 FB  5 EN\nGPIO bank IO1 IO2",
         "Recommended Operating Conditions\nVIN 3.0 to 17 V\nPlace a 10 uF capacitor within 2 mm of VIN"]
RAW = {"category": "regulator", "description": "buck",
       "package": {"name": "SOT-23-5", "pins": 5, "p": 1},
       "pins": [{"num": ["1"], "name": "VIN", "type": "power_in", "p": 1}, {"num": ["2"], "name": "GND", "type": "ground", "p": 1},
                {"num": ["5"], "name": "EN", "type": "input", "p": 1},
                {"num": ["4"], "name": "BOGUS", "type": "input", "p": 1}, {"num": ["3"], "name": "SW", "type": "output", "p": 1}],
       "supplies": [{"pins": ["VIN", "GND", "SW"], "min": 3.0, "max": 17, "unit": "V", "p": 2},
                    {"pins": ["VIN"], "min": 2.5, "max": 40, "unit": "V", "p": 2}],
       "decoupling": [{"pins": ["VIN"], "value": "10 uF", "distance_mm": 2, "p": 2}],
       "crystal": {"load_pf": 12, "p": 9},
       "swap_groups": [{"name": "GPIO", "pins": ["IO1", "IO2"], "why": "bank", "p": 1}]}


def test_verify_keeps_page_backed_facts_and_rejects_the_rest():
    d = DO.verify(RAW, PAGES)
    assert [p["name"] for p in d["pins"]] == ["VIN", "GND", "EN", "SW"]              # BOGUS is not on its page
    assert len(d["supplies"]) == 1 and d["supplies"][0]["max"] == 17           # 40 V is not printed on p2
    assert "crystal" in d["unknown"] and "crystal" not in d                    # page 9 does not exist
    assert {r["section"] for r in d["rejected"]} == {"pins", "supplies", "crystal"}
    assert "interfaces" in d["unknown"] and d["swap_groups"]


def _project(tmp_path):
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "X.pdf").write_bytes(b"%PDF fake")
    (ds / PD.MANIFEST).write_text(json.dumps({"TPS0000": {"file": "X.pdf", "sha256": "ab" * 32}}))
    return str(ds)


def test_build_all_caches_per_mpn_and_sha_and_elec_reads_it(tmp_path, monkeypatch):
    ds = _project(tmp_path)
    monkeypatch.setattr(DO, "pages", lambda pdf: PAGES)
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return "here:\n" + json.dumps(RAW), 0.01
    res = DO.build_all(ds, ["TPS0000"], ask=ask, log=lambda m: None)
    assert res["TPS0000"][1] == "made" and len(calls) == 1 and "TPS0000" in calls[0] and "=== PAGE 2 ===" in calls[0]
    assert DO.build_all(ds, ["TPS0000"], ask=ask, log=lambda m: None)["TPS0000"][1] == "cached" and len(calls) == 1
    fx = EL.load_facts(ds)["TPS0000"]
    assert fx["supply_pins"] == ["VIN"]                                         # the ground and SW pins of a range row are not supplies
    assert fx["vin_min"] == {"VIN": 3.0} and fx["vin_max"] == {"VIN": 17} and fx["decap_mm"] == {"VIN": 2}
    assert fx["_src"]["vin_max"] == "X.pdf#p2 (dossier)"


def test_hand_fact_wins_over_the_dossier(tmp_path, monkeypatch):
    ds = _project(tmp_path)
    monkeypatch.setattr(DO, "pages", lambda pdf: PAGES)
    DO.build_all(ds, ["TPS0000"], ask=lambda p: (json.dumps(RAW), 0.0), log=lambda m: None)
    man = json.load(open(os.path.join(ds, PD.MANIFEST)))
    man["TPS0000"]["facts"] = {"vin_max": {"value": 6.0, "source": "X.pdf#p2"}}
    open(os.path.join(ds, PD.MANIFEST), "w").write(json.dumps(man))
    fx = EL.load_facts(ds)["TPS0000"]
    assert fx["vin_max"] == 6.0 and "vin_min" not in fx and "supply_pins" not in fx and fx["decap_mm"] == {"VIN": 2}


def test_offline_makes_nothing_and_caches_nothing(tmp_path):
    ds = _project(tmp_path)
    res = DO.build_all(ds, ["TPS0000"], log=lambda m: None)                     # FLUXPLACE_AI=0 (conftest)
    assert res["TPS0000"][0] is None and res["TPS0000"][1].startswith("offline")
    assert DO.load("TPS0000", "ab" * 32) is None


def test_bad_reply_is_a_status_not_a_crash(tmp_path, monkeypatch):
    ds = _project(tmp_path)
    monkeypatch.setattr(DO, "pages", lambda pdf: PAGES)
    res = DO.build_all(ds, ["TPS0000"], ask=lambda p: ("no json here", 0.0), log=lambda m: None)
    assert res["TPS0000"][0] is None and res["TPS0000"][1].startswith("model failed")


def test_select_keeps_first_pages_and_keyword_pages_within_budget():
    pg = ["intro " * 10] * 3 + ["filler " * 100] * 20 + ["Absolute Maximum Ratings " + "x" * 50, "Pin Configuration " + "y" * 50]
    sel = DO.select(pg, budget=600)
    assert sel[:3] == [1, 2, 3] and 24 in sel and 25 in sel and 4 not in sel


def test_pinmap_mismatch_warns_in_spec_check(tmp_path, monkeypatch):
    ds = _project(tmp_path)
    monkeypatch.setattr(DO, "pages", lambda pdf: PAGES)
    DO.build_all(ds, ["TPS0000"], ask=lambda p: (json.dumps(RAW), 0.0), log=lambda m: None)
    monkeypatch.setattr(PD, "evidence", lambda *a, **k: (True, [], [], ""))
    spec = {"components": [{"ref": "U1", "mpn": "TPS0000", "pinmap": {"VIN": "1", "EN": "2", "GND": "5"}, "pinmap_source": "X.pdf#p1"}],
            "nets": {"A": [["U1", "1"]], "B": [["U1", "2"]], "C": [["U1", "5"]]}}
    out = PD.spec_check(spec, ds)
    mm = [f for f in out if f[1] == "PINMAP_DOSSIER_MISMATCH"]
    assert len(mm) == 1 and mm[0][0] == "WARN" and "pad 2: spec EN, datasheet has EN on pad 5" in mm[0][3]
    assert "pad 5: spec GND, datasheet has GND on pad 2" in mm[0][3] and "pad 1" not in mm[0][3]


def test_synonyms_are_not_mismatches():
    dos = {"pins": [{"num": ["7"], "name": "EN/SYNC", "type": "input"}, {"num": ["3"], "name": "OC", "type": "open_drain"},
                    {"num": ["2"], "name": "VEE", "type": "ground"}, {"num": ["4"], "name": "GND", "type": "ground"},
                    {"num": ["8"], "name": "VCC", "type": "power_in"}]}
    assert DO.pinmap_mismatches({"EN": "7", "FLT": "3", "GND": "2", "VCC_IO": "3"}, dos) == []
    assert DO.pinmap_mismatches({"OUT": "4"}, dos) == [("OUT", "4", "datasheet pad 4 is ground (GND)")]
    assert DO.pinmap_mismatches({"GND": "8"}, dos) == [("GND", "8", "datasheet has GND on pad 4")]


def test_drawing_pdf_is_read_from_pictures_and_never_used_downstream(tmp_path, monkeypatch):
    from fluxplace import partcite as PC
    ds = _project(tmp_path)
    monkeypatch.setattr(DO, "pages", lambda pdf: ["", ""])
    monkeypatch.setattr(PC, "page_count", lambda pdf: 2)
    monkeypatch.setattr(PC, "render_page", lambda pdf, k, png: (True, png))
    seen = []
    raw = {"category": "connector", "pins": [{"num": ["1"], "name": "SIG", "type": "passive", "p": 1}],
           "currents": [{"what": "contact", "max": 3, "unit": "A", "p": 2}], "package": {"name": "x", "p": 7}}
    res = DO.build_all(ds, ["TPS0000"], ask=lambda p: (seen.append(p) or json.dumps(raw), 0.0), log=lambda m: None)
    d = res["TPS0000"][0]
    assert d["evidence"] == "vision" and "page-1.png" in seen[0] and d["pins"] and d["currents"]
    assert "package" in d["unknown"]                                            # page 7 is out of range even from pictures
    assert DO.elec_facts(d) == {} and DO.pinmap_mismatches({"GND": "1"}, d) == []
