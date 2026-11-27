"""Parts-first gate: every part is RESOLVED (concrete in-stock MPN, datasheet on disk, cited pin data, footprint/package agreement,
real 3D model) BEFORE placement or routing.  Three entry points, one module:

  gate()        the first-stage gate `ready` and `pipeline` run.  Stock is checked at the build quantity (`[bom] quantity_target`
                boards x parts per board).  A part that is not provably complete is a blocker; "not checked" is never a pass.
                The only bypass is the explicit `--allow-undocumented` (recorded, repeated in every later report).
  todo()        `parts-todo`: the machine-readable work list an agent needs, one record per part, with the evidence already known
                and distributor candidates (stock/price at the build quantity, datasheet URL, why each fits or not).
  apply()       `parts-apply`: validates agent proposals and writes only what passes, in one place (spec + mpn_map).  Anything
                that changes footprint, pinout, value or rating is an owner decision and is never auto-applied.

Needs per part (the gate's vocabulary): mpn, stock, datasheet, pinmap, package, model.  Nothing here is board-specific.
"""
import copy
import json
import os
import re
import tempfile

from . import parts as P
from . import modelpaths as MP

NEEDS = ("mpn", "mfr", "stock", "datasheet", "pinmap", "package", "model")
_CODE_NEED = {"PART_GENERIC": "mpn", "PART_MPN_MISSING": "mpn", "PART_MPN_IN_VALUE": "mpn", "PART_MFR_MISSING": "mfr",
              "PART_NO_STOCK": "stock", "PART_DATASHEET_MISSING": "datasheet", "PART_UNKNOWABLE": "datasheet", "PART_PINMAP_UNSOURCED": "pinmap",
              "PART_PACKAGE_MISMATCH": "package", "PART_PINCOUNT_MISMATCH": "package", "PART_FOOTPRINT_MISSING": "package",
              "PART_FOOTPRINT_MISMATCH": "package", "PART_PACKAGE_MISSING": "package"}
OWNER_FIELDS = ("footprint", "fp", "value", "rating", "pinout")
_STANDIN = re.compile(r"stand-?in|placeholder|dummy|generic", re.I)


# ------------------------------------------------------------------ the gate
def model_state(ref, facts):
    """-> (ok, detail).  Needs the board's facts: without a board the model cannot be seen, which is a blocker, not a pass."""
    from . import review as R
    if not facts:
        return False, "NOT CHECKED (no board: 3D model not looked at)"
    p = (facts.get("parts") or {}).get(ref)
    if p is None:
        return False, "part not on the board"
    models = p.get("models") or []
    if p.get("mech"):
        return True, "mechanical (no nets): no body needed"   # review.check_models exempts mech the same way
    if not models:
        return False, "footprint has no 3D model"
    bp = facts.get("board_path", "")
    bd = os.path.dirname(os.path.abspath(bp)) if bp else ""
    why = "3D model is a stand-in or does not resolve to a file"
    for m in models:
        if _STANDIN.search(os.path.basename(m)):
            continue
        full = R.resolve_model(m, bp)
        if not os.path.exists(full) and bp:       # a stage copy below the folder its lib/ paths were written for (review G5a)
            hit = MP.ancestor_hit(m, bd, resolve=lambda q, d: R.resolve_model(q, os.path.join(d, "_")))
            full = hit[0] if hit else full
        if not os.path.exists(full):
            continue
        if bd and MP.outside_lib(m, full, bd):
            why = f"3D model {os.path.basename(m)} resolves outside the project model library"
            continue
        return True, os.path.basename(m)
    return False, why


