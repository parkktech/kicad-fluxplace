"""AI component fetch: datasheet source chain, drop-in equivalents, substitution apply, page render and pin-table citation, model chain.
Fixtures only: no network (the transport is stubbed), no credentials."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import dropin as DI, dsfetch as D, modelchain as MC, partcite as PC, partsgate as PG, parts as P  # noqa: E402


def make_pdf(path, text, pad=9000):
    """A minimal valid one-page PDF whose page text is `text` (padded past the size floor)."""
    lines = "".join(f"BT /F1 9 Tf 20 {780 - 11 * i} Td ({l}) Tj ET\n" for i, l in enumerate(text.split("\n")))
    stream = lines.encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n%" + b"x" * pad + b"\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    open(path, "wb").write(out)
    return path


FILLER = "\n".join(["General description and electrical characteristics of the device family, absolute maximum ratings, ordering notes."] * 6)


@pytest.fixture
def web(monkeypatch, tmp_path):
    """Stub D.get: url -> (status, bytes).  Unknown urls 404.  Records every request."""
    pages, calls = {}, []

    def get(url, timeout=60, headers=None):
        calls.append(url)
        st, data = pages.get(url, (404, b""))
        return st, "application/pdf", data
    monkeypatch.setattr(D, "get", get)
    return pages, calls


def pdf_bytes(tmp_path, text, name="x.pdf"):
    return open(make_pdf(str(tmp_path / name), text), "rb").read()


# ------------------------------------------------------------------ datasheet chain
def test_validate_rejects_wrong_generic_small_and_non_pdf(tmp_path):
    good = make_pdf(str(tmp_path / "g.pdf"), "ABC1234XYZ pin configuration\n" + FILLER)
    assert D.validate_pdf(good, "ABC1234XYZ")[0] == "part"
    assert D.validate_pdf(good, "ABC1234XYZR")[0] == "part"                       # packaging suffix on our MPN: base code is in the text
    lvl, why = D.validate_pdf(good, "QQQ9999ZZZ")
    assert lvl is None and "wrong or generic" in why
    ser = make_pdf(str(tmp_path / "s.pdf"), "ABC1234 series datasheet\n" + FILLER)
    assert D.validate_pdf(ser, "ABC1234XYZ")[0] == "series"
    html = tmp_path / "h.pdf"
    html.write_bytes(b"<!DOCTYPE html>" + b"x" * 20000)
    assert "not a PDF" in D.validate_pdf(str(html), "ABC1234XYZ")[1]
    tiny = tmp_path / "t.pdf"
    tiny.write_bytes(b"%PDF-1.4 tiny")
    assert "too small" in D.validate_pdf(str(tiny), "ABC1234XYZ")[1]


def test_chain_falls_back_in_order_and_records_every_attempt(web, tmp_path):
    pages, calls = web
    good = pdf_bytes(tmp_path, "ABC1234XYZ pin configuration\n" + FILLER)
    pages["https://a/blocked.pdf"] = (403, b"")
    pages["https://b/html.pdf"] = (200, b"<!DOCTYPE html>" + b"x" * 20000)
    pages["https://c/wrong.pdf"] = (200, pdf_bytes(tmp_path, "OTHER9999 datasheet\n" + FILLER))
    pages["https://d/ok.pdf"] = (200, good)
    srcs = [("distributor", lambda m, c: [("digikey", "https://a/blocked.pdf"), ("mouser", "https://b/html.pdf")]),
            ("element14", lambda m, c: (_ for _ in ()).throw(LookupError("skipped: no ELEMENT14_API_KEY"))),
            ("lcsc", lambda m, c: [("lcsc", "https://c/wrong.pdf"), ("lcsc", "https://d/ok.pdf")]),
            ("manufacturer", lambda m, c: [("manufacturer", "https://never/asked.pdf")])]
    ds = str(tmp_path / "ds")
    r = D.fetch_datasheet("ABC1234XYZ", ds, creds={}, sources=srcs)
    assert r["ok"] and r["level"] == "part" and r["source"] == "lcsc" and r["file"] == "ABC1234XYZ.pdf"
    out = [t["outcome"] for t in r["tried"]]
    assert out[0] == "skipped: no ELEMENT14_API_KEY" or "skipped" in " ".join(out)
    assert any(o == "HTTP 403" for o in out) and any("not a PDF" in o for o in out) and any("wrong or generic" in o for o in out)
    assert "https://never/asked.pdf" not in calls                                     # stopped at the first valid file
    ent = json.load(open(os.path.join(ds, "datasheets.json")))["ABC1234XYZ"]
    assert ent["level"] == "part" and ent["via"] == "lcsc" and ent["sha256"]
    n = len(calls)
    r2 = D.fetch_datasheet("ABC1234XYZ", ds, creds={}, sources=srcs)                  # cached: nothing downloaded again
    assert r2["ok"] and r2["cached"] and len(calls) == n


def test_series_is_a_last_resort_and_flagged(web, tmp_path):
    pages, _ = web
    pages["https://s/series.pdf"] = (200, pdf_bytes(tmp_path, "ABC1234 family datasheet\n" + FILLER))
    srcs = [("lcsc", lambda m, c: [("lcsc", "https://s/series.pdf")])]
    r = D.fetch_datasheet("ABC1234XYZ", str(tmp_path / "ds"), creds={}, sources=srcs)
    assert r["ok"] and r["level"] == "series"
    assert json.load(open(tmp_path / "ds" / "datasheets.json"))["ABC1234XYZ"]["level"] == "series"


def test_failure_names_what_was_tried_and_is_negative_cached(web, tmp_path):
    pages, calls = web
    srcs = [("distributor", lambda m, c: [("digikey", "https://a/blocked.pdf")]), ("lcsc", lambda m, c: [])]
    ds = str(tmp_path / "ds")
    r = D.fetch_datasheet("ABC1234XYZ", ds, creds={}, sources=srcs)
    assert not r["ok"] and {"HTTP 404", "no datasheet URL offered"} <= {t["outcome"] for t in r["tried"]}
    n = len(calls)
    assert D.fetch_datasheet("ABC1234XYZ", ds, creds={}, sources=srcs)["cached"] and len(calls) == n
    D.fetch_datasheet("ABC1234XYZ", ds, creds={}, sources=srcs, refresh=True)
    assert len(calls) == n + 1


def test_packaging_variants_are_tried_at_the_distributor():
    assert D.packaging_variants("GCM155R71C104KA55D") == ["GCM155R71C104KA55D", "GCM155R71C104KA55"]
    assert D.packaging_variants("ADS7138IRTER")[1] == "ADS7138IRTET"


# ------------------------------------------------------------------ drop-in rules
def test_packaging_equivalence():
    assert DI.packaging_equivalent("ADS7138IRTER", "ADS7138IRTET")
    assert DI.packaging_equivalent("GCM155R71C104KA55D", "GCM155R71C104KA55J")
    assert DI.packaging_equivalent("RC0603FR-0710KL", "RC0603FR-0710KL")
    assert DI.packaging_equivalent("BSS138Q-7-F", "BSS138Q-7")                        # one extends the other by a short tail
    assert not DI.packaging_equivalent("ISOW1044DFMR", "ISOW1042DFMR")
    assert not DI.packaging_equivalent("GCM155R71C104KA55D", "GCM155R71C105KA55D")


def test_parse_quantity():
    assert DI.parse_quantity("100nF") == pytest.approx((1e-7, "F"))
    assert DI.parse_quantity("±10%") == (10.0, "%")
    assert DI.parse_quantity("20 mOhm")[0] == pytest.approx(0.02)
    assert DI.parse_quantity("X7R") is None


ORIG = {"mpn": "CAP-A", "package": "0402 (1005 Metric)", "pins": None, "temp_min": -55, "temp_max": 125, "lifecycle": "Active",
        "params": {"Capacitance": "0.1 µF", "Voltage - Rated": "16V", "Tolerance": "±10%", "Temperature Coefficient": "X7R",
                   "Ratings": "AEC-Q200"}}
FP = "Capacitor_SMD:C_0402_1005Metric"
DS_OK = {"level": "part", "text": "CAP-B 0402 1005 metric size code"}


def cand(**kw):
    c = {"mpn": "CAP-B", "package": "0402 (1005 Metric)", "pins": None, "temp_min": -55, "temp_max": 125, "lifecycle": "Active", "stock": 5000,
         "params": dict(ORIG["params"], **{"Voltage - Rated": "25V"})}
    c.update(kw)
    return c


def test_drop_in_when_everything_is_verified():
    r = DI.classify(ORIG, cand(), FP, 400, (-40, 85), DS_OK)
    assert r["drop_in"], r
    assert r["checks"]["parameters"][0] is True


@pytest.mark.parametrize("mod,word", [
    ({"stock": 100}, "stock"),
    ({"package": "0603 (1608 Metric)"}, "footprint"),
    ({"temp_max": 85}, "temperature"),
    ({"lifecycle": "Obsolete"}, "lifecycle"),
    ({"params": dict(ORIG["params"], Capacitance="0.22 µF")}, "Capacitance"),                # value differs
    ({"params": dict(ORIG["params"], **{"Voltage - Rated": "10V"})}, "Voltage"),            # rating lower
    ({"params": dict(ORIG["params"], Tolerance="±20%")}, "Tolerance"),                      # tolerance worse
    ({"params": dict(ORIG["params"], **{"Temperature Coefficient": "X5R"})}, "Temperature Coefficient"),
    ({"params": dict(ORIG["params"], Ratings="-")}, "qualification"),                       # AEC-Q lost
])
def test_not_drop_in_when_anything_differs(mod, word):
    r = DI.classify(ORIG, cand(**mod), FP, 400, (-40, 85), DS_OK)
    assert not r["drop_in"] and any(word in x for x in r["differs"]), r


def test_unverified_is_not_drop_in():
    no_ds = DI.classify(ORIG, cand(), FP, 400, (-40, 85), None)
    assert not no_ds["drop_in"] and any("datasheet" in x for x in no_ds["unverified"])
    series = DI.classify(ORIG, cand(), FP, 400, (-40, 85), {"level": "series", "text": ""})
    assert not series["drop_in"]
    no_t = DI.classify(ORIG, cand(temp_min=None, temp_max=None), FP, 400, (-40, 85), DS_OK)
    assert not no_t["drop_in"] and any("temperature" in x for x in no_t["unverified"])
    nothing = DI.classify({**ORIG, "params": {}}, cand(params={}), FP, 400, (-40, 85), DS_OK)
    assert not nothing["drop_in"] and any("parameter" in x for x in nothing["unverified"])


def test_pin_count_and_resistance_direction():
    o = {"mpn": "U-A", "package": "SOT-23-5", "pins": 5, "temp_min": -40, "temp_max": 125, "params": {"Rds On (Max)": "2 Ohm"}}
    ok = DI.classify(o, {**o, "mpn": "U-B", "stock": 900, "params": {"Rds On (Max)": "1.5 Ohm"}}, "SOT-23-5", 100, (None, None),
                     {"level": "part", "text": "SOT-23-5 package"})
    assert ok["drop_in"], ok
    bad = DI.classify(o, {**o, "mpn": "U-B", "stock": 900, "pins": 6, "params": {"Rds On (Max)": "1.5 Ohm"}}, "SOT-23-5", 100, (None, None),
                      {"level": "part", "text": "SOT-23-5"})
    assert not bad["drop_in"] and any("pins" in x for x in bad["differs"])
    worse = DI.classify(o, {**o, "mpn": "U-B", "stock": 900, "params": {"Rds On (Max)": "3 Ohm"}}, "SOT-23-5", 100, (None, None),
                        {"level": "part", "text": "SOT-23-5"})
    assert not worse["drop_in"]


def test_rank_puts_verified_first():
    a = {"mpn": "A", "drop_in": False, "differs": ["x"], "unverified": [], "ext": 1}
    b = {"mpn": "B", "drop_in": True, "differs": [], "unverified": [], "ext": 9}
    c = {"mpn": "C", "drop_in": False, "differs": [], "unverified": ["y"], "ext": 0.1}
    assert [x["mpn"] for x in DI.rank([a, c, b])] == ["B", "C", "A"]


# ------------------------------------------------------------------ alternatives in todo, substitution in apply
def _spec():
    return {"components": [{"ref": "C1", "value": "0.1uF", "lib": "L", "fp": "C_0402_1005Metric", "mpn": "CAP-A", "mfr": "ACME",
                            "package": "0402"}], "nets": {}}


def _row(mpn, **kw):
    return {"matched": mpn, "manufacturer": "ACME", "package": "0402 (1005 Metric)", "temp_min": -55, "temp_max": 125, "lifecycle": "Active",
            "description": "CAP CER 0.1UF 16V X7R 0402", "datasheet": "", "raw_params": dict(ORIG["params"]), **kw}


def test_alternatives_rank_and_only_verified_are_drop_in():
    searcher = lambda q, limit=10, qty=1: [  # noqa: E731
        {"mpn": "CAP-B", "mfr": "X", "stock": 9000, "ext": 5, "source": "digikey", "package": "0402 (1005 Metric)", "lifecycle": "Active",
         "temp_min": -55, "temp_max": 125, "params": dict(ORIG["params"], **{"Voltage - Rated": "25V"})},
        {"mpn": "CAP-C", "mfr": "X", "stock": 9000, "ext": 3, "source": "digikey", "package": "0603 (1608 Metric)", "lifecycle": "Active",
         "temp_min": -55, "temp_max": 125, "params": dict(ORIG["params"])},
        {"mpn": "CAP-A-REEL", "mfr": "ACME", "stock": 9000, "ext": 4, "source": "mouser", "package": "0402 (1005 Metric)",
         "lifecycle": "Active", "temp_min": -55, "temp_max": 125, "params": dict(ORIG["params"])}]
    ver = lambda mpn, url=None: {"level": "part", "text": mpn + " 0402 1005", "file": mpn + ".pdf"}  # noqa: E731
    alts = PG.alternatives("CAP-A", _row("CAP-A"), "Capacitor_SMD:C_0402_1005Metric", 400, (-40, 85), searcher, ver)
    by = {a["mpn"]: a for a in alts}
    assert by["CAP-B"]["drop_in"] and not by["CAP-C"]["drop_in"] and by["CAP-A-REEL"]["identical"]
    assert alts[0]["drop_in"] and any("footprint" in x for x in by["CAP-C"]["differs"])
    nover = PG.alternatives("CAP-A", _row("CAP-A"), "Capacitor_SMD:C_0402_1005Metric", 400, (-40, 85), searcher, None)
    assert not any(a["drop_in"] for a in nover if not a["identical"])                  # no datasheet check, no drop_in


def _world(tmp_path):
    sp, mp = tmp_path / "spec.json", tmp_path / "mpn_map.json"
    sp.write_text(json.dumps(_spec(), indent=1))
    mp.write_text(json.dumps({"C1": "CAP-A"}))
    rows = {"CAP-A": _row("CAP-A"), "CAP-B": _row("CAP-B", raw_params=dict(ORIG["params"], **{"Voltage - Rated": "25V"})),
            "CAP-D": _row("CAP-D", raw_params=dict(ORIG["params"], Capacitance="0.22 µF")), "CAP-A-REEL": _row("CAP-A-REEL")}
    return sp, mp, rows


def _apply(tmp_path, props, verdicts):
    sp, mp, rows = _world(tmp_path)
    ds = tmp_path / "ds"
    ds.mkdir(exist_ok=True)
    for m in rows:
        (ds / (m + ".pdf")).write_bytes(b"%PDF")
    facts = {"board_path": "/x/b.kicad_pcb", "parts": {"C1": {"footprint": "C_0402_1005Metric", "models": ["${KIPRJMOD}/m.step"], "pads_total": 2}}}
    return PG.apply(str(sp), str(mp), props, {}, facts, str(ds), rows.get, lambda m, r: {"verdict": "OK", "stock": 9000},
                    model_ok=lambda r, c, p: (True, ""), ds_ok=lambda m, p: True, dropin=verdicts), sp, mp


def test_apply_substitution_needs_a_verified_drop_in_and_is_recorded(tmp_path):
    def dropin(old, new, refs):
        return DI.classify(DI.row_for(_row(old), old), DI.row_for(_row(new, raw_params=_world(tmp_path)[2][new]["raw_params"]), new) | {"stock": 9000},
                           "C_0402_1005Metric", 400, (-40, 85), {"level": "part", "text": new + " 0402 1005"})
    rep, sp, mp = _apply(tmp_path, [{"ref": "C1", "mpn": "CAP-B"}], dropin)
    assert [x["what"] for x in rep["applied"]] == ["C1 CAP-B"], rep
    assert rep["applied"][0]["replaces"]["from"] == "CAP-A"
    assert json.load(open(mp))["C1"] == "CAP-B"
    log = json.load(open(tmp_path / "parts-substitutions.json"))
    assert log[0]["from"] == "CAP-A" and log[0]["to"] == "CAP-B" and log[0]["evidence"]["parameters"] and log[0]["refs"] == ["C1"]


def test_apply_substitution_that_changes_value_is_owner_and_unwritten(tmp_path):
    def dropin(old, new, refs):
        return DI.classify(DI.row_for(_row(old), old), DI.row_for(_row(new, raw_params=_world(tmp_path)[2][new]["raw_params"]), new) | {"stock": 9000},
                           "C_0402_1005Metric", 400, (-40, 85), {"level": "part", "text": new + " 0402 1005"})
    rep, sp, mp = _apply(tmp_path, [{"ref": "C1", "mpn": "CAP-D"}], dropin)
    assert not rep["applied"] and rep["owner"] and "Capacitance" in rep["owner"][0]["reasons"][0]
    assert json.load(open(mp))["C1"] == "CAP-A" and not (tmp_path / "parts-substitutions.json").exists()
    rep, _, _ = _apply(tmp_path, [{"ref": "C1", "mpn": "CAP-B"}], None)                    # no verifier: never auto-applied
    assert rep["owner"] and not rep["applied"]


def test_apply_packaging_variant_is_not_a_substitution(tmp_path):
    rep, sp, mp = _apply(tmp_path, [{"ref": "C1", "mpn": "CAP-A-REEL"}], None)
    assert rep["applied"] and "replaces" not in rep["applied"][0] and not (tmp_path / "parts-substitutions.json").exists()


# ------------------------------------------------------------------ page render + pin-table citation
def test_render_page_png(tmp_path):
    pdf = make_pdf(str(tmp_path / "d.pdf"), "pin table\n" + FILLER)
    assert PC.page_count(pdf) == 1
    ok, why = PC.render_page(pdf, 1, str(tmp_path / "p.png"))
    assert ok and open(tmp_path / "p.png", "rb").read(8)[1:4] == b"PNG", why
    assert not PC.render_page(pdf, 2, str(tmp_path / "q.png"))[0]


def _cspec():
    return {"components": [{"ref": "J1", "value": "CONN", "fp": "F", "landpattern": {"pins": 3}},
                           {"ref": "U1", "value": "IC", "fp": "F", "pinmap": {"VIN": "1", "GND": "2", "OUT": "3"}}],
            "nets": {"N1": [["J1", "1"], ["J1", "2"], ["J1", "MP"], ["U1", "1"]], "N2": [["J1", "3"], ["U1", "2"]]}}


def test_cite_matches_and_flags_mismatches(tmp_path):
    ok = PC.compare(_cspec(), "J1", {"pins": {"1": "A", "2": "B", "3": "C", "MP": "mount"}}, pad_count=3)
    assert ok["ok"], ok
    bad = PC.compare(_cspec(), "J1", {"pins": {"1": "A", "2": "B"}}, pad_count=3)
    assert not bad["ok"] and any("pin 3" in f for f in bad["findings"]) and any("signal pads" in f for f in bad["findings"])
    pm = PC.compare(_cspec(), "U1", {"pins": {"1": "VIN", "2": "OUT", "3": "GND"}})
    assert not pm["ok"] and any("pinmap GND->2" in f for f in pm["findings"])
    good = PC.compare(_cspec(), "U1", {"pins": {"1": "VIN", "2": "GND", "3": "OUT"}})
    assert good["ok"]
    spec = _cspec()
    assert PC.set_source(spec, "U1", "U1.pdf", 4) and spec["components"][1]["pinmap_source"] == "U1.pdf#p4"
    assert PC.set_source(spec, "J1", "J1.pdf", 2) and "pinmap" not in spec["components"][0]            # a source, never an invented pinmap
    assert not PC.set_source(spec, "NOPE", "x.pdf", 1)
    p = PC.record(str(tmp_path), "J1", "J-1", "J1.pdf", 2, {"pins": {"1": "A"}}, bad)
    assert json.load(open(p))["J1"]["page"] == 2 and json.load(open(p))["J1"]["findings"]


# ------------------------------------------------------------------ model chain
STEP = ("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\n#2=CARTESIAN_POINT('',(%g,%g,%g));\nENDSEC;\nEND-ISO-10303-21;\n")


def test_validate_step_and_plausibility(tmp_path):
    f = tmp_path / "m.step"
    f.write_text(STEP % (1.0, 0.5, 0.5))
    ok, why, bb = MC.validate_step(str(f))
    assert ok and bb == (1.0, 0.5, 0.5)
    assert MC.plausible(bb, (1.2, 0.7))[0] is True
    assert MC.plausible(bb, (10, 8))[0] is False                                          # a 1 mm body on a 10 mm footprint
    assert MC.plausible(bb, None)[0] is None
    bad = tmp_path / "n.step"
    bad.write_text("<html>not step</html>")
    assert not MC.validate_step(str(bad))[0]


def test_model_chain_records_tries_and_never_fakes(tmp_path):
    good = tmp_path / "good.step"
    good.write_text(STEP % (1.0, 0.5, 0.5))
    wrong = tmp_path / "wrong.step"
    wrong.write_text(STEP % (30, 20, 5))
    srcs = [("project-library", lambda m, c: (None, "not in the project library")),
            ("digikey-media", lambda m, c: (str(wrong), "fetched")),
            ("easyeda-lcsc", lambda m, c: (_ for _ in ()).throw(RuntimeError("HTTP 403"))),
            ("manufacturer", lambda m, c: (str(good), "download"))]
    r = MC.resolve("X1", str(tmp_path / "lib"), creds={}, fp_size=(1.2, 0.7), sources=srcs)
    assert r["ok"] and r["source"] == "manufacturer"
    assert [t["source"] for t in r["tried"]] == ["project-library", "digikey-media", "easyeda-lcsc", "manufacturer"]
    assert "rejected" in r["tried"][1]["outcome"] and "RuntimeError" in r["tried"][2]["outcome"]
    none = MC.resolve("X1", str(tmp_path / "lib"), creds={}, fp_size=(1.2, 0.7), sources=srcs[:3])
    assert not none["ok"] and none["path"] is None and len(none["tried"]) == 3


def test_every_http_call_in_the_new_modules_goes_through_redact():
    import re
    for mod in ("dsfetch", "modelchain", "partcite", "dropin"):
        txt = open(os.path.join(os.path.dirname(__file__), "..", "fluxplace", mod + ".py")).read()
        assert not re.search(r"urllib\.request\.urlopen|requests\.get|curl_cffi", txt.replace("no browser impersonation", "")), mod


def test_pincount_gate_accepts_a_pin_drawn_as_several_copper_pieces():
    spec = {"components": [{"ref": "U1", "value": "IC", "fp": "VQFN-HR-14", "lib": "L", "mpn": "ABC123", "mfr": "X", "package": "14-VQFN",
                            "pinmap_source": "ABC123.pdf#p1"}], "nets": {"N": [["U1", "1"]]}}
    for total, names, bad in ((18, 14, False), (18, 18, True)):
        facts = {"board_path": "/x/b.kicad_pcb", "parts": {"U1": {"footprint": "VQFN-HR-14", "models": [], "pads_total": total,
                                                                   "pads": {str(i): "N" for i in range(1, names + 1)}}}}
        res = P.run(spec, {}, facts=facts, mpn_map={}, ds_dir=None, data={"ABC123": {"matched": "ABC123", "manufacturer": "X", "package": "14-VQFN-HR (4x3.5)"}})
        assert bool([f for f in res["findings"] if f["code"] == "PART_PINCOUNT_MISMATCH"]) is bad


def test_a_catalog_already_on_disk_covers_other_parts(web, tmp_path):
    pages, calls = web
    ds = tmp_path / "ds"
    ds.mkdir()
    make_pdf(str(ds / "ABC1234XYZ.pdf"), "ABC1234XYZ and ABC5678QQQ ordering table\n" + FILLER)
    import hashlib
    sha = hashlib.sha256((ds / "ABC1234XYZ.pdf").read_bytes()).hexdigest()
    (ds / "datasheets.json").write_text(json.dumps({"ABC1234XYZ": {"file": "ABC1234XYZ.pdf", "sha256": sha, "source": "x", "level": "part"}}))
    r = D.fetch_datasheet("ABC5678QQQD", str(ds), creds={}, sources=[("lcsc", lambda m, c: [("lcsc", "https://never.pdf")])])
    assert r["ok"] and r["source"] == "local" and r["file"] == "ABC1234XYZ.pdf" and not calls
    assert not D.fetch_datasheet("ZZZ9999QQQ", str(ds), creds={}, sources=[])["ok"]                 # other manufacturer prefix: not scanned
