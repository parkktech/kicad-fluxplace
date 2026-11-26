"""Datasheet-evidence reading: number forms, derived pitch, OCR fallback, pin_alias."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import partdocs as PD
from fluxplace import review as R
from tests.test_partdocs import _pdf

HAVE = shutil.which("pdftotext") is not None


@pytest.mark.parametrize("txt", ["pitch 0.50", "pitch (0.5)", "pitch .5 mm", "Raster 0,5 mm", "0.5mm", "0.500"])
def test_number_forms_accept(txt):
    assert R._num_in_text(txt, 0.5)


@pytest.mark.parametrize("txt", ["pitch 0.55", "1.50 and 10.5", "0.05", "w 0,55 x"])
def test_number_forms_reject(txt):
    assert not R._num_in_text(txt, 0.5)


def test_inch_form():
    assert R._num_in_text("pitch .039", 1.0) and R._num_in_text("0.0394", 1.0)


def test_derived_pitch_needs_printed_pad_and_gap():
    t = "pad 1.45\ngap 2.55\n0.30 0.70"
    assert R._derived_pitch(t, 4.0, [1.45, 3.0]) == "1.45 + 2.55"
    assert R._derived_pitch(t, 4.0, [1.50, 3.0]) is None      # pad width not printed
    assert R._derived_pitch("pad 1.45 gap 2.40", 4.0, [1.45, 3.0]) is None


def _facts(pitch):
    g = [(str(i + 1), (i - 0.5) * pitch, 0.0, 1.45, 1.8) for i in range(2)]
    return {"parts": {"L1": {"footprint": "Drawn", "pad_geom": g, "mech": False}}}


def _lp_run(tmp_path, lines, pitch=4.0):
    ds = tmp_path / "ds"
    ds.mkdir(exist_ok=True)
    _pdf(str(ds / "L.pdf"), lines)
    spec = {"components": [{"ref": "L1", "landpattern": {"source": "L.pdf#p1", "pitch": pitch,
                                                          "pad": [1.45, 1.8]}}]}
    return {(f["code"], f["level"]) for f in R.check_landpattern(_facts(pitch), spec, str(ds))}


FILL = ["a 0.10", "b 0.20", "c 0.30", "d 0.40"]


@pytest.mark.skipif(not HAVE, reason="pdftotext not installed")
def test_landpattern_derived_and_plain(tmp_path):
    c = _lp_run(tmp_path, FILL + ["pad 1.45", "gap 2.55", "pad h 1.80"])
    assert ("LANDPATTERN_PITCH_DERIVED", "INFO") in c and not any(x[0] == "LANDPATTERN_NOT_ON_PAGE" for x in c)
    c = _lp_run(tmp_path, FILL + ["pad 1.45", "gap 2.30", "pad h 1.80"])
    assert ("LANDPATTERN_NOT_ON_PAGE", "FAIL") in c
    c = _lp_run(tmp_path, FILL + ["pitch (4.0) mm", "pad 1.45"])
    assert not any(x[0].startswith("LANDPATTERN_") and x[1] != "INFO" for x in c)


@pytest.mark.skipif(not HAVE, reason="pdftotext not installed")
def test_landpattern_ocr_fallback(tmp_path, monkeypatch):
    # page has too little text for pdftotext; the (faked) OCR carries the dimensions
    monkeypatch.setattr(PD, "ocr_text", lambda pdf, pages: "pitch 4.00\n1.00 2.00 3.00 5.00 6.00")
    c = _lp_run(tmp_path, ["drawing"])
    assert ("LANDPATTERN_PITCH_OCR", "INFO") in c
    monkeypatch.setattr(PD, "ocr_text", lambda pdf, pages: None)
    c = _lp_run(tmp_path, ["drawing"])
    assert ("LANDPATTERN_PAGE_UNREADABLE", "WARN") in c


def test_ocr_text_none_without_tesseract(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    PD._OCR_CACHE.clear()
    assert PD.ocr_text(str(tmp_path / "x.pdf"), [1]) is None


@pytest.mark.skipif(not HAVE, reason="pdftotext not installed")
def test_pinmap_ocr_and_alias(tmp_path, monkeypatch):
    pdf = str(tmp_path / "S.pdf")
    _pdf(pdf, ["RF connector drawing", "1 SIGNAL", "shell / body  ground"] + ["x"] * 3)
    pm = {"SIG": "1", "GND": "2"}
    ok, found, missing, d = PD.evidence({"SIGNAL": "1", "GND": "2"}, pdf, [1])
    assert ok is True or "GND" in found           # 'ground' is the GND alias already
    ok, found, missing, d = PD.evidence({"SIGNAL": "1", "SHIELD": "2"}, pdf, [1])
    assert not ok and missing == ["SHIELD"]
    ok, found, missing, d = PD.evidence({"SIGNAL": "1", "SHIELD": "2"}, pdf, [1],
        pin_alias={"SHIELD": {"datasheet": "shell / body", "page": "S.pdf#p1"}}, ds_dir=str(tmp_path))
    assert ok and not missing and "alias SHIELD" in d
    ok, *_ = PD.evidence({"SIGNAL": "1", "SHIELD": "2"}, pdf, [1],
        pin_alias={"SHIELD": {"datasheet": "chassis", "page": "S.pdf#p1"}}, ds_dir=str(tmp_path))
    assert not ok
    # OCR supplies names pdftotext cannot see
    _pdf(pdf, ["fig"])
    monkeypatch.setattr(PD, "ocr_text", lambda p, pg: "pin 1 VBUS pin 2 SCL pin 3 SDA and more text here ok")
    ok, found, missing, d = PD.evidence({"VBUS": "1", "SCL": "2", "SDA": "3"}, pdf, [1])
    assert ok and "ocr" in d