def gate(spec, cons, facts, res, qty=None, ds_dir=None):
    """-> {"ok", "qty", "parts": {ref: [{need, code, detail}]}, "by_need": {need: n}, "unchecked": [..], "n_parts",
    "geometry": [FP_*/LANDPATTERN_PAD_* FAIL findings]}.
    `res` is parts.evaluate()'s result (findings, rows, stock_data).
    Footprint geometry (fpcheck: FP_*, landpattern pad tables) is a "package" blocker. It is NEVER bypassed:
    `--allow-undocumented` does not pass FP_* / LANDPATTERN_PAD_* failures (ready refuses them).
    A footprint with no 3D model, or one that does not resolve (or lies outside the project model library), is likewise
    never bypassed (`g["models"]`): a board that renders with holes is not documented, it is unchecked."""
    from . import fpcheck as FC
    out = {}
    geometry = []
    geo_unchecked = []
    for f in FC.gate_findings(spec, cons, facts, ds_dir=ds_dir):
        if f["code"] == "FP_UNCHECKED":
            geo_unchecked.append(f["msg"])
        if f["level"] != P.FAIL or not FC.is_geometry(f["code"]):
            continue
        geometry.append(f)
        for r in f.get("refs") or [f"FP:{f.get('fp', '?')}"]:
            out.setdefault(r, []).append({"need": "package", "code": f["code"], "detail": f["msg"]})
    for f in res["findings"]:
        need = _CODE_NEED.get(f["code"])
        if need is None or (f["level"] == P.INFO and f["code"] != "PART_GENERIC"):
            continue
        if f["code"] == "PART_PACKAGE_MISSING" and f["level"] != P.FAIL:
            continue                       # a part with no package string (connector, module) is blocked only by `[parts] package = "required"`
        for r in f["refs"]:
            out.setdefault(r, []).append({"need": need, "code": f["code"], "detail": f["msg"]})
    unchecked = []
    models = []                            # refs with a missing / unresolvable / out-of-library 3D model: never bypassed
    stock = res.get("stock_data")
    if not res.get("lookup"):
        unchecked.append("distributor data and stock NOT CHECKED (offline / no credentials)")
    elif stock is None:
        unchecked.append("stock NOT CHECKED")
    for ref, row in res["rows"].items():
        got = out.setdefault(ref, [])
        if row.get("mpn") and "stock" not in {g["need"] for g in got} and "stock" not in row:
            got.append({"need": "stock", "code": "STOCK_UNCHECKED", "detail": f"{ref} {row['mpn']}: stock not checked"})
        ok, detail = model_state(ref, facts)
        if not ok and "NOT CHECKED" not in detail and detail != "part not on the board":
            from . import modelstart as MS
            ok = MS.waived(ref, cons, ((facts.get("parts") or {}).get(ref) or {}).get("footprint", ""))   # [review] waive, with its reason
        if not ok:
            if "NOT CHECKED" not in detail and detail != "part not on the board":
                models.append(ref)
            got.append({"need": "model", "code": "MODEL_" + ("UNCHECKED" if "NOT CHECKED" in detail else "MISSING"),
                        "detail": f"{ref}: {detail}"})
        if not got:
            del out[ref]
    unchecked += geo_unchecked
    by_need = {}
    for items in out.values():
        for n in {g["need"] for g in items}:
            by_need[n] = by_need.get(n, 0) + 1
    return {"ok": not out, "qty": qty, "parts": out, "by_need": by_need, "unchecked": unchecked,
            "n_parts": len(res["rows"]), "geometry": geometry, "models": models}


def summary_line(g):
    if g["ok"]:
        return f"parts gate: PASS - {g['n_parts']} part(s) resolved (build quantity {g['qty'] or 'unset, flat stock_need'})"
    n = ", ".join(f"{k} {v}" for k, v in sorted(g["by_need"].items(), key=lambda kv: -kv[1]))
    return (f"parts gate: BLOCKED - {len(g['parts'])} of {g['n_parts']} part(s) not resolved ({n}); "
            f"`fluxplace parts-todo --json` lists the work")


def blockers(g, top=4):
    seen, out = {}, []
    for ref, items in sorted(g["parts"].items()):
        for it in items:
            if seen.get(it["need"], 0) < top:
                seen[it["need"]] = seen.get(it["need"], 0) + 1
                out.append(f"{it['code']} {it['detail']}")
    return out


def bypass_note(g):
    n = ", ".join(f"{k} {v}" for k, v in sorted(g["by_need"].items()))
    return f"UNDOCUMENTED PARTS ALLOWED (--allow-undocumented): {len(g['parts'])} of {g['n_parts']} part(s) not resolved ({n})"


# ------------------------------------------------------------------ parts-todo
def _is_chip(ref):
    return bool(re.match(r"^(R|C|L|FB|RN)\d", ref or "", re.I))


def _group_key(c, row):
    return "|".join([row.get("mpn") or "", (c.get("value") or "").strip().lower(), row.get("fp") or "", str(row.get("package") or "")])


