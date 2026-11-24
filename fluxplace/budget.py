"""One budget model (REFACTOR-PLAN-W16 section 2). Slice 1 (A6): `family_of`, the one family matcher.
A family is a constraints table key (`[pairs.F]`, `[channel.F]`) that is the longest prefix of a net name AND the net is a
leg of a classified pair.  A bare prefix hit also matches plain signals (X_LED, X_CLKREQ, X_CC): those are not pairs."""


_TAILS = ("_P", "_N", "DP", "DM", "P", "N", "+", "-")


def pair_legs(names, cons):
    """Set of nets that are a leg of a differential pair: a partner is in `names` (graph.diff_pairs / family tails), or the net sits
    under a [pairs.F] family and ends in a P/N tail (its partner is just not in `names`, e.g. a DRC item list). Never a net the
    owner declared plain (`not_pairs`), never a plain signal under the family prefix (X_LED, X_CLKREQ, X_CC)."""
    from . import netclass, constraints as C
    cons = cons or {}
    legs = set(netclass._pairs(sorted(names), cons))
    nopair = C.not_pairs(cons)
    fams = cons.get("pairs") or {}
    for n in names:
        if n not in nopair and n.endswith(_TAILS) and any(n.startswith(f) for f in fams):
            legs.add(n)
    return legs


def family_of(table, net, legs=None):
    """Longest key of `table` that prefixes `net`, else None. legs: set of pair-leg nets (pair_legs); when given, a net that is
    not a leg has no family."""
    if legs is not None and net not in legs:
        return None
    best = None
    for f in table or {}:
        if net.startswith(f) and (best is None or len(f) > len(best)):
            best = f
    return best


KINDS = ("rail", "support", "pair", "keepout", "keepout-flight", "separation", "escape", "swap")


def row(kind, name, metric, unit, value=None, target=None, mx=None, source="", klass="INF", hard=False, minimum=False,
        refs=(), nets=(), layers=(), detail="", **extra):
    """One budget row, the same shape for every kind (REFACTOR-PLAN-W16 section 2): quantity, target, max, source, klass.
    target warns, max fails (D71cv); minimum=True: target and max are floors (fail below). verdict: channel.verdict."""
    from . import channel as CH
    neg = (lambda v: None if v is None else -v) if minimum else (lambda v: v)
    r = {"kind": kind, "name": name, "metric": metric, "unit": unit, "value": value, "target": target, "max": mx,
         "source": source, "klass": klass, "hard": bool(hard), "minimum": bool(minimum), "refs": list(refs), "nets": list(nets),
         "layers": list(layers), "verdict": CH.verdict(neg(value), neg(target), neg(mx))[0], "detail": detail}
    r.update(extra)
    return r


def no_source(rows):
    """Rows whose number has no source (BUDGET_NO_SOURCE): a limit exists and `source` is empty or says NO SOURCE."""
    return [r for r in rows if (r["target"] is not None or r["max"] is not None) and (not r["source"] or "NO SOURCE" in r["source"])]


def pair_limits(cons, master, ps_per_mm=None):
    """-> (budget dict of the KEYS above, labels {key: where it came from}, family or None)."""
    from . import channel as CH
    cons = cons or {}
    fam = family_of(cons.get("channel"), master)
    b, lab = {}, {}
    if fam:
        e = cons["channel"][fam]
        for k in CH.KEYS:
            if k in e:
                b[k] = float(e[k])
                lab[k] = f"[channel.{fam}] {e.get('source', 'NO SOURCE')}"
    if fam and cons["channel"][fam].get("noisy_classes"):
        b["noisy_classes"] = list(cons["channel"][fam]["noisy_classes"])
    pf = family_of(cons.get("pairs"), master)
    if pf and "skew_ps_max" not in b:
        pe = cons["pairs"][pf]
        mx, tg = pe.get("skew_max_mm"), pe.get("skew_mm")
        if mx is None and "skew_ps" in pe:
            b["skew_ps_max"], lab["skew_ps_max"] = float(pe["skew_ps"]), f"fallback [pairs.{pf}] skew_ps (single limit)"
        elif ps_per_mm and (mx is not None or tg is not None):
            if mx is None:
                b["skew_ps_max"], lab["skew_ps_max"] = float(tg) * ps_per_mm, f"fallback [pairs.{pf}] skew_mm {tg} mm (single limit, as max)"
            else:
                b["skew_ps_max"], lab["skew_ps_max"] = float(mx) * ps_per_mm, f"fallback [pairs.{pf}] skew_max_mm {mx} mm"
                if tg is not None and "skew_ps_target" not in b:
                    b["skew_ps_target"], lab["skew_ps_target"] = float(tg) * ps_per_mm, f"fallback [pairs.{pf}] skew_mm {tg} mm"
    return b, lab, fam


