"""Tests for fluxplace.partdocs — the documentation gate. No network; the
PDF fixture is a hand-written minimal PDF so pdftotext can read it."""
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import partdocs as PD


def _pdf(path, lines):
    """Minimal single-page PDF with the given text lines (Helvetica)."""
    content = "BT /F1 10 Tf 40 750 Td 14 TL " + " ".join(
        "(%s) Tj T*" % l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        "<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += "%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += "xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += "".join("%010d 00000 n \n" % o for o in offs)
    out += "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "w") as fh:
        fh.write(out)


HAVE_PDFTOTEXT = shutil.which("pdftotext") is not None


def test_parse_source_and_aliases():
    assert PD.parse_source("datasheets/X.pdf#p3") == ("datasheets/X.pdf", [3])
    assert PD.parse_source("X.pdf#p2-4") == ("X.pdf", [2, 3, 4])
    assert PD.parse_source("X.pdf") == ("X.pdf", [])
    assert PD.parse_source("Vishay doc 75769 p.1") == (None, [])
    assert "GND" in PD._aliases("PGND") and "VSS" in PD._aliases("GND1")
    assert "D1-" in PD._aliases("D1_N") and "TX+" in PD._aliases("TX_P")


def test_is_passive():
    assert PD.is_passive("R12", "R_0402", 2) and PD.is_passive("C3", "", 2)
    assert not PD.is_passive("U1", "", 8) and not PD.is_passive("D7", "", 5)
    assert PD.is_passive("MH1", "", 1) and not PD.is_passive("J3", "", 2)


@pytest.mark.skipif(not HAVE_PDFTOTEXT, reason="pdftotext not installed")
def test_evidence_reads_the_cited_page():
    d = tempfile.mkdtemp()
    pdf = os.path.join(d, "SP0504.pdf")
    _pdf(pdf, ["SP0504BAHT pin configuration", "1 I/O1  2 GND  3 I/O2", "4 I/O3  5 I/O4"])
    ok, found, missing, detail = PD.evidence({"IO1": "1", "GND": "2", "IO2": "3",
                                              "IO3": "4", "IO4": "5"}, pdf, [1])
    assert ok and not missing, detail
    ok, found, missing, _ = PD.evidence({"VBUS": "1", "SCL": "2"}, pdf, [1])
    assert ok is False and set(missing) == {"VBUS", "SCL"}


@pytest.mark.skipif(not HAVE_PDFTOTEXT, reason="pdftotext not installed")
def test_spec_check_fails_undocumented_parts():
    d = tempfile.mkdtemp()
    _pdf(os.path.join(d, "U9.pdf"), ["TLV7031 pinout: 1 OUT 2 V- 3 IN+ 4 IN- 5 V+"])
    json.dump({"TLV7031DBVR": {"file": "U9.pdf", "sha256": PD._sha(os.path.join(d, "U9.pdf")),
                               "source": "test", "fetched": "2026-09-03"}},
              open(PD.manifest_path(d), "w"))
    spec = {"components": [
        {"ref": "U9", "value": "TLV7031", "fp": "SOT-23-5",
         "pinmap": {"OUT": "1", "GND": "2", "INP": "3", "INN": "4", "VCC": "5"},
         "pinmap_source": "U9.pdf#p1"},
        {"ref": "U7", "value": "TLV7031", "fp": "SOT-23-5",
         "pinmap": {"OUT": "1", "GND": "2"}},            # no datasheet on disk
        {"ref": "U3", "value": "MAX-M10S", "fp": "ublox_MAX"},   # no pinmap, 18 pins
        {"ref": "R1", "value": "10k", "fp": "R_0402"},           # passive, fine
        {"ref": "K1", "value": "relay", "fp": "Relay"},          # no MPN at all
    ], "nets": {"A": [["U9", "1"], ["U7", "1"], ["R1", "1"]] + [["U3", str(i)] for i in range(1, 19)],
                "GND": [["U9", "2"], ["U7", "2"], ["R1", "2"], ["K1", "1"], ["K1", "2"], ["K1", "3"]]}}
    mpn = {"U9": "TLV7031DBVR", "U7": "NOPE-1", "U3": "MAX-M10S-00B", "R1": "RC0402"}
    finds = PD.spec_check(spec, d, mpn_map=mpn)
    codes = {(f[1], f[2]) for f in finds if f[0] == "FAIL"}
    assert ("DATASHEET_MISSING", "U7") in codes
    assert ("DATASHEET_MISSING", "U3") in codes and ("PINMAP_MISSING", "U3") in codes
    assert ("MPN_MISSING", "K1") in codes
    assert not any(ref == "U9" and lvl == "FAIL" for lvl, _, ref, _ in finds), finds
    assert not any(ref == "R1" and lvl == "FAIL" for lvl, _, ref, _ in finds)
    # INP/INN alias IN+/IN- ; GND aliases V- ; VCC aliases V+
    ok, found, missing, _ = PD.evidence(spec["components"][0]["pinmap"], os.path.join(d, "U9.pdf"), [1])
    assert ok, missing