def _fit(cand, fp, required, temp):
    """-> (fits, [reasons]) for a distributor candidate against a footprint and the build quantity."""
    from . import review as R
    why, ok = [], True
    if (cand.get("stock") or 0) >= required:
        why.append(f"stock {cand['stock']} >= {required} needed")
    else:
        ok = False
        why.append(f"stock {cand.get('stock') or 0} < {required} needed")
    pkg = cand.get("package")
    if pkg and fp:
        v, d = R.packages_agree(pkg, fp)
        if v in ("family", "pins"):
            ok = False
            why.append(f"package '{pkg}' does not match footprint {fp} ({d})")
        elif v == "ok":
            why.append(f"package '{pkg}' matches {fp}")
        else:
            why.append(f"package '{pkg}' vs {fp}: cannot tell")
    elif not pkg:
        why.append("no package on the listing")
    sz, how = P.chip_from_mpn(cand["mpn"])
    fs = P.chip_from_fp(fp or "")
    if sz and fs and sz not in fs:
        ok = False
        why.append(f"MPN {how} says {sz}, footprint is {'/'.join(sorted(fs))}")
    lo, hi = temp
    if lo is not None or hi is not None:
        tmin, tmax = cand.get("temp_min"), cand.get("temp_max")
        if tmin is None or tmax is None:
            why.append("operating temperature not stated by the listing: check the datasheet")
        elif (lo is not None and tmin > lo) or (hi is not None and tmax < hi):
            ok = False
            why.append(f"temperature {tmin:g}..{tmax:g} C does not cover {lo:g}..{hi:g} C")
        else:
            why.append(f"temperature {tmin:g}..{tmax:g} C covers {lo:g}..{hi:g} C")
    if (cand.get("lifecycle") or "Active").lower() not in ("active", "in production", "new product", ""):
        ok = False
        why.append(f"lifecycle {cand['lifecycle']}")
    if not cand.get("datasheet"):
        why.append("no datasheet URL on the listing")
    return ok, why


def todo(spec, cons, res, g, qty=None, searcher=None, limit=5, verifier=None, enricher=None):
    """-> {"quantity", "summary", "unresolved", "parts": [record]}: one record per part (`missing` empty when resolved).
    `searcher(query, limit, qty)` -> [{mpn, mfr, stock, unit, ext, source, datasheet, package, temp_min, temp_max, lifecycle, params}].
    A part that has an MPN but is short at the build quantity also gets `alternatives`: dropin.classify() for each candidate (same MPN in
    other packaging first), `drop_in` true only when verified; `verifier(mpn, url)` -> {"level","text"} fetches the candidate's datasheet."""
    comps = {c.get("ref"): c for c in spec.get("components", [])}
    stock = res.get("stock_data") or {}
    data = res.get("data") or {}
    pc = (cons or {}).get("parts") or {}
    temp = tuple(None if pc.get(k) is None else float(pc[k]) for k in ("temp_min", "temp_max"))
    groups = {}
    for ref, row in res["rows"].items():
        groups.setdefault(_group_key(comps[ref], row), []).append(ref)
    recs, cache = [], {}
    for ref, row in sorted(res["rows"].items()):
        c = comps[ref]
        miss = g["parts"].get(ref, [])
        mpn = row.get("mpn")
        d = data.get(mpn) if mpn else None
        gk = _group_key(c, row)
        rec = {"ref": ref, "group": gk, "group_refs": sorted(groups[gk]), "value": c.get("value"), "footprint": row.get("fp"),
               "package": row.get("package"), "pins": row.get("pins"), "pinmap": c.get("pinmap"),
               "pinmap_source": c.get("pinmap_source"), "mpn": mpn, "mfr": row.get("mfr"), "datasheet_file": row.get("datasheet"),
               "missing": sorted({m["need"] for m in miss}), "findings": [{"code": m["code"], "detail": m["detail"]} for m in miss],
               "evidence": {"distributor": ({k: d.get(k) for k in ("matched", "manufacturer", "package", "pins", "lifecycle", "datasheet",
                                                                    "temp_min", "temp_max", "description")} if d else None),
                            "stock": stock.get(mpn) if mpn else None}, "candidates": []}
        if ({"mpn", "stock"} & set(rec["missing"])) and searcher:
            req = (qty or 1) * len(groups[gk])
            if gk not in cache:
                q = (c.get("value") or "").strip()
                if _is_chip(ref):
                    q = " ".join(x for x in (q, "/".join(sorted(P.chip_from_fp(row.get("fp") or "")))) if x)
                else:
                    q = re.sub(r"\s*\(.*", "", q)
                try:
                    found = searcher(q, limit=max(limit * 2, 10), qty=req) or []
                except Exception as e:                               # noqa: BLE001 - a failed search is reported, not fatal
                    found = [{"error": f"{type(e).__name__}: {e}"}]
                cs = []
                for cand in found:
                    if cand.get("error"):
                        cs.append(cand)
                        continue
                    ok, why = _fit(cand, row.get("fp"), req, temp)
                    cs.append({**cand, "fits": ok, "why": why, "required": req})
                cs.sort(key=lambda x: (not x.get("fits"), x.get("ext") is None, x.get("ext") or 0))
                cache[gk] = {"query": q, "list": cs[:limit]}
            rec["query"], rec["candidates"] = cache[gk]["query"], cache[gk]["list"]
        if mpn and "stock" in rec["missing"] and searcher and d:
            ak = ("alt", gk)
            if ak not in cache:
                cache[ak] = alternatives(mpn, d, row.get("fp"), (qty or 1) * len(groups[gk]), temp, searcher, verifier, limit, enricher=enricher)
            rec["alternatives"] = cache[ak]
        recs.append(rec)
    return {"quantity": qty, "summary": g["by_need"], "unresolved": len(g["parts"]), "parts": recs}