PAIR_W_MM = 0.15            # trace width for the loss estimate (channel.predict's default)


def _centres(B, net):
    """{ref: (x, y)}: centre of the pads of `net` on each part."""
    by = {}
    for q in B["pads"]:
        if q["net"] == net:
            by.setdefault(q["ref"], []).append((q["x"], q["y"]))
    return {r: (sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps)) for r, ps in by.items()}


def mst_edges(pts):
    """Prim MST over {key: (x, y)} -> [(key_a, key_b, mm)], deterministic (keys sorted)."""
    import math
    keys = sorted(pts)
    if len(keys) < 2:
        return []
    done, out = [keys[0]], []
    while len(done) < len(keys):
        d, a, b = min((math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]), a, b) for a in done for b in keys if b not in done)
        out.append((a, b, d))
        done.append(b)
    return out


def pair_rows(B, cons, stack=None):
    """Rows of every classified pair in a [channel.F] or [pairs.F] family: length (predicted = endpoint distance x detour),
    loss (needs stack), endpoint (derived max distance between the pair's parts), skew (no value before routing).
    stack = (rows, planes, signal_layers) or None."""
    from . import channel as CH, netclass as NC
    cons = cons or {}
    names = sorted({q["net"] for q in B["pads"] if q["net"]})
    pm = NC._pairs(names, cons)
    legs = pair_legs(names, cons)
    out = []
    for m in sorted({min(n, p) for n, p in pm.items()}):
        if not (family_of(cons.get("channel"), m, legs) or family_of(cons.get("pairs"), m, legs)):
            continue
        srows, planes, sig = stack if stack else (None, None, None)
        dl = CH.delay_ps_mm(srows, planes, sig[0], PAIR_W_MM) if stack else None
        b, lab, fam = pair_limits(cons, m, dl)
        ctr = _centres(B, m)
        ep = sum(e[2] for e in mst_edges(ctr))
        ln = ep * CH.DETOUR
        name, refs, nets = f"{m}/{pm[m]}", sorted(ctr), [m, pm[m]]
        klass = str(((cons.get("channel") or {}).get(fam) or {}).get("klass", "INF")) if fam else "INF"
        lo = CH.loss_db_mm(srows, planes, sig[0], b.get("freq_ghz", CH.DEFAULT_FREQ_GHZ), PAIR_W_MM) if stack else None
        per_mm = (lo[0] + lo[1]) if lo else None
        conn = float(b.get("connector_db", 0.0))
        if "length_mm_max" in b or "length_mm_target" in b:
            out.append(row("pair", name, "length", "mm", round(ln, 2), b.get("length_mm_target"), b.get("length_mm_max"),
                           lab.get("length_mm_max") or lab.get("length_mm_target", ""), klass, "length_mm_max" in b, refs=refs, nets=nets))
        if per_mm and ("loss_db_max" in b or "loss_db_target" in b):
            out.append(row("pair", name, "loss", "dB", round(ln * per_mm + conn, 3), b.get("loss_db_target"), b.get("loss_db_max"),
                           lab.get("loss_db_max") or lab.get("loss_db_target", ""), klass, "loss_db_max" in b, refs=refs, nets=nets))
        lim, src = {}, []
        for key, lk, dk in (("max", "length_mm_max", "loss_db_max"), ("target", "length_mm_target", "loss_db_target")):
            c = ([b[lk]] if lk in b else []) + ([(b[dk] - conn) / per_mm] if per_mm and dk in b else [])
            lim[key] = round(min(c) / CH.DETOUR, 2) if c else None
            src += [lab[k] for k in (lk, dk) if k in b and k in lab]
        if lim["max"] is not None or lim["target"] is not None:
            out.append(row("pair", name, "endpoint", "mm", round(ep, 2), lim["target"], lim["max"],
                           f"derived: min(length, loss length) / detour {CH.DETOUR:g}; " + "; ".join(sorted(set(src))), "INF",
                           lim["max"] is not None, refs=refs, nets=nets))
        if "skew_ps_max" in b or "skew_ps_target" in b:
            out.append(row("pair", name, "skew", "ps", None, b.get("skew_ps_target"), b.get("skew_ps_max"),
                           lab.get("skew_ps_max") or lab.get("skew_ps_target", ""), klass, False, refs=refs, nets=nets))
    return out


