"""3D models at project start (ready): ok / bound now / fetched now / owner action / offline pending / waived. No network: fake resolver."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as G
from fluxplace import modelstart as MS, partsgate as PG, review as R

PADS = ['(pad "1" smd rect (at 0 0 0.0) (size 0.5 0.5) (layers "F.Cu") (net "N"))']


def _project(tmp, parts, models=None):
    """tmp/hardware/{lib/3dmodels, lib/footprints/p.pretty, tools}, tmp/lib -> hardware/lib, board tmp/W1/stage/b.kicad_pcb.
    parts: [(ref, footprint)]; models: {ref: path as written}."""
    lib = tmp / "hardware" / "lib"
    (lib / "3dmodels").mkdir(parents=True)
    (lib / "footprints" / "p.pretty").mkdir(parents=True)
    (tmp / "hardware" / "tools").mkdir()
    os.symlink("hardware/lib", tmp / "lib")
    body = ""
    for i, (ref, name) in enumerate(parts):
        mdl = f'(model "{models[ref]}" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0))) ' if models and ref in models else ""
        body += G.fp(ref, 10 + 5 * i, 10, PADS, name=name).replace('(property "Reference"', mdl + '(property "Reference"', 1)
    stage = tmp / "W1" / "stage"
    stage.mkdir(parents=True)
    return G.board(stage, body), str(lib / "3dmodels")


def _facts(board, mm=None):
    return R.facts_from_board(board, mpn_map=mm or {}, backend="file")


def _step(path):
    open(path, "w").write("x")


def test_bound_resolving_model_is_ok_and_unbound_hole_exempt(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPA")], {"U1": "${KIPRJMOD}/../lib/3dmodels/a.step"})
    _step(os.path.join(lib, "a.step"))
    r = MS.run(b, _facts(b), {}, {})
    assert r["ok"] == ["U1"] and not r["bound"] and not r["owner"] and r["line"].startswith("3D models: 1 ok, 0 bound now, 0 fetched now, 0 need owner action")


def test_binds_from_footprint_library_with_verified_transform_and_stage_depth(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPA"), ("U2", "FPA")])
    _step(os.path.join(lib, "a.step"))
    open(tmp_path / "hardware/lib/footprints/p.pretty/FPA.kicad_mod", "w").write(
        '(footprint "FPA"\n\t(model "${KIPRJMOD}/../lib/3dmodels/a.step"\n\t\t(offset (xyz 1 2 3))\n\t\t(scale (xyz 1 1 1))\n\t\t(rotate (xyz -90 0 270))\n\t)\n)\n')
    r = MS.run(b, _facts(b), {}, {})
    assert [x["ref"] for x in r["bound"]] == ["U1", "U2"] and all(x["verified"] for x in r["bound"]) and not r["flags"]
    f2 = _facts(b)
    assert f2["parts"]["U1"]["models"] == ["${KIPRJMOD}/../lib/3dmodels/a.step"]
    assert PG.model_state("U1", f2)[0] and PG.model_state("U2", f2)[0]
    txt = open(b).read()
    assert "(xyz 1 2 3)" in txt and "(xyz -90 0 270)" in txt and txt.count("(model ") == 2


def test_binds_from_sources_file_row_and_by_mpn_file_flags_unverified(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPB"), ("U2", "FPC")])
    _step(os.path.join(lib, "b.step"))
    _step(os.path.join(lib, "Acme_PART-C1.step"))
    json.dump({"bindings": {"FPB": {"file": "b.step", "offset": [0, 4.75, 0], "rotate": [0, 0, 90], "verified": True}}},
              open(tmp_path / "hardware/tools/model_sources.json", "w"))
    r = MS.run(b, _facts(b), {"U2": "PART-C1"}, {})
    by = {x["ref"]: x for x in r["bound"]}
    assert by["U1"]["verified"] and not by["U2"]["verified"]
    assert any("orientation unverified: render check" in x for x in r["flags"]) and "(xyz 0 4.75 0)" in open(b).read()


def test_fetches_with_fake_resolver_records_provenance_and_binds(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPD")])
    scratch = tmp_path / "dl.step"
    _step(scratch)
    calls = []

    def fake(mpn, lib_dir, size, registry):
        calls.append(mpn)
        return {"ok": True, "path": str(scratch), "source": "digikey-media", "tried": []}
    json.dump({"real": {}}, open(tmp_path / "hardware/tools/model_sources.json", "w"))
    r = MS.run(b, _facts(b), {"U1": "D-1"}, {}, resolver=fake)
    assert calls == ["D-1"] and [x["ref"] for x in r["fetched"]] == ["U1"] and "1 fetched now" in r["line"]
    assert os.path.isfile(os.path.join(lib, "D-1.step"))
    src = json.load(open(tmp_path / "hardware/tools/model_sources.json"))
    assert "digikey-media" in src["real"]["U1"] and src["bindings"]["FPD"]["verified"] is False
    assert PG.model_state("U1", _facts(b))[0]
    assert MS.run(b, _facts(b), {"U1": "D-1"}, {}, resolver=fake)["ok"] == ["U1"]          # second run: nothing to do


def test_nothing_found_is_owner_action_with_page_and_gate_stays_failed(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPE")])
    json.dump({"real": {"U1": "E-9 - vendor page https://vendor.example/cad/E-9 needs sign-in"}}, open(tmp_path / "hardware/tools/model_sources.json", "w"))
    r = MS.run(b, _facts(b), {"U1": "E-9"}, {}, resolver=lambda *a: {"ok": False, "tried": [{"source": "x", "outcome": "sign-in"}]})
    assert [o["ref"] for o in r["owner"]] == ["U1"] and r["owner"][0]["page"] == "https://vendor.example/cad/E-9"
    assert "1 need owner action" in r["line"] and any("OWNER ACTION U1 E-9" in x for x in MS.detail_lines(r))
    assert not PG.model_state("U1", _facts(b))[0]


def test_offline_does_not_fetch_and_lists_pending(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPF")])

    def boom(*a):
        raise AssertionError("network used offline")
    r = MS.run(b, _facts(b), {"U1": "F-1"}, {}, offline=True, resolver=boom)
    assert [p["ref"] for p in r["pending"]] == ["U1"] and not r["owner"] and "fetch pending (offline)" in r["line"]


def test_review_waive_with_reason_passes_the_part(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPG")])
    cons = {"review": {"waive": ["MODEL_MISSING:^U1 "]}}
    r = MS.run(b, _facts(b), {"U1": "G-1"}, cons, offline=True)
    assert r["waived"] == ["U1"] and not r["pending"] and not r["owner"]


def test_ready_reports_the_models_line_and_refuses_bypass(tmp_path):
    from fluxplace import ready as RDY
    b, lib = _project(tmp_path, [("U1", "FPH")])
    spec = {"components": [], "nets": {}}
    r = MS.run(b, _facts(b), {}, {}, offline=False, resolver=lambda *a: {"ok": False, "tried": []})
    assert r["owner"] and r["owner"][0]["page"].startswith("no MPN")
    line = RDY._line("3D models", RDY.FAIL if r["owner"] else RDY.PASS, r["line"], MS.detail_lines(r))
    assert line["status"] == RDY.FAIL


def test_rebind_swapped_binds_a_project_library_copy_for_an_unresolved_library_path(tmp_path):
    stock = "${KICAD10_3DMODEL_DIR}/Connector_Molex.3dshapes/Molex_Micro-Fit_3.0_43650-0315_1x03_P3.00mm_Vertical.step"
    b, lib = _project(tmp_path, [("J3", "Molex_Micro-Fit_3.0_43650-0315_1x03_P3.00mm_Vertical")], {"J3": stock})
    assert R.resolve_model(stock, b) != stock and not os.path.isfile(R.resolve_model(stock, b))
    _step(os.path.join(lib, "MOLEX_43650-0315.step"))
    r = MS.rebind_swapped(b, "Connector_Molex:Molex_Micro-Fit_3.0_43650-0315_1x03_P3.00mm_Vertical", [(stock, (0, 0, 0), (1, 1, 1), (0, 0, 0))])
    assert r["bound"].endswith("MOLEX_43650-0315.step")
    f = _facts(b)
    assert PG.model_state("J3", f)[0] and "${KIPRJMOD}/../lib/3dmodels/MOLEX_43650-0315.step" in open(b).read()


def test_rebind_swapped_leaves_a_resolving_path_and_warns_when_nothing_matches(tmp_path):
    b, lib = _project(tmp_path, [("U1", "FPA")], {"U1": "${KIPRJMOD}/../lib/3dmodels/a.step"})
    _step(os.path.join(lib, "a.step"))
    assert MS.rebind_swapped(b, "FPA", [("${KIPRJMOD}/../lib/3dmodels/a.step", (0, 0, 0), (1, 1, 1), (0, 0, 0))])["bound"] is None
    r = MS.rebind_swapped(b, "FPA", [("${KICAD10_3DMODEL_DIR}/X.3dshapes/zzz.step", (0, 0, 0), (1, 1, 1), (0, 0, 0))])
    assert r["bound"] is None and r["line"].startswith("WARNING")


def test_rebind_swapped_searches_every_library_up_the_tree_not_only_the_nearest(tmp_path):
    """k8: a board inside a delivery folder sees the delivery's partial lib first; the source project's lib holds the file."""
    stock = "${KICAD10_3DMODEL_DIR}/Connector_Molex.3dshapes/Molex_Micro-Fit_3.0_43650-0315_1x03_P3.00mm_Vertical.step"
    src = tmp_path / "src"
    (src / "lib" / "3dmodels").mkdir(parents=True)
    _step(str(src / "lib" / "3dmodels" / "MOLEX_43650-0315.step"))
    (src / "delivery" / "lib" / "3dmodels").mkdir(parents=True)               # nearer, partial
    _step(str(src / "delivery" / "lib" / "3dmodels" / "other.step"))
    b = src / "delivery" / "b.kicad_pcb"
    b.write_text('(kicad_pcb (footprint "Connector_Molex:Molex_Micro-Fit_3.0_43650-0315_1x03_P3.00mm_Vertical" (property "Reference" "J3")))')
    r = MS.rebind_swapped(str(b), "Connector_Molex:Molex_Micro-Fit_3.0_43650-0315_1x03_P3.00mm_Vertical", [(stock, (0, 0, 0), (1, 1, 1), (0, 0, 0))])
    assert r["bound"] and r["bound"].endswith("MOLEX_43650-0315.step") and str(src / "lib") in r["bound"]