def alternatives(mpn, d, fp, required, temp, searcher, verifier=None, limit=5, verify_top=3, enricher=None):
    """Ranked equivalents for a part short at `required`: packaging variants of the same MPN, then parametric matches.
    -> [{mpn, mfr, stock, ext, source, drop_in, identical, checks, differs, unverified, lead_time?}]."""
    from . import dropin as DI, sourcing as S
    orig = DI.row_for(d, mpn)
    queries = [S._search_term(mpn)]
    desc = re.sub(r"\s+", " ", (d.get("description") or "")).strip()
    if desc:
        queries.append(desc[:120])
    seen, rows = {mpn}, []
    for q in queries:
        try:
            found = searcher(q, limit=20, qty=required) or []
        except Exception as e:                                         # noqa: BLE001
            rows.append({"error": f"{q[:40]}: {type(e).__name__}"})
            continue
        for c in found:
            if c.get("mpn") and c["mpn"] not in seen:
                seen.add(c["mpn"])
                rows.append(c)
    out = []
    if enricher:                                   # a listing without parametrics (Mouser keyword rows) is looked up by exact MPN
        n = 0
        for c in rows:
            if not c.get("error") and not c.get("params") and (c.get("stock") or 0) >= required and n < 8:
                n += 1
                try:
                    e = enricher(c["mpn"]) or {}
                except Exception:                                      # noqa: BLE001
                    e = {}
                c["params"] = e.get("raw_params") or e.get("params") or {}
                for k in ("package", "temp_min", "temp_max", "pins"):
                    c[k] = c.get(k) or e.get(k)
                c["lifecycle"] = c.get("lifecycle") or e.get("lifecycle")
    for c in rows:
        if c.get("error"):
            out.append(c)
            continue
        r = DI.classify(orig, DI.row_for(c, c["mpn"]) | {"stock": c.get("stock"), "lifecycle": c.get("lifecycle"), "pins": c.get("pins")}, fp, required, temp, None)
        out.append({**{k: c.get(k) for k in ("mpn", "mfr", "stock", "unit", "ext", "source", "datasheet", "lifecycle", "package", "lead_time")},
                    "required": required, **r})
    out = DI.rank([o for o in out if not o.get("error")])[:max(limit, verify_top)]
    if verifier:                                                       # datasheets only for the leading candidates: each is a download
        for o in out[:verify_top]:
            if o["checks"].get("stock", (False,))[0] and not o["differs"] or o["identical"]:
                ds = verifier(o["mpn"], o.get("datasheet"))
                row = next(c for c in rows if c.get("mpn") == o["mpn"])
                o.update({**o, **DI.classify(orig, DI.row_for(row, o["mpn"]) | {"stock": row.get("stock"), "lifecycle": row.get("lifecycle")},
                                            fp, required, temp, ds)})
                o["datasheet_file"] = (ds or {}).get("file")
                o["datasheet_level"] = (ds or {}).get("level")
        out = DI.rank(out)
    return out[:limit]


