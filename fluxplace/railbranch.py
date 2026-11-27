"""W46: the branches of a [power] rail (load pads the rails step left open) are routed by the router at each connection's own
D71da width, before the plain step; the plain step never routes a rail net.

Why: the plain step laid every branch at the signal width (0.15 mm); power.check (tree-flow solve) then found runs under their
solved current and widening afterwards has no clearance (W42 closed 4.2 of ~240 narrow mm).  KRT takes one width per net per call, so the
open connections of a net are grouped by width class and routed class by class, widest first; the pads of the classes not being
routed are held (renamed to a one-pad dummy net for that call) so KRT leaves them open.  A connection the router cannot lay at its width goes down its
ladder ONLY to a rung D71da proves (neck_heat_c at the connection's current and length <= the rail's rise); with no proven rung it stays
open and is reported (never silently the signal width).
"""
import math
import os

HOLD = "~hold"


def branch_nets(cons, plane_nets=()):
    """[power] nets with a current figure that are not plane nets (taps) or declared signals: the nets whose connections
    this step owns and the plain step skips."""
    from . import power as PWR
    sig = PWR.signal_nets(cons)
    return sorted(n for n, r in PWR.plan(cons).items() if r and r.get("track_mm") is not None and n not in set(plane_nets) and n not in sig)


def conn_width(r, amps, track_mm):
    """D71da width of one connection carrying `amps` (None -> the rail total, the caller flags it): the IPC-2221 outer width
    rounded up to 0.05 mm, never under the signal width nor over the rail's own width."""
    from . import power as PWR
    if amps is None:
        amps = r.get("current_a") or 0.0
    w = PWR._ceil05(PWR.ipc2221_width_mm(amps, r["rise_c"], r["oz_outer"], False)) if amps > 0 else track_mm
    return round(max(track_mm, min(w, max(float(r["track_mm"]), track_mm))), 2)


def conn_ladder(cons, net, r, amps, length_mm, track_mm):
    """[(width, proof)] widest first: the connection's own width (proof None), then the rail ladder rungs below it and the signal
    width, each only when the neck-down test of D71da holds at `amps` over `length_mm` (proof = the heat rise)."""
    from . import power as PWR
    a = r.get("current_a") or 0.0 if amps is None else amps
    w0 = conn_width(r, amps, track_mm)
    out = [(w0, None)]
    rungs = PWR.ladder_widths(cons, net, w0, track_mm)[1:]
    if track_mm < w0 - 1e-6 and (not rungs or rungs[-1] > track_mm + 1e-6):
        rungs = [*rungs, round(track_mm, 2)]
    for w in rungs:
        heat = PWR.neck_heat_c(a, max(length_mm, 0.01), w, r["oz_outer"])
        if heat <= r["rise_c"] + 1e-9:
            out.append((w, round(heat, 1)))
    return out


def _dist_pt_seg(px, py, s):
    from . import power as PWR
    return PWR._seg_dist(px, py, s[0], s[1], s[2], s[3])


