"""Parts-first gate (ready/pipeline first stage), parts-todo and parts-apply validation.  Fixtures only: no network, no credentials."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import parts as P, partsgate as PG, pipeline as PL
from test_ready import _board, _cons

OK_STOCK = {"verdict": "OK", "stock": 50000, "required": 200}


def _spec():
    return {"components": [
        {"ref": "R1", "value": "10k", "lib": "L", "fp": "R_0603_1608Metric", "mpn": "RC0603FR-0710KL", "mfr": "Yageo"},
        {"ref": "R2", "value": "4.7k", "lib": "L", "fp": "R_0603_1608Metric"},                       # generic passive
        {"ref": "U1", "value": "AMP", "lib": "L", "fp": "SOIC-8", "mpn": "AMP1234ID", "mfr": "ACME", "package": "SOIC-8",
         "pinmap": {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5", "F": "6", "G": "7", "H": "8"}, "pinmap_source": "AMP1234ID.pdf#p4"}],
        "nets": {"N1": [["R1", "1"], ["U1", "1"]]}}


def _facts(models=True):
    m = ["${KIPRJMOD}/m.step"] if models else []
    return {"board_path": "/x/b.kicad_pcb", "parts": {r: {"footprint": f, "models": list(m), "pads_total": n}
                                                      for r, f, n in (("R1", "R_0603_1608Metric", 2), ("R2", "R_0603_1608Metric", 2),
                                                                      ("U1", "SOIC-8", 8))}}


def _ds(tmp):
    d = tmp / "ds"
    d.mkdir(exist_ok=True)
    for n in ("RC0603FR-0710KL", "AMP1234ID"):
        (d / (n + ".pdf")).write_bytes(b"%PDF")
    return str(d)


def _res(tmp, spec, facts, stock=None, data=None, keep=False):
    data = data if data is not None else {"RC0603FR-0710KL": {"matched": "RC0603FR-0710KL", "manufacturer": "Yageo", "package": "0603"},
                                          "AMP1234ID": {"matched": "AMP1234ID", "manufacturer": "ACME", "package": "SOIC-8"}}
    stock = stock if stock is not None else {"RC0603FR-0710KL": OK_STOCK, "AMP1234ID": OK_STOCK}
    return P.evaluate(spec, {}, facts=facts, ds_dir=(str(tmp / "ds") if keep else _ds(tmp)), api=True, data=data, stock=stock)


def _model(tmp):
    (tmp / "m.step").write_text("x")
    f = _facts()
    f["board_path"] = str(tmp / "b.kicad_pcb")
    return f


def test_gate_blocks_each_missing_need(tmp_path):
    spec, f = _spec(), _model(tmp_path)
    g = PG.gate(spec, {}, f, _res(tmp_path, spec, f), qty=100)
    assert not g["ok"] and set(g["parts"]) == {"R2"} and "mpn" in {x["need"] for x in g["parts"]["R2"]}
    # drop R2: everything else is resolved
    spec["components"] = [c for c in spec["components"] if c["ref"] != "R2"]
    g = PG.gate(spec, {}, f, _res(tmp_path, spec, f), qty=100)
    assert g["ok"] and "PASS" in PG.summary_line(g)
    # no 3D model / out of stock / no datasheet / no pin source each block on their own
    g = PG.gate(spec, {}, _facts(models=False), _res(tmp_path, spec, _facts(models=False)), qty=100)
    assert {x["need"] for x in g["parts"]["U1"]} == {"model"}
    low = {"RC0603FR-0710KL": OK_STOCK, "AMP1234ID": {"verdict": "NONE", "stock": 0}}
    g = PG.gate(spec, {}, f, _res(tmp_path, spec, f, stock=low), qty=100)
    assert {x["need"] for x in g["parts"]["U1"]} == {"stock"}
    os.remove(tmp_path / "ds" / "AMP1234ID.pdf")
    g = PG.gate(spec, {}, f, _res(tmp_path, spec, f, keep=True), qty=100)
    assert "datasheet" in {x["need"] for x in g["parts"]["U1"]}
    spec["components"][1]["pinmap_source"] = ""
    g = PG.gate(spec, {}, f, _res(tmp_path, spec, f, keep=True), qty=100)
    assert "pinmap" in {x["need"] for x in g["parts"]["U1"]}


def test_gate_never_passes_what_it_did_not_check(tmp_path):
    spec = _spec()
    spec["components"] = spec["components"][:1]
    f = _model(tmp_path)
    res = P.evaluate(spec, {}, facts=f, ds_dir=_ds(tmp_path), api=False)            # offline: distributor + stock NOT CHECKED
    g = PG.gate(spec, {}, f, res)
    assert not g["ok"] and g["unchecked"] and "STOCK_UNCHECKED" in {x["code"] for x in g["parts"]["R1"]}
    g = PG.gate(spec, {}, None, _res(tmp_path, spec, f))                             # no board: model NOT CHECKED
    assert "MODEL_UNCHECKED" in {x["code"] for x in g["parts"]["R1"]}


def test_package_footprint_mismatch_blocks(tmp_path):
    spec = _spec()
    spec["components"] = [dict(spec["components"][0], mpn="RC0805FR-0710KL")]        # 0805 series code on an 0603 footprint
    f = _model(tmp_path)
    data = {"RC0805FR-0710KL": {"matched": "RC0805FR-0710KL", "manufacturer": "Yageo", "package": "0805"}}
    os.makedirs(tmp_path / "ds", exist_ok=True)
    g = PG.gate(spec, {}, f, _res(tmp_path, spec, f, data=data, stock={"RC0805FR-0710KL": OK_STOCK}))
    assert "package" in {x["need"] for x in g["parts"]["R1"]}


def test_build_quantity_reaches_the_stock_grade(monkeypatch, tmp_path):
    seen = {}

    def fake_check(by, need=10, cache_dir=None, refresh=False, log=None, both=False, qty=None):
        seen.update(by=by, qty=qty)
        return {m: {"verdict": "OK", "stock": 9, "required": len(r) * (qty or 1), "per_board": len(r)} for m, r in by.items()}, {}
    from fluxplace import sourcing as S
    monkeypatch.setattr(S, "check", fake_check)
    out = P.fetch_stock(["A", "B"], refs={"A": ["R1", "R2"], "B": ["U1"]}, qty=250)
    assert seen["qty"] == 250 and out["A"]["required"] == 500 and out["B"]["per_board"] == 1
    assert P.build_quantity({"bom": {"quantity_target": 100}}) == 100 and P.build_quantity({}) is None


# ---------------------------------------------------------------- the stage: --force / --allow-incomplete no longer skip it
def _pipe_args(tmp_path, b, sp, cons, **kw):
    from test_pipeline import _args
    return _args(tmp_path, b, spec=str(sp), constraints=cons, answer=["rule-floors=standard"], to_stage="floorplan-check", **kw)


def test_pipeline_gate_is_not_skipped_by_force_or_allow_incomplete(tmp_path):
    b = _board(tmp_path)
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    cons = _cons(tmp_path)
    a = _pipe_args(tmp_path, b, sp, cons, force=True, allow_incomplete=True)
    assert PL.run(a) == 1
    st = PL.load_state(a.workdir)
    assert st["stages"]["ready"]["verdict"] == "blocked" and "floorplan-check" not in st["stages"]
    assert not st.get("undocumented")


def test_allow_undocumented_is_loud_recorded_and_stamps(tmp_path):
    b = _board(tmp_path)
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    cons = _cons(tmp_path)
    a = _pipe_args(tmp_path, b, sp, cons, allow_undocumented=True)
    assert PL.run(a) == 0
    st = PL.load_state(a.workdir)
    assert "UNDOCUMENTED PARTS ALLOWED" in st["undocumented"] and "not_ready" in st
    assert "UNDOCUMENTED" in open(os.path.join(a.workdir, "LEDGER.txt")).read()
    assert "UNDOCUMENTED" in st["incomplete"]
    from fluxplace import questions as QS
    with pytest.raises(SystemExit):
        QS.refuse_assumed(st["board"], "fab")


def test_ready_cli_exit_codes_for_the_gate(tmp_path, capsys):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    base = ["ready", str(sp), "--constraints", _cons(tmp_path), "--no-api", "--datasheets", _ds(tmp_path)]
    with pytest.raises(SystemExit) as e:
        cli.main(base + ["--allow-incomplete"])
    assert e.value.code == 1 and "does not bypass" in capsys.readouterr().out
    cli.main(base + ["--allow-undocumented"])
    out = capsys.readouterr().out
    assert "UNDOCUMENTED PARTS ALLOWED" in out and "NOT READY" in out


# ---------------------------------------------------------------- parts-todo
def test_todo_lists_evidence_and_ranks_candidates(tmp_path):
    spec, f = _spec(), _model(tmp_path)
    spec["components"].append({"ref": "R3", "value": "4.7k", "lib": "L", "fp": "R_0603_1608Metric"})
    res = _res(tmp_path, spec, f)
    g = PG.gate(spec, {}, f, res, qty=100)
    calls = []

    def searcher(q, limit=10, qty=1):
        calls.append((q, qty))
        return [{"mpn": "RC0805FR-074K7L", "stock": 900000, "ext": 4.0, "package": "0805", "datasheet": "u", "temp_min": -55, "temp_max": 155},
                {"mpn": "RC0603FR-074K7L", "stock": 900000, "ext": 5.0, "package": "0603", "datasheet": "u", "temp_min": -55, "temp_max": 155},
                {"mpn": "RC0603FR-074K7LX", "stock": 10, "ext": 1.0, "package": "0603", "datasheet": "", "temp_min": -55, "temp_max": 155}]
    out = PG.todo(spec, {"parts": {"temp_min": -40, "temp_max": 85}}, res, g, qty=100, searcher=searcher)
    assert len(calls) == 1 and calls[0][1] == 200            # one search for the two identical resistors, at per-board count x 100
    r2 = next(r for r in out["parts"] if r["ref"] == "R2")
    assert r2["group_refs"] == ["R2", "R3"] and "mpn" in r2["missing"]
    c = r2["candidates"]
    assert c[0]["mpn"] == "RC0603FR-074K7L" and c[0]["fits"] and any("covers" in w for w in c[0]["why"])
    assert not next(x for x in c if x["mpn"] == "RC0805FR-074K7L")["fits"]
    assert not next(x for x in c if x["mpn"] == "RC0603FR-074K7LX")["fits"]
    r1 = next(r for r in out["parts"] if r["ref"] == "R1")
    assert r1["missing"] == [] and r1["evidence"]["distributor"]["manufacturer"] == "Yageo"
    json.dumps(out)


def test_todo_cli_json_offline(tmp_path, capsys):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    cli.main(["parts-todo", str(sp), "--constraints", _cons(tmp_path), "--no-api", "--datasheets", _ds(tmp_path), "--json", "--only-missing"])
    out = json.loads(capsys.readouterr().out)
    assert out["unresolved"] >= 1 and all(r["missing"] for r in out["parts"]) and "BLOCKED" in out["gate"]


# ---------------------------------------------------------------- parts-apply
def _world(tmp_path):
    spec, f = _spec(), _model(tmp_path)
    sp, mp = tmp_path / "spec.json", tmp_path / "mpn_map.json"
    sp.write_text(json.dumps(spec, indent=1))
    mp.write_text(json.dumps({"R1": "RC0603FR-0710KL", "U1": "AMP1234ID"}, indent=1))
    rows = {"RC0603FR-074K7L": {"matched": "RC0603FR-074K7L", "manufacturer": "Yageo", "package": "0603"},
            "RC0805FR-074K7L": {"matched": "RC0805FR-074K7L", "manufacturer": "Yageo", "package": "0805"},
            "RC0603FR-0710KL": {"matched": "RC0603FR-0710KL", "manufacturer": "Yageo", "package": "0603"},
            "AMP1234ID": {"matched": "AMP1234ID", "manufacturer": "ACME", "package": "SOIC-8"}}
    stocks = {"RC0603FR-074K7L": OK_STOCK, "RC0805FR-074K7L": OK_STOCK, "RC0603FR-0710KL": OK_STOCK, "AMP1234ID": OK_STOCK,
              "RC0603FR-0LOW": {"verdict": "LOW", "stock": 3}}
    rows["RC0603FR-0LOW"] = {"matched": "RC0603FR-0LOW", "manufacturer": "Yageo", "package": "0603"}
    return spec, f, sp, mp, rows, stocks


def _run(tmp_path, props, **kw):
    spec, f, sp, mp, rows, stocks = _world(tmp_path)
    ds = _ds(tmp_path)
    (tmp_path / "ds" / "RC0603FR-074K7L.pdf").write_bytes(b"%PDF")
    rep = PG.apply(str(sp), str(mp), props, {}, f, ds, lambda m: rows.get(m), lambda m, r: stocks.get(m),
                   model_ok=lambda r, c, p: PG.model_state(r, f), ds_ok=lambda m, p: P.datasheet_file(m, ds) is not None, **kw)
    return rep, sp, mp


def test_apply_writes_only_validated_proposals_in_one_place(tmp_path):
    props = [{"refs": ["R2"], "mpn": "RC0603FR-074K7L"}]
    rep, sp, mp = _run(tmp_path, props)
    assert [x["refs"] for x in rep["applied"]] == [["R2"]] and not rep["rejected"] and not rep["owner"]
    c = {x["ref"]: x for x in json.load(open(sp))["components"]}
    assert c["R2"]["mpn"] == "RC0603FR-074K7L" and c["R2"]["mfr"] == "Yageo" and c["R2"]["package"] == "0603"
    assert json.load(open(mp))["R2"] == "RC0603FR-074K7L" and json.load(open(mp))["U1"] == "AMP1234ID"
    assert c["R1"]["mpn"] == "RC0603FR-0710KL"                                        # untouched parts untouched


def test_apply_rejects_and_lists_each_failure(tmp_path):
    props = [{"ref": "R2", "mpn": "NOSUCHPART1"},                                      # not at a distributor
             {"ref": "R2", "mpn": "RC0603FR-0LOW"},                                    # not in stock at the build quantity
             {"ref": "R2", "mpn": "RC0805FR-074K7L"},                                  # 0805 part on an 0603 footprint
             {"ref": "R2", "mpn": "RC0603FR-074K7L", "datasheet": "x.pdf"},            # fine: datasheet is on disk
             {"ref": "R9", "mpn": "RC0603FR-074K7L"}]                                  # unknown ref
    rep, sp, mp = _run(tmp_path, props)
    why = {x["proposal"]: " ".join(x["reasons"]) for x in rep["rejected"]}
    assert "not found" in why[0] and "stock" in why[1] and "PART_PACKAGE_MISMATCH" in why[2] and "unknown ref" in why[4]
    assert [x["proposal"] for x in rep["applied"]] == [3]
    assert not [c for c in json.load(open(sp))["components"] if c.get("mpn") in ("NOSUCHPART1", "RC0805FR-074K7L")]


def test_apply_rejects_missing_datasheet_and_model(tmp_path):
    spec, f, sp, mp, rows, stocks = _world(tmp_path)
    ds = _ds(tmp_path)                                                                   # no RC0603FR-074K7L.pdf here
    rep = PG.apply(str(sp), str(mp), [{"ref": "R2", "mpn": "RC0603FR-074K7L"}], {}, f, ds, rows.get, lambda m, r: stocks.get(m),
                   model_ok=lambda r, c, p: PG.model_state(r, f), ds_ok=lambda m, p: P.datasheet_file(m, ds) is not None)
    assert "datasheet" in " ".join(rep["rejected"][0]["reasons"]) and not rep["applied"]
    f2 = _facts(models=False)
    rep = PG.apply(str(sp), str(mp), [{"ref": "R2", "mpn": "RC0603FR-0710KL"}], {}, f2, ds, rows.get, lambda m, r: stocks.get(m),
                   model_ok=lambda r, c, p: PG.model_state(r, f2), ds_ok=lambda m, p: True)
    assert "3D model" in " ".join(rep["rejected"][0]["reasons"])


def test_apply_never_auto_applies_owner_changes(tmp_path):
    props = [{"ref": "R2", "mpn": "RC0603FR-074K7L", "needs_owner": True},
             {"ref": "R2", "mpn": "RC0603FR-074K7L", "footprint": "R_0805_2012Metric"},
             {"ref": "R2", "mpn": "RC0603FR-074K7L", "changes": ["rating"]},
             {"ref": "U1", "mpn": "AMP1234ID", "pinmap": {"A": "2", "B": "1", "C": "3", "D": "4", "E": "5", "F": "6", "G": "7", "H": "8"}}]
    rep, sp, mp = _run(tmp_path, props)
    assert [x["proposal"] for x in rep["owner"]] == [0, 1, 2, 3] and not rep["applied"]
    assert "pinmap" in " ".join(rep["owner"][3]["reasons"])
    assert "mpn" not in {x["ref"]: x for x in json.load(open(sp))["components"]}["R2"]


def test_apply_accepts_a_packaging_variant_and_says_so(tmp_path):
    spec, f, sp, mp, rows, stocks = _world(tmp_path)
    ds = _ds(tmp_path)
    (tmp_path / "ds" / "RC0603FR-074K7L.pdf").write_bytes(b"%PDF")
    rows["RC0603FR-074K7L"] = dict(rows["RC0603FR-074K7L"], matched="RC0603FR-074K7LTR")       # tape-and-reel listing
    rep = PG.apply(str(sp), str(mp), [{"ref": "R2", "mpn": "RC0603FR-074K7L"}], {}, f, ds, rows.get, lambda m, r: stocks.get(m),
                   model_ok=lambda r, c, p: PG.model_state(r, f), ds_ok=lambda m, p: True)
    assert not rep["rejected"] and "RC0603FR-074K7LTR" in rep["applied"][0]["note"]
    rows["RC0603FR-074K7L"]["matched"] = "OTHERPART123"
    rep = PG.apply(str(sp), str(mp), [{"ref": "R2", "mpn": "RC0603FR-074K7L"}], {}, f, ds, rows.get, lambda m, r: stocks.get(m),
                   model_ok=lambda r, c, p: PG.model_state(r, f), ds_ok=lambda m, p: True, dry_run=True)
    assert "not that MPN" in rep["rejected"][0]["reasons"][0]


def test_apply_dry_run_and_duplicate_claims(tmp_path):
    props = [{"ref": "R2", "mpn": "RC0603FR-074K7L"}, {"ref": "R2", "mpn": "RC0603FR-074K7L"}]
    rep, sp, mp = _run(tmp_path, props, dry_run=True)
    assert len(rep["applied"]) == 1 and len(rep["rejected"]) == 1 and "already set" in rep["rejected"][0]["reasons"][0]
    assert "mpn" not in {x["ref"]: x for x in json.load(open(sp))["components"]}["R2"]


def test_apply_cli_end_to_end(tmp_path, monkeypatch, capsys):
    spec, f, sp, mp, rows, stocks = _world(tmp_path)
    ds = _ds(tmp_path)
    (tmp_path / "ds" / "RC0603FR-074K7L.pdf").write_bytes(b"%PDF")
    from fluxplace import partsearch as PS
    monkeypatch.setattr(PS, "lookup", lambda m, cache_dir=None: rows.get(m))
    monkeypatch.setattr(PS, "stocker", lambda qty, cache_dir=None: (lambda m, r: stocks.get(m)))
    b = _board(tmp_path)
    prop = tmp_path / "p.json"
    prop.write_text(json.dumps([{"ref": "R2", "mpn": "RC0603FR-074K7L", "model": "m.step"}]))
    (tmp_path / "m.step").write_text("x")
    cli.main(["parts-apply", str(prop), "--spec", str(sp), "--mpn-map", str(mp), "--constraints", _cons(tmp_path), "--datasheets", ds])
    assert "1 applied" in capsys.readouterr().out and "RC0603FR-074K7L" in sp.read_text()


def test_find_map_prefers_the_boards_own_versioned_map(tmp_path):
    from fluxplace import sourcing as S
    (tmp_path / "tools").mkdir()
    (tmp_path / "hw").mkdir()
    for n in ("mpn_map.json", "mpn_map-v16.json"):
        (tmp_path / "tools" / n).write_text("{}")
    assert S.find_map(str(tmp_path / "hw" / "proj-v16.kicad_pcb")).endswith("mpn_map-v16.json")
    assert S.find_map(str(tmp_path / "hw" / "proj-v15.kicad_pcb")).endswith("mpn_map.json")
    assert S.find_map(str(tmp_path / "hw" / "proj.kicad_pcb")).endswith("mpn_map.json")