# ------------------------------------------------------------------ parts-apply
def _norm_pm(pm):
    return {str(k).upper(): str(v) for k, v in (pm or {}).items()}


def validate(spec, cons, facts, mm, ds, prop, lookup, stocker, model_ok=None, ds_ok=None, dropin=None):
    """-> ("apply"|"owner"|"reject", [reasons], patch).  `lookup(mpn)` -> distributor row|None; `stocker(mpn, refs)` -> {verdict, stock}|None;
    `model_ok(ref, comp, prop)` -> (bool, why); `ds_ok(mpn, prop)` -> bool (datasheet file on disk or fetched).
    A proposal that CHANGES an existing MPN (not a packaging variant of it) is a substitution: it is applied only when
    `dropin(old, new, refs)` -> dropin.classify() result says `drop_in`, otherwise it is an owner decision listing what differs."""
    refs = prop.get("refs") or ([prop["ref"]] if prop.get("ref") else [])
    mpn = (prop.get("mpn") or "").strip()
    comps = {c.get("ref"): c for c in spec.get("components", [])}
    if not refs or not mpn:
        return "reject", ["a proposal needs ref(s) and an mpn"], None
    bad = [r for r in refs if r not in comps]
    if bad:
        return "reject", [f"unknown ref(s): {', '.join(bad)}"], None
    owner = []
    if prop.get("needs_owner"):
        owner.append("proposal is flagged needs_owner")
    for k in OWNER_FIELDS:
        if prop.get(k) not in (None, "", False):
            owner.append(f"proposal changes {k}")
    for ch in prop.get("changes") or []:
        owner.append(f"proposal changes {ch}")
    if prop.get("pinmap"):
        for r in refs:
            old = comps[r].get("pinmap")
            if old and _norm_pm(old) != _norm_pm(prop["pinmap"]):
                owner.append(f"{r}: proposed pinmap differs from the spec pinmap")
    if owner:
        return "owner", sorted(set(owner)), None
    reasons = []
    from . import dropin as DI
    olds = {(comps[r].get("mpn") or mm.get(r) or "").strip() for r in refs} - {""}
    subst = sorted(o for o in olds if not DI.packaging_equivalent(o, mpn))
    evid = None
    if subst:
        if dropin is None or len(subst) > 1:
            return "owner", [f"replaces {', '.join(subst)} with {mpn}: needs drop-in verification (none available)"], None
        evid = dropin(subst[0], mpn, refs)
        if not evid or not evid.get("drop_in"):
            why = (evid or {}).get("differs", []) + (evid or {}).get("unverified", []) or ["could not be checked"]
            return "owner", [f"replaces {subst[0]} with {mpn}, not a verified drop-in: " + "; ".join(why[:6])], None
    d = lookup(mpn)
    if not d:
        return "reject", [f"{mpn}: not found at DigiKey/Mouser"], None
    from . import sourcing as S
    same, exact = S._same_part(d.get("matched") or "", mpn)       # a packaging/plating variant of the MPN is the same part, said aloud
    if not same:
        reasons.append(f"{mpn}: the distributor matched {d.get('matched')}, not that MPN")
    sk = stocker(mpn, refs)
    if not sk or sk.get("verdict") != "OK":
        got = f"grade {sk.get('verdict')}, {sk.get('stock')} on hand" if sk else "not checked"
        reasons.append(f"{mpn}: stock {got} at the build quantity")
    patch = {"refs": refs, "mpn": mpn, "matched": None if exact else d.get("matched"), "mfr": prop.get("mfr") or d.get("manufacturer"), "package": prop.get("package") or d.get("package")}
    for k in ("pinmap_source", "pinmap"):
        if prop.get(k):
            patch[k] = prop[k]
    if subst:
        patch["replaces"] = {"from": subst[0], "to": mpn, "date": __import__("time").strftime("%Y-%m-%d"),
                             "evidence": {k: v[1] for k, v in evid["checks"].items()}}
    if not patch["mfr"]:
        reasons.append(f"{mpn}: no manufacturer")
    if ds_ok is not None and not ds_ok(mpn, prop):
        reasons.append(f"{mpn}: datasheet not on disk and not fetchable")
    trial = copy.deepcopy(spec)                                   # the part-entry checks on a copy with the proposal applied
    tm = dict(mm or {})
    for c in trial["components"]:
        if c.get("ref") in refs:
            for k in ("mfr", "package", "pinmap_source", "pinmap"):
                if patch.get(k) and not (k == "pinmap" and c.get("pinmap")):
                    c[k] = patch[k]
            c["mpn"] = mpn
            tm[c["ref"]] = mpn
    r2 = P.run(trial, cons, facts=facts, mpn_map=tm, ds_dir=ds, data={mpn: d}, stock={mpn: sk} if sk else {})
    for f in r2["findings"]:
        if f["level"] == P.FAIL and set(f["refs"]) & set(refs) and f["code"] != "PART_NO_STOCK":
            reasons.append(f"{f['code']}: {f['msg']}")
    if model_ok is not None:
        for r in refs:
            ok, why = model_ok(r, comps[r], prop)
            if not ok:
                reasons.append(f"{r}: 3D model - {why}")
                break
    reasons = list(dict.fromkeys(reasons))
    return ("reject", reasons, None) if reasons else ("apply", [], patch)