def netclasses(cons, board=None, stack=None):
    """{family: {z, track_width, diff_pair_width, diff_pair_gap, layers {layer: (w, gap)}}} for every [pairs.F] with
    impedance_diff: what the project net class must carry (IMPROVEMENTS-W15 item 3). stack = (rows, planes, signal_layers):
    per-layer geometry from zgeom.family_table, the function pairs.layer_geometry uses; the class width / gap is the first
    signal layer's. Without a stack: constraints._expected_geometry (what inject_netclasses writes)."""
    from . import constraints as C, zgeom as ZG
    out = {}
    for fam, fv in sorted(((cons or {}).get("pairs") or {}).items()):
        if not isinstance(fv, dict) or not fv.get("impedance_diff"):
            continue
        z, w, g = C._expected_geometry(cons, fam, board)
        lay = {}
        if stack:
            rows, planes, sig = stack
            tab = ZG.family_table(cons, rows, planes, sig, fam, board=board)
            lay = {l: (t["w"], t["gap"]) for l, t in tab.items() if t["w"] and t["status"] in ("ok", "off", "unreachable")}
            if sig and sig[0] in lay:
                w, g = lay[sig[0]]
        out[fam] = {"z": z, "track_width": w, "diff_pair_width": w, "diff_pair_gap": g, "layers": lay}
    return out


def keepouts(B, cons):
    """Routing keep-outs with their layers: board rule areas that forbid tracks, and [placement.keepout.N] with `layers`.
    -> [{name, rect (x0, y0, x1, y1), layers [sorted], all_layers, source}]"""
    cu = list(B.get("copper") or [])
    out, k = [], 0
    for z in B.get("zones", []):
        if not (z.get("keepout") and z.get("no_tracks") and z.get("pts")):
            continue
        k += 1
        xs, ys = [p[0] for p in z["pts"]], [p[1] for p in z["pts"]]
        lay = sorted(set(z["layers"]) & set(cu))
        out.append({"name": z.get("name") or f"rule-area {k}", "rect": (min(xs), min(ys), max(xs), max(ys)), "layers": lay,
                    "all_layers": set(lay) >= set(cu), "source": "board rule area (tracks not allowed)"})
    for name, kv in sorted((((cons or {}).get("placement") or {}).get("keepout") or {}).items()):
        if not isinstance(kv, dict) or not kv.get("layers"):
            continue
        rect = tuple(float(v) for v in kv["rect"]) if kv.get("rect") else None
        if rect is None and kv.get("under") in B["parts"]:
            c, m = B["parts"][kv["under"]]["court"], float(kv.get("margin_mm", 0.0))
            rect = (c[0] - m, c[1] - m, c[2] + m, c[3] + m)
        if rect is None:
            continue
        lay = sorted(cu) if kv["layers"] == "all" else sorted(set(kv["layers"]) & set(cu))
        out.append({"name": name, "rect": rect, "layers": lay, "all_layers": set(lay) >= set(cu), "source": str(kv.get("source", ""))})
    return out


def keepout_rows(B, cons):
    out = []
    for k in keepouts(B, cons):
        x0, y0, x1, y1 = k["rect"]
        out.append(row("keepout", k["name"], "area", "mm2", round((x1 - x0) * (y1 - y0), 2), None, None, k["source"], "REQ", True,
                       layers=k["layers"], detail="all copper layers" if k["all_layers"] else f"{len(k['layers'])} layer(s)", rect=list(k["rect"])))
    return out


def flight_rows(B, cons, fixed=()):
    """A pair whose straight line between two of its parts crosses a routing keep-out: one row naming the pair and the
    keep-out. hard = the keep-out is on every copper layer AND both parts are fixed (no placement move and no layer change
    can fix it). A keep-out that contains one of the two points is the part's own rule area: skipped."""
    from . import geom as GM, netclass as NC
    cons = cons or {}
    kos = keepouts(B, cons)
    names = sorted({q["net"] for q in B["pads"] if q["net"]})
    pm, legs, out = NC._pairs(names, cons), pair_legs(names, cons), []
    inside = lambda r, p: r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]                      # noqa: E731
    ncu = len(B.get("copper") or [])
    for m in sorted({min(n, p) for n, p in pm.items()}):
        if not (family_of(cons.get("channel"), m, legs) or family_of(cons.get("pairs"), m, legs)):
            continue
        ctr = _centres(B, m)
        for a, b, _d in mst_edges(ctr):
            for k in kos:
                if inside(k["rect"], ctr[a]) or inside(k["rect"], ctr[b]) or not GM.seg_hits_rect(ctr[a], ctr[b], k["rect"]):
                    continue
                out.append(row("keepout-flight", f"{m}/{pm[m]} x {k['name']}", "crossing", "count", 1, None, 0, k["source"], "REQ",
                               k["all_layers"] and a in fixed and b in fixed, refs=sorted([a, b]), nets=[m, pm[m]], layers=k["layers"],
                               detail=f"flight line {a}-{b} crosses keep-out {k['name']} on {len(k['layers'])} of {ncu} copper layers"))
    return out
