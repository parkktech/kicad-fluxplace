"""One budget model (REFACTOR-PLAN-W16 section 2). Slice 1 (A6): `family_of`, the one family matcher.
A family is a constraints table key (`[pairs.F]`, `[channel.F]`) that is the longest prefix of a net name AND the net is a
leg of a classified pair.  A bare prefix hit also matches plain signals (X_LED, X_CLKREQ, X_CC): those are not pairs."""


_TAILS = ("_P", "_N", "DP", "DM", "P", "N", "+", "-")


def pair_legs(names, cons, complete=False):
    """Set of nets that are a leg of a differential pair: a partner is in `names` (graph.diff_pairs / family tails), or the net sits
    under a [pairs.F] family and ends in a P/N tail (its partner is just not in `names`, e.g. a DRC item list). Never a net the
    owner declared plain (`not_pairs`), never a plain signal under the family prefix (X_LED, X_CLKREQ, X_CC).
    complete=True: `names` is every net of the board, so a net with no partner in it is no leg (an active-low X_RST_N, X_WAKE_N
    under the family prefix is a plain signal): the tail rule is off."""
    from . import netclass, constraints as C
    cons = cons or {}
    legs = set(netclass._pairs(sorted(names), cons))
    nopair = C.not_pairs(cons)
    fams = cons.get("pairs") or {}
    for n in () if complete else names:
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
        refs=(), nets=(), layers=(), detail="", derived="", **extra):
    """One budget row, the same shape for every kind (REFACTOR-PLAN-W16 section 2): quantity, target, max, source, klass.
    target warns, max fails (D71cv); minimum=True: target and max are floors (fail below). verdict: channel.verdict.
    derived: what the limit was worked out from (a netclass budget, a default, a closed form); a derived limit is never hard."""
    from . import channel as CH
    neg = (lambda v: None if v is None else -v) if minimum else (lambda v: v)
    r = {"kind": kind, "name": name, "metric": metric, "unit": unit, "value": value, "target": target, "max": mx,
         "source": source, "klass": klass, "hard": bool(hard) and not derived, "minimum": bool(minimum), "refs": list(refs),
         "nets": list(nets), "layers": list(layers), "verdict": CH.verdict(neg(value), neg(target), neg(mx))[0], "detail": detail,
         "derived": str(derived or "")}
    r.update(extra)
    return r


def cites_page(source):
    """True when the source text names a page (file.pdf#p7, p.7, p7)."""
    import re
    return bool(re.search(r"(?:#p|\bp\.?\s?)\d", str(source or "")))


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
                           lim["max"] is not None, refs=refs, nets=nets, derived=f"endpoint = length / detour {CH.DETOUR:g}"))
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
        own = z.get("owner") or ""                       # a footprint's own rule area: it moves with the part
        out.append({"name": z.get("name") or (f"{own} rule-area {k}" if own else f"rule-area {min(xs):g},{min(ys):g}"),
                    "rect": (min(xs), min(ys), max(xs), max(ys)), "layers": lay, "all_layers": set(lay) >= set(cu), "owner": own,
                    "source": f"footprint rule area of {own} (tracks not allowed)" if own else "board rule area (tracks not allowed)"})
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
        out.append({"name": name, "rect": rect, "layers": lay, "all_layers": set(lay) >= set(cu), "source": str(kv.get("source", "")),
                    "owner": str(kv.get("under") or "") if not kv.get("rect") else ""})
    return out


def keepout_rows(B, cons):
    out = []
    for k in keepouts(B, cons):
        x0, y0, x1, y1 = k["rect"]
        out.append(row("keepout", k["name"], "area", "mm2", round((x1 - x0) * (y1 - y0), 2), None, None, k["source"], "REQ", True,
                       refs=[k["owner"]] if k["owner"] else [], layers=k["layers"], detail="all copper layers" if k["all_layers"] else f"{len(k['layers'])} layer(s)", rect=list(k["rect"])))
    return out


