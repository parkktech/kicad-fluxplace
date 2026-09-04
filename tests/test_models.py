"""Tests for fluxplace.models — offline parts only (no network, no pcbnew)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import models as M


def test_resolve_vars_kiprjmod(tmp_path):
    f = tmp_path / "x.step"
    f.write_bytes(b"ISO-10303-21;")
    p = M.resolve_vars("${KIPRJMOD}/x.step", str(tmp_path))
    assert os.path.exists(p)


def test_resolve_vars_unset_never_matches(tmp_path):
    p = M.resolve_vars("${NO_SUCH_VAR_XYZ}/x.step", str(tmp_path))
    assert not os.path.exists(p)


def test_fetch_step_cached(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    (d / "MPN-1.step").write_bytes(b"ISO-10303-21;" + b"x" * 2000)
    path, status = M.fetch_step("MPN-1", d, creds={}, tok=None)
    assert status == "cached" and path.name == "MPN-1.step"


def test_credentials_shape():
    creds = M.credentials()
    assert isinstance(creds, dict)
    for v in creds.values():
        assert isinstance(v, str) and v


if __name__ == "__main__":
    import tempfile
    import pathlib
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as td:
                    fn(pathlib.Path(td))
            else:
                fn()
            print(name, "OK")


STEP_FIXTURE = b"""ISO-10303-21;
HEADER; FILE_DESCRIPTION(('x'),'2;1'); ENDSEC;
DATA;
#1=SI_UNIT(.MILLI.,.METRE.);
#10=CARTESIAN_POINT('',(0.,0.,0.));
#11=CARTESIAN_POINT('',(40.,10.,3.));
#12=CARTESIAN_POINT('',(20.,5.,1.5));
ENDSEC; END-ISO-10303-21;
"""


def test_step_bbox(tmp_path):
    f = tmp_path / "m.step"
    f.write_bytes(STEP_FIXTURE)
    lo, hi = M.step_bbox(f)
    assert lo == (0.0, 0.0, 0.0) and hi == (40.0, 10.0, 3.0)


def test_plan_alignment_centers_and_floors(tmp_path):
    f = tmp_path / "m.step"
    f.write_bytes(STEP_FIXTURE)
    plan = M.plan_alignment(f, fp_w=42.0, fp_h=12.0)   # landscape/landscape
    assert plan["rotation"] == (0.0, 0.0, 0.0)
    assert plan["offset"] == (-20.0, 5.0, 0.0)         # centered, floored


def test_plan_alignment_rotates_mismatched_aspect(tmp_path):
    f = tmp_path / "m.step"
    f.write_bytes(STEP_FIXTURE)                        # model is landscape
    plan = M.plan_alignment(f, fp_w=12.0, fp_h=42.0)   # footprint portrait
    assert plan["rotation"][2] == 90.0


# ------------------------------------------------------- missing_models / --fetch
# Needs pcbnew (skipped where it is not importable). No network: lcsc.lookup
# and easyeda_model are monkeypatched.
import json as _json  # noqa: E402

import pytest  # noqa: E402

pcbnew = pytest.importorskip("pcbnew")
from fluxplace import lcsc as LCSC  # noqa: E402


def _footprint(board, ref, net=None, model=None):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    p = pcbnew.PAD(fp)
    p.SetNumber("1")
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetSize(pcbnew.VECTOR2I(int(0.6e6), int(0.6e6)))
    if net is not None:
        p.SetNet(net)
    fp.Add(p)
    if model:
        m = pcbnew.FP_3DMODEL()
        m.m_Filename = model
        fp.Models().append(m)
    board.Add(fp)
    return fp


def test_missing_models_skips_mechanical_finds_broken_and_absent(tmp_path):
    board = pcbnew.BOARD()
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    _footprint(board, "U1", net=sig, model="${KIPRJMOD}/no_such.step")  # broken
    _footprint(board, "U2", net=sig, model=None)                        # no model
    _footprint(board, "MH1", net=None, model=None)                     # mechanical

    board_path = str(tmp_path / "b.kicad_pcb")
    todo = {ref: reason for ref, _fp_name, reason in
           M.missing_models(board, board_path=board_path)}
    assert todo == {"U1": "broken-path", "U2": "no-model"}


def test_fetch_missing_attaches_and_records_provenance(tmp_path, monkeypatch):
    board_dir = tmp_path / "hardware" / "utv-comms"
    board_dir.mkdir(parents=True)
    board_path = board_dir / "b.kicad_pcb"
    lib_dir = tmp_path / "hardware" / "lib" / "3dmodels"   # <board_dir>/../lib/3dmodels

    board = pcbnew.BOARD()
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    _footprint(board, "U1", net=sig, model=None)
    pcbnew.SaveBoard(str(board_path), board)

    monkeypatch.setattr(LCSC, "lookup",
                        lambda mpn, **k: {"lcsc": "C123456", "brand": "Acme"})
    fetched = tmp_path / "fetched.step"
    fetched.write_bytes(b"ISO-10303-21;" + b"x" * 2000)
    monkeypatch.setattr(M, "easyeda_model",
                        lambda code, out_dir, log=print: fetched)

    sources_json = tmp_path / "model_sources.json"
    rep = M.fetch_missing(str(board_path), {"U1": "ACME-1"}, str(lib_dir),
                          str(sources_json), log=lambda m: None)

    assert rep["unresolved"] == []
    assert [r for r, _mpn, _f in rep["attached"]] == ["U1"]

    saved = pcbnew.LoadBoard(str(board_path))
    fp = next(f for f in saved.GetFootprints() if f.GetReference() == "U1")
    models = list(fp.Models())
    assert len(models) == 1
    assert models[0].m_Filename.startswith("${KIPRJMOD}/../lib/3dmodels/")

    provenance = _json.loads(sources_json.read_text())
    assert provenance["real"]["U1"].startswith("ACME-1")
    assert "C123456" in provenance["real"]["U1"]


def test_fetch_missing_absolute_path_when_lib_dir_is_nonstandard(tmp_path,
                                                                monkeypatch):
    board_dir = tmp_path / "hardware" / "utv-comms"
    board_dir.mkdir(parents=True)
    board_path = board_dir / "b.kicad_pcb"
    lib_dir = tmp_path / "elsewhere" / "3dmodels"    # NOT <board_dir>/../lib/3dmodels

    board = pcbnew.BOARD()
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    _footprint(board, "U1", net=sig, model=None)
    pcbnew.SaveBoard(str(board_path), board)

    monkeypatch.setattr(LCSC, "lookup",
                        lambda mpn, **k: {"lcsc": "C1", "brand": "Acme"})
    fetched = tmp_path / "fetched.step"
    fetched.write_bytes(b"ISO-10303-21;" + b"x" * 2000)
    monkeypatch.setattr(M, "easyeda_model",
                        lambda code, out_dir, log=print: fetched)

    sources_json = tmp_path / "model_sources.json"
    M.fetch_missing(str(board_path), {"U1": "ACME-1"}, str(lib_dir),
                    str(sources_json), log=lambda m: None)

    saved = pcbnew.LoadBoard(str(board_path))
    fp = next(f for f in saved.GetFootprints() if f.GetReference() == "U1")
    assert list(fp.Models())[0].m_Filename == str(lib_dir / "Acme_ACME-1.step")


def test_fetch_missing_no_mpn_is_unresolved(tmp_path):
    board_dir = tmp_path / "hardware" / "utv-comms"
    board_dir.mkdir(parents=True)
    board_path = board_dir / "b.kicad_pcb"
    board = pcbnew.BOARD()
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    _footprint(board, "U1", net=sig, model=None)
    pcbnew.SaveBoard(str(board_path), board)

    rep = M.fetch_missing(str(board_path), {}, str(tmp_path / "lib"),
                          str(tmp_path / "model_sources.json"),
                          log=lambda m: None)
    assert rep["attached"] == []
    assert rep["unresolved"] == [("U1", "no MPN mapping")]