def plan(board, cons, nets, track_mm, nlayers=None, ctx=None):
    """The open connections of `nets` on `board` (zones filled, KiCad connectivity): {net: {"rail": r, "main": n pads, "comps": [
    {"pads": [(ref, num)], "amps", "how", "unknown", "len_mm", "ladder"}]}}.  A connection = a group of pads not joined to the
    net's largest group; amps = the sum of its pads' loads (PWR._pad_amps); None when a pad's load is not in the budget (-> the rail total, flagged)."""
    import pcbnew
    from . import power as PWR
    ctx = ctx or PWR._board_ctx(board, set(nets))
    if not ctx:
        return {}
    b = ctx["b"]
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.BuildConnectivity()
    conn = b.GetConnectivity()
    out = {}
    for net in nets:
        r = PWR.rail(cons, net, layers=nlayers)
        if not r or r.get("track_mm") is None:
            continue
        pads = [p for p in b.GetPads() if p.GetNetname() == net]
        info = {(pa[0], pa[1]): pa for pa in ctx["pads"].get(net, [])}
        groups, items = {}, {}
        for p in pads:
            k = (p.GetParentFootprint().GetReference(), p.GetNumber())
            its = conn.GetConnectedItems(p)
            g = tuple(sorted((q.GetParentFootprint().GetReference(), q.GetNumber()) for q in its if q.Type() == pcbnew.PCB_PAD_T)) or (k,)
            groups[k] = g
            items[g] = its
        uniq = sorted(set(groups.values()), key=lambda g: (-len(g), g))
        if len(uniq) <= 1:
            out[net] = {"rail": r, "main": len(uniq[0]) if uniq else 0, "comps": []}
            continue
        main = uniq[0]
        segs, pts = [], []
        for it in items[main]:
            t = it.Type()
            if t == pcbnew.PCB_TRACE_T:
                segs.append((pcbnew.ToMM(it.GetStart().x), pcbnew.ToMM(it.GetStart().y), pcbnew.ToMM(it.GetEnd().x), pcbnew.ToMM(it.GetEnd().y)))
            elif t == pcbnew.PCB_VIA_T:
                pts.append((pcbnew.ToMM(it.GetPosition().x), pcbnew.ToMM(it.GetPosition().y)))
            elif t == pcbnew.PCB_PAD_T:
                pts.append((pcbnew.ToMM(it.GetPosition().x), pcbnew.ToMM(it.GetPosition().y)))
        pw = (cons or {}).get("power", {}).get(net) or {}
        drows = [d for d in (cons or {}).get("dcdrop", []) or [] if isinstance(d, dict) and d.get("net") == net]
        src = str(pw.get("source") or (drows[0].get("from") if drows else "") or "").split(".")[0]
        npins = {}
        for pa in info.values():
            npins[pa[0]] = npins.get(pa[0], 0) + 1
        comps = []
        for g in uniq[1:]:
            tot, unk, how = 0.0, [], set()
            for k in g:
                pa = info.get(k)
                a_, h_ = PWR._pad_amps(pa, npins, drows, src, r["current_a"], r.get("max_v") or 0.0) if pa else (None, "unknown (pad)")
                how.add(h_)
                if a_ is None:
                    unk.append(k[0])
                else:
                    tot += a_
            amps = None if unk else tot
            d = min([min(math.hypot(info[k][5][0] - x, info[k][5][1] - y) for x, y in pts) for k in g if k in info and pts] +
                    [min(_dist_pt_seg(info[k][5][0], info[k][5][1], s) for s in segs) for k in g if k in info and segs] or [0.0])
            comps.append({"pads": list(g), "amps": amps, "how": "+".join(sorted(how)), "unknown": sorted(set(unk)),
                          "len_mm": round(d, 2), "ladder": conn_ladder(cons, net, r, amps, d, track_mm)})
        out[net] = {"rail": r, "main": len(main), "comps": comps}
    return out


def _set_nets(src, dst, renames):
    """Write `dst` = `src` with the pads {(ref, num): net name} moved to those nets (a missing net is created)."""
    import pcbnew
    b = pcbnew.LoadBoard(src)
    made = {}
    for fp in b.GetFootprints():
        for p in fp.Pads():
            k = (fp.GetReference(), p.GetNumber())
            if k in renames:
                nm = renames[k]
                ni = b.FindNet(nm) or made.get(nm)
                if ni is None:
                    ni = made[nm] = pcbnew.NETINFO_ITEM(b, nm)
                    b.Add(ni)
                p.SetNet(ni)
    pcbnew.SaveBoard(dst, b)


def hold(src, dst, held):
    """`held` = {(ref, num): net}: those pads go to a dummy one-pad net `<net>~hold` for the router call."""
    _set_nets(src, dst, {k: n + HOLD for k, n in held.items()})


def unhold(path, held):
    _set_nets(path, path, dict(held))