def homes(B, cons, spec=None):
    """{ref: the IC that owns it} for support parts whose owner is decided (clusters.DECIDED); an IC is its own home."""
    from . import clusters as CL
    own, _links = CL.ownership(B, None, cons, spec)
    return {r: o["ic"] for r, o in own.items() if o.get("how") in CL.DECIDED}


def flight_rows(B, cons, fixed=(), home=None):
    """A pair whose straight line between two of its parts crosses a routing keep-out: one row naming the pair and the
    keep-out. hard = the keep-out is on every copper layer AND both parts are fixed (no placement move and no layer change
    can fix it). A keep-out owned by one of the two parts, or one that contains one of the two points, is the part's own
    rule area: skipped."""
    from . import geom as GM, netclass as NC
    cons = cons or {}
    kos = keepouts(B, cons)
    names = sorted({q["net"] for q in B["pads"] if q["net"]})
    pm, legs, out = NC._pairs(names, cons), pair_legs(names, cons), []
    inside = lambda r, p: r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]                      # noqa: E731
    h = lambda r: (home or {}).get(r, r)                                                     # noqa: E731
    ncu = len(B.get("copper") or [])
    for m in sorted({min(n, p) for n, p in pm.items()}):
        if not (family_of(cons.get("channel"), m, legs) or family_of(cons.get("pairs"), m, legs)):
            continue
        ctr = _centres(B, m)
        for a, b, _d in mst_edges(ctr):
            for k in kos:
                if (k["owner"] and h(k["owner"]) in (h(a), h(b))) or inside(k["rect"], ctr[a]) or inside(k["rect"], ctr[b]) or not GM.seg_hits_rect(ctr[a], ctr[b], k["rect"]):
                    continue
                out.append(row("keepout-flight", f"{m}/{pm[m]} x {k['name']}", "crossing", "count", 1, None, 0, k["source"], "REQ",
                               k["all_layers"] and a in fixed and b in fixed, refs=sorted([a, b]), nets=[m, pm[m]], layers=k["layers"],
                               detail=f"flight line {a}-{b} crosses keep-out {k['name']} on {len(k['layers'])} of {ncu} copper layers"))
    return out


KO = "keepout:"            # a [[separation]] item "keepout:NAME" stands for the rect of that routing keep-out (budget.keepouts)


def sep_name(e, i):
    """Name of a [[separation]] row: its name, else "sep " + the sorted items of both groups (a row added or removed above it
    does not rename it, unlike the list index). Used by budget rows and intent.away alike."""
    if e.get("name"):
        return str(e["name"])
    items = lambda v: [v] if isinstance(v, str) else list(v or [])                        # noqa: E731
    a, b = sorted(str(x) for x in items(e.get("a"))), sorted(str(x) for x in items(e.get("b")))
    return "sep " + "|".join(a) + " / " + "|".join(b) if a or b else f"separation{i + 1}"


def separation_rows(B, cons, home=None):
    """One row per [[separation]]: the smallest courtyard-to-courtyard distance between group a and group b. An item
    "keepout:NAME" is the rect of a routing keep-out (an antenna cut-out has no part of its own on a carrier board)."""
    from . import geom as GM, intent as IT
    parts, out = B["parts"], []
    ko = {KO + k["name"]: k["rect"] for k in keepouts(B, cons)}
    owner = {KO + k["name"]: k["owner"] for k in keepouts(B, cons)}
    h = lambda r: (home or {}).get(r, r)                                                    # noqa: E731

    def group(items):
        items = [items] if isinstance(items, str) else list(items or [])
        rects = {x: ko[x] for x in items if x in ko}
        rects.update({r: parts[r]["court"] for r in IT.match(parts, [x for x in items if not str(x).startswith(KO)])})
        return rects
    for i, e in enumerate((cons or {}).get("separation") or []):
        if e.get("enabled", True) is False:
            continue
        name = sep_name(e, i)
        ga, gb = group(e.get("a")), group(e.get("b"))
        gb = {k: v for k, v in gb.items() if k not in ga}     # a ref both groups match belongs to group a: never measured against its mates

        def own_ko(x, y):                                     # a keep-out against its owner part or that part's module
            return any(owner.get(p) and h(owner[p]) == h(q) for p, q in ((x, y), (y, x)))
        best = min(((GM.rect_gap(ga[x], gb[y]), x, y) for x in sorted(ga) for y in sorted(gb) if x != y and not own_ko(x, y)), default=None)
        has_min = e.get("min_mm") is not None
        out.append(row("separation", name, "distance", "mm", round(best[0], 2) if best else None, e.get("target_mm"), e.get("min_mm"),
                       str(e.get("source", "")), str(e.get("klass", "INF")), has_min and bool(e.get("hard", True)), minimum=True,
                       refs=[best[1], best[2]] if best else [], detail=str(e.get("why", "")),
                       derived=str(e.get("derived") or "")))
    return out