def apply(spec_path, mpn_map_path, proposals, cons, facts, ds, lookup, stocker, model_ok=None, ds_ok=None, dry_run=False, dropin=None):
    """Validate every proposal, then write all that pass to the spec and the mpn_map in one step.
    -> {"applied": [...], "owner": [...], "rejected": [...]}."""
    spec = json.load(open(spec_path))
    mm_all = json.load(open(mpn_map_path)) if mpn_map_path and os.path.exists(mpn_map_path) else {}
    mm = {k: v for k, v in mm_all.items() if not k.startswith("_") and isinstance(v, str)}
    rep = {"applied": [], "owner": [], "rejected": []}
    patches, claimed = [], {}
    for i, prop in enumerate(proposals):
        label = (",".join(prop.get("refs") or [prop.get("ref", "?")]) + " " + str(prop.get("mpn", ""))).strip()
        verdict, why, patch = validate(spec, cons, facts, mm, ds, prop, lookup, stocker, model_ok, ds_ok, dropin)
        if verdict == "apply":
            dup = [r for r in patch["refs"] if r in claimed]
            if dup:
                verdict, why = "reject", [f"{', '.join(dup)} already set by proposal #{claimed[dup[0]] + 1} in this file"]
        if verdict == "apply":
            claimed.update({r: i for r in patch["refs"]})
            patches.append(patch)
            rep["applied"].append({"proposal": i, "what": label, "refs": patch["refs"],
                                   **({"note": f"distributor lists it as {patch['matched']}"} if patch.get("matched") else {}),
                                   **({"replaces": patch["replaces"]} if patch.get("replaces") else {})})
        else:
            rep["owner" if verdict == "owner" else "rejected"].append(
                {"proposal": i, "what": label, "reasons": why, "refs": prop.get("refs") or [prop.get("ref")]})
    if patches and not dry_run:
        by = {c.get("ref"): c for c in spec["components"]}
        for p in patches:
            for r in p["refs"]:
                c = by[r]
                c["mpn"] = p["mpn"]
                for k in ("mfr", "package", "pinmap_source"):
                    if p.get(k):
                        c[k] = p[k]
                if p.get("pinmap") and not c.get("pinmap"):
                    c["pinmap"] = p["pinmap"]
                mm_all[r] = p["mpn"]
        _write_json(spec_path, spec)
        if mpn_map_path:
            _write_json(mpn_map_path, mm_all)
        subs = [p["replaces"] | {"refs": p["refs"]} for p in patches if p.get("replaces")]
        if subs:                                       # old MPN -> new MPN with the evidence, next to the spec, appended
            lp = os.path.join(os.path.dirname(os.path.abspath(spec_path)), "parts-substitutions.json")
            try:
                log = json.load(open(lp))
            except (OSError, ValueError):
                log = []
            _write_json(lp, log + subs)
    return rep


def _write_json(path, obj):
    """Atomic write that keeps the file's own indent style (so a git diff shows only the records that changed)."""
    ind, nl = 1, True
    try:
        txt = open(path).read()
        m = re.match(r"\s*[\[{]\s*\n( +)", txt[:400])
        ind, nl = (len(m.group(1)) if m else None), txt.endswith("\n")
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=ind, ensure_ascii=False)
        if nl:
            fh.write("\n")
    os.replace(tmp, path)