def route(src, dst, cons, nets, track_mm, nlayers, sig, mk_cmd, runner, left_s, rec, status=None, max_rounds=24):
    """Route the open connections of `nets` class by class (see the module doc).  `mk_cmd(src, out, layers, {net: width}, nets)` builds the
    router command, `runner(cmd, log, cap_s=)` runs it, `left_s()` the seconds left.  Writes `dst`; `rec[net]` = {"routed": [{pads,
    width, amps, how}], "necked": [... proof], "open": [{pads, amps, tried, reason}], "unknown": [pads]}.  -> rc."""
    from . import power as PWR
    cur, state, wd = src, {}, os.path.dirname(dst)
    key = lambda net, c: (net, tuple(c["pads"]))
    for rnd in range(max_rounds):
        pl = plan(cur, cons, nets, track_mm, nlayers)
        calls = {}                                           # layers -> {net: (width, [comps routed], [held pad keys])}
        for net, p in pl.items():
            act = [c for c in p["comps"] if state.get(key(net, c), 0) < len(c["ladder"])]
            if not act:
                continue
            w = max(c["ladder"][state.get(key(net, c), 0)][0] for c in act)
            now = [c for c in act if c["ladder"][state.get(key(net, c), 0)][0] == w]
            held = {k: net for c in p["comps"] if c not in now for k in c["pads"]}
            lay = tuple(PWR.ladder_layers(cons, sig, w, fallback=True))
            calls.setdefault(lay, {})[net] = (w, now, held)
        if not calls or left_s() <= 0:
            break
        progressed = False
        for lay, group in calls.items():
            held = {k: n for _w, _c, h in group.values() for k, n in h.items()}
            hb = os.path.join(wd, f"{os.path.basename(dst)[:-10]}.b{rnd}.hold.kicad_pcb")
            out = hb[:-9] + "out.kicad_pcb"
            hold(cur, hb, held)
            _carry_pro(cur, hb)
            cmd = mk_cmd(hb, out, list(lay), {n: g[0] for n, g in group.items()}, sorted(group))
            rc, _s = runner(cmd, out[:-10] + ".log", cap_s=left_s())
            if rc not in (0, 3) or not os.path.exists(out):
                for net, (w, now, _h) in group.items():
                    for c in now:
                        state[key(net, c)] = len(c["ladder"])      # the router failed for these: reported open, never retried at 0.15
                        rec.setdefault(net, {}).setdefault("open", []).append({"pads": c["pads"], "amps": c["amps"], "tried": [w], "reason": f"router rc={rc}"})
                continue
            unhold(out, held)
            _carry_pro(cur, out)
            cur = out
            progressed = True
        after = plan(cur, cons, nets, track_mm, nlayers)
        for lay, group in calls.items():
            for net, (w, now, _h) in group.items():
                still = {tuple(c["pads"]) for c in (after.get(net) or {}).get("comps", [])}
                for c in now:
                    k = key(net, c)
                    if tuple(c["pads"]) in still:
                        state[k] = state.get(k, 0) + 1
                    else:
                        i = state.get(k, 0)
                        row = {"pads": c["pads"], "width": w, "amps": c["amps"], "how": c["how"]}
                        n = rec.setdefault(net, {})
                        n.setdefault("routed", []).append(row)
                        if c["ladder"][i][1] is not None:
                            n.setdefault("necked", []).append(dict(row, proof_rise_c=c["ladder"][i][1], len_mm=c["len_mm"]))
                        state[k] = len(c["ladder"])
        if not progressed:
            break
    final = plan(cur, cons, nets, track_mm, nlayers)
    for net, p in final.items():
        for c in p["comps"]:
            n = rec.setdefault(net, {})
            if not any(o["pads"] == c["pads"] for o in n.get("open", [])):
                n.setdefault("open", []).append({"pads": c["pads"], "amps": c["amps"], "tried": [x for x, _p in c["ladder"][:max(1, state.get(key(net, c), 0))]],
                                                 "reason": "no proven width fits" if state.get(key(net, c), 0) >= len(c["ladder"]) else "not routed"})
            if c["unknown"]:
                n.setdefault("unknown", []).append(c["pads"])
    import shutil
    if os.path.abspath(cur) != os.path.abspath(dst):
        shutil.copy(cur, dst)
    return 0


def _carry_pro(src, dst):
    import shutil
    s, t = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(s) and not os.path.exists(t):
        shutil.copy(s, t)


def summary(rec):
    routed = sum(len(v.get("routed", [])) for v in rec.values())
    necked = sum(len(v.get("necked", [])) for v in rec.values())
    left = sum(len(v.get("open", [])) for v in rec.values())
    return routed, necked, left