def tracks_between(pitch, pad, width, space):
    """Tracks that pass between two pads: n x width + (n + 1) x space <= pitch - pad."""
    import math
    return max(0, math.floor((pitch - pad - space) / (width + space) + 1e-9))


def escape_rows(B, cons):
    """One closed-form row per fine-pitch part (escapeplan.is_fine): tracks between pads at the rule floor, rows per layer,
    layers needed, corridor depth. The lay's break-out check stays the authority (a contradiction is a drift row, step 10)."""
    import math
    from . import escapeplan as EP, escreserve as ER, zgeom as ZG
    pr = EP.plan_params(cons)
    floor = ZG.floors(cons)[0]
    via = float(((cons or {}).get("route") or {}).get("min_via_mm", 0.46))
    out = []
    for ref in sorted(B["parts"]):
        part = B["parts"][ref]
        ps = [q for q in part["pads"] if not q.get("np")]
        if len(ps) < 4 or EP.is_mech(ref) or not EP.is_fine(part, pr):
            continue
        d, a, b = min(((math.hypot(p["x"] - q["x"], p["y"] - q["y"]), i, j) for i, p in enumerate(ps) for j, q in enumerate(ps)
                       if i < j and p["num"] != q["num"] and math.hypot(p["x"] - q["x"], p["y"] - q["y"]) > 0.05))
        along_x = abs(ps[a]["x"] - ps[b]["x"]) >= abs(ps[a]["y"] - ps[b]["y"])
        pitch = round(d, 4)
        pad = round(2 * (ps[a]["ex"] if along_x else ps[a]["ey"]), 4)
        sig = [q for q in ps if 2 * min(q["ex"], q["ey"]) <= 2 * pitch]      # without thermal and shell pads
        x0, x1 = min(q["x"] for q in sig), max(q["x"] for q in sig)
        y0, y1 = min(q["y"] for q in sig), max(q["y"] for q in sig)
        # rings: pad rows counted from the nearest outward edge (a perimeter part and a two-row connector are 1 deep)
        deep = ER._rows([min(q["x"] - x0, x1 - q["x"], q["y"] - y0, y1 - q["y"]) for q in sig], tol=pitch / 2)
        n, nv = tracks_between(pitch, pad, floor, floor), tracks_between(pitch, via, floor, floor)
        surface = n + 1
        if deep <= surface:
            need = 1
        elif deep <= surface + nv + 1:
            need = 2
        else:
            need = None if nv == 0 else 2 + math.ceil((deep - surface - nv - 1) / nv)
        out.append(row("escape", ref, "tracks_between_pads", "count", n, None, None,
                       f"closed form: floor((pitch {pitch:g} - pad {pad:g} - space {floor:g}) / (width {floor:g} + space {floor:g})); "
                       f"rows per layer after Pulliam (JLab BGA escape note)", "INF", False, refs=[ref],
                       detail=f"{len(ps)} pins, {deep} ring(s) deep; " + ("no layer count escapes it without via-in-pad" if need is None
                                                                        else f"{need} signal layer(s) needed"),
                       pitch_mm=pitch, pad_mm=pad, gap_mm=round(pitch - pad, 4), pins=len(ps), rows_deep=deep,
                       rows_surface=surface, tracks_between_vias=nv, layers_needed=need,
                       corridor_mm=round(min(deep, surface) * 2 * floor, 3)))
    return out


def escape_layer_rows(B, cons, rows=None):
    """One row per escape row: signal layers needed against the signal layers the stackup has (copper minus [stackup] planes).
    Closed form, so derived (soft). No-escape (layers_needed None) reports have + 1."""
    cu = list(B.get("copper") or [])
    have = len([l for l in cu if l not in set(((cons or {}).get("stackup") or {}).get("planes") or ())])
    out = []
    for e in rows if rows is not None else escape_rows(B, cons):
        need = e["layers_needed"]
        out.append(row("escape", e["name"], "layers_needed", "count", have + 1 if need is None else need, None, have,
                       e["source"], "INF", True, refs=e["refs"], detail=e["detail"], derived="closed form"))
    return out


def swap_rows(B, cons):
    """Declared swap groups. polarity with both legs on the same two parts: value 1 when the two flight lines cross (a swap at
    one end removes the crossing; proposal only, the schematic owns the change). Other kinds are listed with no value."""
    from . import geom as GM
    out = []
    for i, e in enumerate((cons or {}).get("swap") or []):
        name, nets = str(e.get("name") or f"swap{i + 1}"), list(e.get("nets") or [])
        val, refs, det = None, [], "listed; evaluated with the crossings term (step 6)"
        if e.get("kind") == "polarity" and len(nets) == 2:
            p, n = _pads_by_ref(B, nets[0]), _pads_by_ref(B, nets[1])
            refs = sorted(set(p) & set(n))
            if len(refs) == 2 and len(p) == 2 and len(n) == 2:
                a, b = refs
                val = 1 if GM._segs_cross(p[a], p[b], n[a], n[b]) else 0
                det = "proposal: swap P/N at one end (schematic change, DECISIONS row)" if val else "no crossing"
        out.append(row("swap", name, "crossing", "count", val, None, 0 if val is not None else None, str(e.get("source", "")), "REQ",
                       False, refs=refs, nets=nets, detail=det, decided=str(e.get("decided", ""))))
    return out


def _pads_by_ref(B, net):
    """{ref: (x, y)} of the first pad of `net` on each part."""
    out = {}
    for q in B["pads"]:
        if q["net"] == net and q["ref"] not in out:
            out[q["ref"]] = (q["x"], q["y"])
    return out


TEMPLATE_KEYS = ("cin_loop_mm", "sw_mm", "fb_mm")


def template_rows(B, cons, spec=None):
    """support rows of a regulator against the maxima of its part's `layout` fact: the measured loop lengths (clusters.loop_metrics) vs
    cin_loop_mm / sw_mm / fb_mm. Hard only when the number is the datasheet's: a key the fact lists in `derived` was worked out from
    courtyard geometry (D71do) and grades as recommended (not hard, klass INF). No fact, no row."""
    from . import clusters as CL, netclass as NC, powerbudget as PB
    facts, mpn_map = PB.facts_for(cons, spec)
    if not any(isinstance(f.get("layout"), dict) for f in facts.values()):
        return []
    names = NC.pin_names(spec)
    out = []
    for c in CL.build(B, cons, spec, facts=facts, mpn_map=mpn_map)[0]:
        lay = c.get("layout")
        if not lay:
            continue
        c = dict(c, supports=[s for s in c["supports"] if s["sure"]])         # a guessed owner is not this regulator's part (M5)
        got = CL.loop_metrics(B, c, names.get(c["ic"], {}))
        der = lay["fact"].get("derived")
        der = set(lay["fact"]) if der is True else {str(k) for k in der} if isinstance(der, (list, tuple)) else set()
        for key in TEMPLATE_KEYS:
            mx = lay["fact"].get(key)
            if not isinstance(mx, (int, float)) or isinstance(mx, bool):
                continue
            hard = key not in der
            src = (f"datasheet fact layout.{key} of {lay['mpn']} ({lay['src']})" if hard
                   else f"derived layout.{key} of {lay['mpn']} from courtyard geometry, recommended ({lay['src']} states the rule, not the number)")
            out.append(row("support", f"{c['ic']} template {key}", "template", "mm", got.get(key), mx, mx, src,
                           "REQ" if hard else "INF", hard, refs=[c["ic"]] + [s["ref"] for s in c["supports"] if s["rank"] < 9],
                           detail="" if got.get(key) is not None else "no such part in the module"))
        pairs = lay["pairs"]
        if pairs:                       # cin_side is the datasheet's REQ the seater steers by: it gets a row, never a silent fallback (A1)
            caps = [s for s in c["supports"] if s["rank"] == 1 and s["ref"].startswith("C")]
            served = sum(1 for pr in pairs if any(CL._on_cin_side(B["parts"][c["ic"]], B["parts"][s["ref"]]["court"], [pr], pr[0],
                                                                  B["parts"][s["ref"]]["pads"]) for s in caps))
            out.append(row("support", f"{c['ic']} template cin_side", "template", "pairs", served, len(pairs), len(pairs),
                           f"datasheet fact layout.cin_side of {lay['mpn']} ({lay['src']})", "REQ", True, minimum=True,
                           refs=[c["ic"]] + [s["ref"] for s in caps], detail="input capacitor on the side of each named pin pair"))
    return out


def table(B, cons, spec=None, stack=None, plan=None, fixed=()):
    """Every budget row of the board, one shape (row): rails and supports from the power plan (powerbudget.build output, or
    None), pairs, keep-outs, keep-out against flight line, separation, escape, swap."""
    out = []
    for net, rail in sorted(((plan or {}).get("rails") or {}).items()):
        src = (rail.get("source") or {}).get("spec") or "?"
        for e in rail.get("edges") or []:
            out.append(row("rail", f"{net} {src}->{e['load']}", "reach", "mm", e["dist_mm"], e["reach_mm"]["target"], e["reach_mm"]["max"],
                           rail["drop"]["source"], rail["drop"].get("klass", "INF"), True, refs=[src, e["load"]], nets=[net],
                           layers=rail["choice"]["layers"],
                           derived=rail["drop"].get("derived") or ("" if cites_page(rail["drop"]["source"]) else "no page cited")))
    for s in (plan or {}).get("supports") or []:
        lim = s["limit"]
        out.append(row("support", f"{s['ref']} -> {s['serves']}", "distance", "mm", s["dist_mm"] if s["sure"] else None, lim["target"],
                       lim["max"], lim["source"], lim["klass"], bool(s["sure"]), refs=[s["ref"], s["serves"].split(".")[0]],
                       nets=[s["net"]], detail=f"{s['role']}, owner by {s['how']}", derived=lim.get("derived", "")))
    for p in (plan or {}).get("pin_demand") or []:
        cv = p.get("cover")
        out.append(row("support", f"{p['ic']}.{p['pad']} own bypass ({', '.join(p['parts'])})", "pin-bypass", "mm", p["dist_mm"], p["limit_mm"],
                       None if cv else p["limit_mm"], "per-pin demand: the IC's own bypass within the support limit (clusters.pin_demand)", "INF",
                       not cv, refs=list(p["parts"]) + [p["ic"]], nets=[p["net"]],
                       detail=(f"only a larger capacitor at the pin: {cv['ref']} {cv['uf']:g} uF at {cv['dist_mm']:g} mm" if cv
                               else "no capacitor at the pin"), derived="default support limit"))
    home, esc = homes(B, cons, spec), escape_rows(B, cons)
    return (out + template_rows(B, cons, spec) + pair_rows(B, cons, stack) + keepout_rows(B, cons) + flight_rows(B, cons, fixed, home)
            + separation_rows(B, cons, home) + esc + escape_layer_rows(B, cons, esc) + swap_rows(B, cons))


def open_items(rows):
    """What `ready` leaves OPEN: [(code, text)]. BUDGET_NO_SOURCE (a limit without a source), KEEPOUT_FLIGHT (a hard
    keep-out against flight-line row), SWAP_PROPOSED (an undecided polarity swap that removes a crossing)."""
    out = [("BUDGET_NO_SOURCE", f"{r['kind']} {r['name']} {r['metric']}") for r in no_source(rows)]
    out += [("KEEPOUT_FLIGHT", f"{r['name']}: {r['detail']}") for r in rows if r["kind"] == "keepout-flight" and r["hard"]]
    out += [("SWAP_PROPOSED", f"{r['name']} ({', '.join(r['nets'])}): {r['detail']}") for r in rows
            if r["kind"] == "swap" and r["value"] == 1 and not r.get("decided")]
    return out


def rail_classes(cons, plan):
    """{net: {plan_mm, class_mm, under}} for every planned rail that is a track (not a pour or corridor): the width the power
    plan needs against the width constraints._build_netclasses writes into the project class (constraints.rail_width_mm).
    under = the written class is narrower than the plan (LESSONS-W15 rule 6: a class must come from the budget)."""
    from . import constraints as C
    out = {}
    for net, rail in sorted(((plan or {}).get("rails") or {}).items()):
        pw = ((cons or {}).get("power") or {}).get(net) or {}
        ch = rail.get("choice") or {}
        if ch.get("kind") != "track" or not ch.get("width_mm") or pw.get("pour") or not pw.get("max_current_ma"):
            continue
        cw = C.rail_width_mm(pw["max_current_ma"])
        out[net] = {"plan_mm": ch["width_mm"], "class_mm": cw, "under": cw + C.GEOM_TOL_MM < ch["width_mm"]}
    return out


def summary(rows):
    """'N fail, M over target' over the rows that carry a verdict ('' when none): the ready line shows it, nothing gates on it here."""
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("fail", "over-target-pass")}
    hard = sum(1 for r in rows if r["verdict"] == "fail" and r["hard"])
    return "; ".join(t for t in (f"{n['fail']} fail ({hard} hard)" if n["fail"] else "",
                                  f"{n['over-target-pass']} over target" if n["over-target-pass"] else "") if t)


def honoured(before, after):
    """Rows of the output board against the same rows of the input: [{name, kind, metric, hard, derived, value_in, value_out, limit,
    status}]. status: "honoured" (verdict ok / over-target-pass), "not_honoured" (fail), "not_measured" (n/a or the row is gone).
    Keyed by (kind, name, metric); a row only in `after` has value_in None."""
    key = lambda r: (r["kind"], r["name"], r["metric"])
    old = {key(r): r for r in before}
    out = []
    for r in after:
        b = old.pop(key(r), None)
        st = {"ok": "honoured", "over-target-pass": "honoured", "fail": "not_honoured"}.get(r["verdict"], "not_measured")
        out.append({"name": r["name"], "kind": r["kind"], "metric": r["metric"], "hard": r["hard"], "derived": r["derived"],
                    "value_in": b["value"] if b else None, "value_out": r["value"],
                    "limit": r["max"] if r["max"] is not None else r["target"], "status": st})
    for b in old.values():
        out.append({"name": b["name"], "kind": b["kind"], "metric": b["metric"], "hard": b["hard"], "derived": b["derived"],
                    "value_in": b["value"], "value_out": None,
                    "limit": b["max"] if b["max"] is not None else b["target"], "status": "not_measured"})
    return out


def honoured_summary(h):
    """'rows on the output: N honoured, H hard not honoured (names), S soft not honoured, M not measured'."""
    names = lambda hard: ", ".join(r["name"] for r in h if r["status"] == "not_honoured" and r["hard"] is hard) or "none"
    cnt = lambda st: sum(1 for r in h if r["status"] == st)
    soft = sum(1 for r in h if r["status"] == "not_honoured" and not r["hard"])
    return (f"rows on the output: {cnt('honoured')} honoured, {sum(1 for r in h if r['status'] == 'not_honoured' and r['hard'])} "
            f"hard not honoured ({names(True)}), {soft} soft not honoured, {cnt('not_measured')} not measured")
