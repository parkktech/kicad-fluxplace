"""Channel budget: loss, skew, length, stubs and DC drop of a net / pair, from the parts at each end.

Data (constraints TOML, all keys optional, `source` required when any number is set):
  [channel.FAMILY]        FAMILY = net-name prefix (longest wins), like [pairs.F]
      loss_db_max / loss_db_target / freq_ghz          insertion loss at freq_ghz
      skew_ps_max / skew_ps_target                     intra-pair delay mismatch
      length_mm_max / length_mm_target                 pad-to-pad copper (inside-pad copper excluded)
      stub_mm_max                                      longest branch / via barrel stub
      via_db (loss per via), connector_db (fixed allowance)
      source = "datasheet p. N / research note"
  [[dcdrop]]  net, current_a, drop_mv_max, drop_mv_target, source  (optional temp_c, plating_um, from, to: `from` = "REF" | "REF.PAD"
              source (its pads on the net are one node), `to` = ["REF", "REF.PAD", ...] loads; drop = current_a x the WORST
              source->load-pad resistance, full current to every load pad (conservative). Without from/to: worst pad pair + a WARN)

Fallbacks (labelled in every row): no skew_ps_* -> [pairs.F] skew_max_mm (max) / skew_mm (target) converted with the
layer delay; [pairs.F] skew_mm alone is the old single limit (max, no target); [pairs.F] skew_ps likewise.

Tiers (D71cv): value > max = fail; target < value <= max = over-target-pass (reported with the margin); else ok.

Physics (closed forms; +-20 % class, never a field solver):
  delay          impedance.delay_ps_per_mm (Hammerstad microstrip / stripline sqrt(Er)/c)
  conductor loss alpha_c = Rs*K/(Z0*w) [Np/m], Rs = sqrt(pi f mu0 / sigma)        (Pozar, Microwave Engineering 3.199 form)
  roughness K    Hammerstad-Jensen: 1 + (2/pi) atan(1.4 (d_rms/d_skin)^2)                (Hammerstad & Jensen 1980)
  dielectric     alpha_d = 27.3 f sqrt(Dk_eff) Df / c  [dB/mm, f GHz, c mm/ns]            (= 8.686 pi f sqrt(Dk) Df / c)
  DC             R = rho L / (w t) per segment, via barrel rho h / (pi ((d/2+tp)^2-(d/2)^2))
"""
import heapq
import math
import re

from . import impedance as IM
from . import lengthmatch as LM

MU0 = 4e-7 * math.pi
SIGMA_CU = 5.8e7                 # S/m, annealed copper at 20 C
RHO_CU_20C = 1.72e-8             # ohm m at 20 C (IPC-2221 uses 1.724e-8)
ALPHA_CU = 0.00393               # 1/K temperature coefficient of copper resistivity
C_MM_PER_NS = 299.792458
DEFAULT_DF = 0.02                # FR-4 loss tangent when the template row carries none
DEFAULT_ROUGHNESS_UM = 1.0       # rms copper roughness, standard ED foil (template key roughness_rms_um overrides)
DEFAULT_PLATING_UM = 20.0        # via barrel plating, IPC-6012 class 2 average minimum
DEFAULT_FREQ_GHZ = 1.0
DETOUR = 1.15                    # Manhattan -> routed length factor for predict()
TOL = 0.02                       # mm, endpoint coincidence
SAMPLE = 0.02                    # mm, step for the inside-own-pad test
from . import channel_noise as CN

KEYS = CN.NOISE_KEYS + ("loss_db_max", "loss_db_target", "freq_ghz", "skew_ps_max", "skew_ps_target", "length_mm_max",
        "length_mm_target", "stub_mm_max", "via_db", "connector_db")


# ------------------------------------------------------------------ physics
def board_stack_rows(path):
    """[{type, thickness, epsilon_r, loss_tangent}] (copper and dielectric rows, in order) from the board's own (stackup),
    the numbers KiCad's tuning / DRC / calculator read. [] when the board has none."""
    import re
    try:
        raw = open(path, errors="replace").read()
    except (OSError, TypeError):
        return []
    m = re.search(r"\(stackup\b(.*?)\n\t\t\)\n", raw, re.S)
    out = []
    for blk in re.split(r'(?=\(layer\s+")', m.group(1) if m else ""):
        t = re.search(r'\(type\s+"([^"]+)"\)', blk)
        if not t or t.group(1) not in ("copper", "core", "prepreg"):
            continue
        g = lambda k: (re.search(r"\(%s\s+([\d.eE+-]+)" % k, blk) or [None, None])[1]
        out.append({"type": t.group(1), "thickness": float(g("thickness") or 0),
                    "epsilon_r": float(g("epsilon_r")) if g("epsilon_r") else None,
                    "loss_tangent": float(g("loss_tangent")) if g("loss_tangent") else None})
    return out


def stack_rows(cons, board_path=None):
    """-> (rows, planes, source). zgeom.resolve_stack rows (geometry: the constraints' template) plus loss_tangent and
    roughness. With board_path, epsilon_r and loss_tangent are taken from the board's own stackup when it has the same
    layer sequence, so loss, delay and KiCad read the same inputs (source says so; a differing sequence keeps the template)."""
    from . import zgeom as ZG
    from . import stackup as ST
    rows, planes, src = ZG.resolve_stack(cons)
    prof = ((cons or {}).get("stackup") or {}).get("profile")
    tl = ST.PROFILES.get(prof, {}).get("layers") if prof else None
    rough = float(((cons or {}).get("stackup") or {}).get("roughness_rms_um")
                  or (ST.PROFILES.get(prof, {}) if prof else {}).get("roughness_rms_um") or DEFAULT_ROUGHNESS_UM)
    br = board_stack_rows(board_path) if board_path else []
    same = bool(br) and [r["type"] for r in br] == [r["type"] for r in rows]
    out = []
    for i, r in enumerate(rows):
        r = dict(r)
        if tl and len(tl) == len(rows):
            r.setdefault("loss_tangent", tl[i].get("loss_tangent"))
        if same and r["type"] != "copper":
            for k in ("epsilon_r", "loss_tangent"):
                if br[i][k] is not None:
                    r[k] = br[i][k]
        r["roughness_rms_um"] = rough
        out.append(r)
    if same:
        src += " + epsilon_r/loss_tangent from the board stackup"
    return out, planes, src


def delay_ps_mm(rows, planes, layer, w_mm=None):
    """ps/mm on `layer` (None when no reference plane)."""
    return IM.delay_ps_per_mm(IM.geometry(rows, set(planes), layer), w_mm)


def skin_depth_um(f_ghz):
    return 1e6 * math.sqrt(1.0 / (math.pi * f_ghz * 1e9 * MU0 * SIGMA_CU))


def rough_factor(f_ghz, rms_um):
    """Hammerstad-Jensen roughness multiplier on conductor loss (1 smooth ... 2 rough)."""
    return 1.0 + 2.0 / math.pi * math.atan(1.4 * (rms_um / skin_depth_um(f_ghz)) ** 2)


def _layer_df(rows, planes, layer):
    """Thickness-weighted loss tangent of the dielectric rows between `layer` and its reference plane(s)."""
    idx = next((i for i, r in enumerate(rows) if r.get("name") == layer), None)
    if idx is None:
        return DEFAULT_DF
    num = den = 0.0
    for step in (-1, 1):
        i = idx + step
        seg = []
        while 0 <= i < len(rows):
            r = rows[i]
            if r.get("type") == "copper":
                if r.get("name") in planes:
                    num += sum(t * d for t, d in seg)
                    den += sum(t for t, _ in seg)
                    break
            else:
                seg.append((float(r.get("thickness") or 0), r.get("loss_tangent") or DEFAULT_DF))
            i += step
    return num / den if den else DEFAULT_DF


def loss_db_mm(rows, planes, layer, f_ghz, w_mm):
    """-> (conductor, dielectric) dB/mm of a trace of width w_mm on `layer` at f_ghz; None when no delay/Z0 (no plane)."""
    g = IM.geometry(rows, set(planes), layer)
    d = IM.delay_ps_per_mm(g, w_mm)
    if d is None:
        return None
    z0 = IM._z0(w_mm, g)
    rs = math.sqrt(math.pi * f_ghz * 1e9 * MU0 / SIGMA_CU)
    k = rough_factor(f_ghz, next((r.get("roughness_rms_um") for r in rows if r.get("name") == layer), None) or DEFAULT_ROUGHNESS_UM)
    ac = rs * k / (z0 * w_mm * 1e-3) * 8.686e-3          # Np/m -> dB/mm
    dk_eff = (d * IM.C_MM_PER_PS) ** 2
    ad = 27.3 * f_ghz * math.sqrt(dk_eff) * _layer_df(rows, planes, layer) / C_MM_NS
    return ac, ad


C_MM_NS = C_MM_PER_NS


def copper_t_mm(rows, layer):
    return next((float(r.get("thickness") or 0.035) for r in rows if r.get("name") == layer), 0.035)


def via_barrel_mm(rows, top, bot):
    """Centre-to-centre barrel length between two copper layers (stackup thickness, half of each copper excluded)."""
    v = LM.via_mm(rows, top, bot)
    if v is None:
        return None
    return max(0.0, v - (copper_t_mm(rows, top) + copper_t_mm(rows, bot)) / 2)


def path_metrics(path, rows, planes, f_ghz=DEFAULT_FREQ_GHZ, spec=None):
    """loss_db, delay_ps for one measured path. spec: a [channel.F] dict (via_db, connector_db). None parts = unknown."""
    spec = spec or {}
    loss, delay, unknown = 0.0, 0.0, []
    for lyr, mm in path["layer_mm"].items():
        if mm <= 0:
            continue
        w = path["layer_w"].get(lyr) or 0.15
        lo = loss_db_mm(rows, planes, lyr, f_ghz, w)
        dl = delay_ps_mm(rows, planes, lyr, w)
        if lo is None or dl is None:
            unknown.append(lyr)
            continue
        loss += mm * (lo[0] + lo[1])
        delay += mm * dl
    tot = sum(path["layer_mm"].values())
    if tot > 0 and path.get("via_mm"):
        delay += path["via_mm"] * delay / tot                 # barrel at the path's mean delay
    loss += path["vias"] * float(spec.get("via_db", 0.0)) + float(spec.get("connector_db", 0.0))
    return {"loss_db": None if unknown else loss, "delay_ps": None if unknown else delay, "unknown_layers": unknown}


def dc_resistance_mohm(path, rows, temp_c=20.0, plating_um=DEFAULT_PLATING_UM):
    """Path resistance in milliohm: sum rho*squares/t per layer plus via barrels."""
    rho = RHO_CU_20C * (1 + ALPHA_CU * (temp_c - 20.0))
    r = 0.0
    for lyr, sq in path["layer_sq"].items():
        r += rho * sq / (copper_t_mm(rows, lyr) * 1e-3)       # (L/w) dimensionless, t in m
    for v in path["via_list"]:
        h = (via_barrel_mm(rows, v["top"], v["bottom"]) or 1.6) * 1e-3
        ro, ri = v["drill"] / 2 * 1e-3 + plating_um * 1e-6, v["drill"] / 2 * 1e-3
        r += rho * h / (math.pi * (ro * ro - ri * ri)) / max(1, int(v.get("n", 1)))   # n: parallel barrels (planned paths)
    return r * 1e3


# ------------------------------------------------------------------ measurement
def _inside(pads, layer_id, x, y):
    import pcbnew
    pt = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    return any(p.IsOnLayer(layer_id) and p.HitTest(pt, 0) for p in pads)


def _eff_len(t, pads, mm):
    """Segment length (mm) with the part inside the net's own pads removed -> (effective, inside)."""
    import pcbnew
    if not pads:
        return mm, 0.0
    a, b = t.GetStart(), t.GetEnd()
    ax, ay, bx, by = (pcbnew.ToMM(v) for v in (a.x, a.y, b.x, b.y))
    chord = math.hypot(bx - ax, by - ay)
    n = max(1, int(chord / SAMPLE))
    lid = t.GetLayer()
    hit = sum(_inside(pads, lid, ax + (bx - ax) * (k + .5) / n, ay + (by - ay) * (k + .5) / n) for k in range(n))
    ins = mm * hit / n
    return max(0.0, mm - ins), ins



def _zone_islands(board, net, layer_names):
    """[(layer name, island index, polygon set, outline index, area mm2)] of the net's filled zones on `layer_names`."""
    out = []
    for z in board.Zones():
        if str(z.GetNetname()) != net or not z.IsFilled():
            continue
        for lid in z.GetLayerSet().Seq():
            ln = board.GetLayerName(lid)
            if ln not in layer_names:
                continue
            ps = z.GetFilledPolysList(lid)
            for k in range(ps.OutlineCount()):
                a = abs(ps.Outline(k).Area()) - sum(abs(ps.Hole(k, h).Area()) for h in range(ps.HoleCount(k)))
                out.append((ln, ps, k, a / 1e12))
    return out


def zone_squares(d, area, short, a):
    """Squares between two contacts of radius `a` (mm) a distance `d` apart in a filled island (area mm2, short bbox side
    mm): d / w_s + ln(min(d, w_s) / a) / pi, w_s = min(area / d, short). The first term is a strip of the island's mean
    width over that length (the bbox side stops a thin island borrowing area it cannot use); the second is the spreading
    resistance of the two contacts (two discs on a sheet: ln(d / a) / pi), which dominates when the island is wide."""
    w_s = max(min(area / d, short), 1e-6)
    return d / w_s + math.log(max(min(d, w_s) / max(a, 1e-3), 1.0)) / math.pi


def _zone_links(board, net, nodes, link, res):
    """Filled zone copper of the net as conductors. OUR OWN MATH (D71da / LESSONS-W15 rule 1: KiCad has no resistance
    calculation): every pair of nodes (pad, via barrel, track end) that a filled island of the net on a layer contains
    (KiCad's fill polygon holds the node's centre) is joined by one edge of zone_squares(). An ESTIMATE of a 2-D sheet,
    not a bound: strip + contact spreading is right for a compact island; an island with slots, a U shape or a neck
    between the two nodes has more resistance than this (not modelled). Nodes = [(node, x, y, layer-name set, contact
    radius mm)]. Only nodes the fill actually contains are joined; a net with no filled zone gets no edge."""
    import pcbnew
    layer_names = {l for _n, _x, _y, ls, _a in nodes for l in ls}
    for ln, ps, k, area in _zone_islands(board, net, layer_names):
        bb = ps.Outline(k).BBox()
        short = min(bb.GetWidth(), bb.GetHeight()) / 1e6
        inside = [(n, x, y, a) for n, x, y, ls, a in nodes if ln in ls
                  and ps.Contains(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)), k)]
        for i, (u, ux, uy, ua) in enumerate(inside):
            for v, vx, vy, va in inside[:i]:              # every pair once
                d = math.hypot(ux - vx, uy - vy)
                if d <= TOL:
                    link(u, v)
                    continue
                sq = max(zone_squares(d, area, short, min(ua, va)), 1e-9)
                info = {"kind": "zone", "layer": ln, "mm": d, "raw": d, "inside": 0.0, "w": d / sq, "id": -1}
                link(u, v, sq if res else d, info)


def _graph(board, net, rows, zones=False):
    """-> nodes dict: adj {node: [(node, w, info)]}, pads [(node, label)], segs list, vias list. `zones`: add the net's
    filled zone copper as conductors (_zone_links) and weigh every edge in squares (resistance) instead of mm."""
    import pcbnew
    names = {board.GetLayerName(i): i for i in range(pcbnew.PCB_LAYER_ID_COUNT)}
    copper = [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]
    pads, segs, vias = [], [], []
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if str(p.GetNetname()) == net and not p.IsOnLayer(pcbnew.Edge_Cuts):
                pads.append(p)
    for t in board.GetTracks():
        if str(t.GetNetname()) != net:
            continue
        if t.GetClass() == "PCB_VIA":
            vias.append(t)
        elif t.GetClass() in ("PCB_TRACK", "PCB_ARC"):
            segs.append(t)
    adj, nid = {}, [0]

    def new():
        nid[0] += 1
        adj[nid[0]] = []
        return nid[0]

    def link(u, v, w=0.0, info=None):
        adj[u].append((v, w, info))
        adj[v].append((u, w, info))
    pad_nodes = [(new(), f"{p.GetParentFootprint().GetReference()}.{p.GetNumber()}", p) for p in pads]
    ends, seginfo = [], []
    for k, t in enumerate(segs):
        mm = t.GetLength() / 1e6
        eff, ins = _eff_len(t, pads, mm)
        lyr = board.GetLayerName(t.GetLayer())
        info = {"kind": "seg", "layer": lyr, "mm": eff, "raw": mm, "inside": ins, "w": t.GetWidth() / 1e6, "id": k}
        a, b = new(), new()
        link(a, b, eff / info["w"] if zones and info["w"] else eff, info)
        s, e = t.GetStart(), t.GetEnd()
        ends += [(a, pcbnew.ToMM(s.x), pcbnew.ToMM(s.y), t.GetLayer()), (b, pcbnew.ToMM(e.x), pcbnew.ToMM(e.y), t.GetLayer())]
        seginfo.append(info)
    vnodes = []
    for v in vias:
        pos = v.GetPosition()
        top, bot = board.GetLayerName(v.TopLayer()), board.GetLayerName(v.BottomLayer())
        i, j = copper.index(top) if top in copper else 0, copper.index(bot) if bot in copper else len(copper) - 1
        span = set(copper[min(i, j):max(i, j) + 1])
        vnodes.append((new(), pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), v.GetWidth() / 2e6, span,
                       {"top": top, "bottom": bot, "x": pcbnew.ToMM(pos.x), "y": pcbnew.ToMM(pos.y), "drill": v.GetDrill() / 1e6}))
    for i, (u, ux, uy, ul) in enumerate(ends):
        for j, (w, wx, wy, wl) in enumerate(ends[:i]):
            if ul == wl and math.hypot(ux - wx, uy - wy) <= max(TOL, min(seginfo[i // 2]["w"], seginfo[j // 2]["w"]) / 2):
                link(u, w)              # k15: an end inside the other end's copper is joined (KiCad does; powerflow.build too)
        for vn, vx, vy, vr, span, _vi in vnodes:
            if board.GetLayerName(ul) in span and math.hypot(ux - vx, uy - vy) <= vr + TOL:
                link(u, vn, 0.0, {"kind": "via-link", "layer": board.GetLayerName(ul)})
        pt = pcbnew.VECTOR2I(pcbnew.FromMM(ux), pcbnew.FromMM(uy))
        for pn, _lab, p in pad_nodes:
            if p.IsOnLayer(ul) and p.HitTest(pt, int(TOL * 1e6)):
                link(u, pn)
    for vn, vx, vy, vr, span, _vi in vnodes:
        pt = pcbnew.VECTOR2I(pcbnew.FromMM(vx), pcbnew.FromMM(vy))
        for pn, _lab, p in pad_nodes:
            if any(names.get(l) is not None and p.IsOnLayer(names[l]) for l in span) and p.HitTest(pt, 0):
                link(vn, pn, 0.0, {"kind": "via-pad"})
    if zones:
        pcb_names = {board.GetLayerName(i) for i in range(pcbnew.PCB_LAYER_ID_COUNT)}
        znodes = [(n, pcbnew_mm(p.GetPosition().x), pcbnew_mm(p.GetPosition().y),
                   {l for l in pcb_names if p.IsOnLayer(names[l])},
                   min(p.GetBoundingBox().GetWidth(), p.GetBoundingBox().GetHeight()) / 2e6) for n, _l, p in pad_nodes]
        znodes += [(vn, vx, vy, set(span), vr) for vn, vx, vy, vr, span, _vi in vnodes]
        znodes += [(n, x, y, {board.GetLayerName(l)}, seginfo[i // 2]["w"] / 2) for i, (n, x, y, l) in enumerate(ends)]
        _zone_links(board, net, znodes, link, True)
    return adj, pad_nodes, seginfo, {vn: vi for vn, _x, _y, _r, _s, vi in vnodes}


def _dijkstra(adj, src):
    dist, prev, h = {src: 0.0}, {}, [(0.0, src)]
    while h:
        d, u = heapq.heappop(h)
        if d > dist.get(u, math.inf):
            continue
        for v, w, info in adj[u]:
            if d + w < dist.get(v, math.inf):
                dist[v], prev[v] = d + w, (u, info)
                heapq.heappush(h, (d + w, v))
    return dist, prev


def _stubs(adj, trunk_nodes, trunk_edges, pad_lab, vmap, rows, trunk_layers):
    """Branches hanging off the trunk: longest leaf distance from each junction, plus unused via barrel."""
    out, claimed = [], set()
    for j in trunk_nodes:
        for v, w, info in adj[j]:
            if frozenset((j, v)) in trunk_edges or v in trunk_nodes or v in claimed:
                continue
            best, hit, stack, vis = 0.0, [], [(v, w)], {j, v}
            while stack:
                u, d = stack.pop()
                best = max(best, d)
                if u in pad_lab:
                    hit.append(pad_lab[u])
                for x, wx, _i in adj[u]:
                    if x not in vis and x not in trunk_nodes:
                        vis.add(x)
                        stack.append((x, d + wx))
            claimed |= vis
            if best > 0.005:
                out.append({"kind": "trace", "mm": round(best, 4), "to": ",".join(hit) or "open end"})
    return out


def _path(adj, pad_nodes, seginfo, vmap, rows, src, dst, prev, dist, loc_pad_lab):
    chain, u = [], dst
    while u != src:
        p, info = prev[u]
        chain.append((p, u, info))
        u = p
    chain.reverse()
    layer_mm, layer_w, layer_sq, vias, nodes, edges, seq = {}, {}, {}, [], {src}, set(), []
    inside = 0.0
    for p, u, info in chain:
        nodes.add(u)
        edges.add(frozenset((p, u)))
        if info and info.get("kind") in ("seg", "zone"):
            l = info["layer"]
            layer_mm[l] = layer_mm.get(l, 0.0) + info["mm"]
            layer_sq[l] = layer_sq.get(l, 0.0) + info["mm"] / info["w"]
            layer_w[l] = layer_w.get(l, 0.0) + info["mm"] * info["w"]
            inside += info["inside"]
            seq.append(l)
        if u in vmap and p not in vmap:
            vias.append((u, len(seq)))
    for l in layer_w:
        layer_w[l] = layer_w[l] / layer_mm[l] if layer_mm[l] else 0.0
    return chain, layer_mm, layer_w, layer_sq, vias, nodes, edges, seq, inside


def pcbnew_mm(v):
    return v / 1e6


def orientations(pp, pn):
    """Pair P's pad-to-pad paths with N's by endpoints. A P pad's partner is the nearest N pad on the same footprint
    (ref = label before the dot). -> [(P path, N path or None, "a>b" label)] for every P path joining two footprints."""
    npads = {}
    for q in pn:
        if q.get("found"):
            npads[q["a"]], npads[q["b"]] = q["a_xy"], q["b_xy"]

    def partner(lbl, xy):
        c = [(math.hypot(xy[0] - v[0], xy[1] - v[1]), k) for k, v in npads.items() if k.split(".")[0] == lbl.split(".")[0]]
        return min(c)[1] if c else None
    byend = {frozenset((q["a"], q["b"])): q for q in pn if q.get("found")}
    out = []
    for q in pp:
        if not q.get("found") or q["a"].split(".")[0] == q["b"].split(".")[0]:
            continue
        na, nb = partner(q["a"], q["a_xy"]), partner(q["b"], q["b_xy"])
        out.append((q, byend.get(frozenset((na, nb))) if na and nb else None, f"{q['a']}>{q['b']}"))
    return out


def _pad_layers(board, pad, cu):
    """Copper layers (names in `cu`) the pad has copper on."""
    import pcbnew
    ids = {board.GetLayerName(i): i for i in range(pcbnew.PCB_LAYER_ID_COUNT)}
    return [l for l in cu if ids.get(l) is not None and pad.IsOnLayer(ids[l])]


def measure_paths(board, nets, rows, mode="trunk", zones=False):
    """{net: [path]} per signal path, pad to pad. mode "trunk": the path between the net's two farthest pads (branches
    off it are stubs); "all": every pad pair. path = {net, a, b, found, length_mm, layer_mm, layer_w, layer_sq, vias,
    via_list, via_mm, stubs [{kind, mm, to|at}], stub_max_mm, inside_pad_mm, copper_total_mm}. Copper inside the
    net's own pads is excluded from length (and from the loss and DC squares). Zones are not measured, unless `zones`
    (DC drop only): then filled zone copper conducts (_zone_links) and the trunk is the worst pair in squares."""
    if isinstance(board, str):
        from . import kicad_io as IO
        board = IO.load(board)
    out = {}
    for net in nets:
        adj, pad_nodes, seginfo, vmap = _graph(board, net, rows, zones)
        lab = {n: l for n, l, _p in pad_nodes}
        xy = {n: (pcbnew_mm(p_.GetPosition().x), pcbnew_mm(p_.GetPosition().y)) for n, _l, p_ in pad_nodes}
        total = sum(s["raw"] for s in seginfo)
        if len(pad_nodes) < 2:
            out[net] = [{"net": net, "found": False, "why": f"{len(pad_nodes)} pad(s)", "copper_total_mm": total}]
            continue
        pads_by_node = {pn: p_ for pn, _l, p_ in pad_nodes}
        dists = {pn: _dijkstra(adj, pn) for pn, _l, _p in pad_nodes}
        pairs = [(a, b) for i, (a, _l, _p) in enumerate(pad_nodes) for b, _l2, _p2 in pad_nodes[i + 1:] if b in dists[a][0]]
        if not pairs:
            out[net] = [{"net": net, "found": False, "why": "pads not connected", "copper_total_mm": total}]
            continue
        if mode == "trunk":
            pairs = [max(pairs, key=lambda ab: dists[ab[0]][0][ab[1]])]
        res = []
        for a, b in pairs:
            dist, prev = dists[a]
            chain, lmm, lw, lsq, vias, nodes, edges, seq, ins = _path(adj, pad_nodes, seginfo, vmap, rows, a, b, prev, dist, lab)
            vl = [dict(vmap[u]) for u, _k in vias]
            stubs = _stubs(adj, nodes, edges, lab, vmap, rows, seq)
            vmm = 0.0
            cu = [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]
            for (u, k), v in zip(vias, vl):      # barrel between the layers either side is signal; barrel stubs: pairaudit.via_stubs (one function)
                used = [cu.index(l) for l in (seq[k - 1] if k > 0 else None, seq[k] if k < len(seq) else None) if l in cu]
                # a via inside the path's end pad (via-in-pad: pad > via > B): the pad's own layers are served at that end
                if k == 0:
                    used += [cu.index(l) for l in _pad_layers(board, pads_by_node[a], cu)]
                if k == len(seq):
                    used += [cu.index(l) for l in _pad_layers(board, pads_by_node[b], cu)]
                if not used or v["top"] not in cu or v["bottom"] not in cu:
                    continue
                lo, hi = min(used), max(used)
                vmm += via_barrel_mm(rows, cu[lo], cu[hi]) or 0.0
            res.append({"net": net, "a": lab[a], "b": lab[b], "a_xy": xy[a], "b_xy": xy[b], "found": True, "length_mm": round(dist[b], 4),
                        "layer_mm": {k: round(v, 4) for k, v in lmm.items()}, "layer_w": lw, "layer_sq": lsq,
                        "vias": len(vl), "via_list": vl, "via_mm": round(vmm, 4), "stubs": stubs,
                        "stub_max_mm": max([s["mm"] for s in stubs], default=0.0),
                        "inside_pad_mm": round(ins, 4), "inside_pad_total_mm": round(sum(x["inside"] for x in seginfo), 4),
                        "copper_total_mm": round(total, 4)})
        out[net] = res
    return out


def _group(pad_nodes, spec):
    """Pad nodes of `spec` ("REF" = every pad of REF on the net, "REF.PAD" = one pad)."""
    spec = str(spec)
    return [n for n, lab, _p in pad_nodes if lab == spec or lab.split(".")[0] == spec]


def measure_src_load(board, net, rows, src, loads, zones=True):
    """[dcdrop] from/to: [path] one per load: copper from the source group (its pads are one node: joined at zero
    resistance) to the WORST pad of each load group (every supply pad of a load must hold the budget; load pads are never
    joined, so no path shortcuts through a part). A load with a pad the source does not reach is not found.
    path = measure_paths' keys layer_sq, via_list plus a, b (the load as given), pad, found, contrib [{layer, w, mm, sq}]
    (top squares first)."""
    adj, pad_nodes, _seg, vmap = _graph(board, net, rows, zones)
    lab = {n: l for n, l, _p in pad_nodes}
    sg = _group(pad_nodes, src)
    for u in sg[1:]:
        adj[sg[0]].append((u, 0.0, None))
        adj[u].append((sg[0], 0.0, None))
    dist, prev = _dijkstra(adj, sg[0]) if sg else ({}, {})
    out = []
    for ld in loads:
        lg = _group(pad_nodes, ld)
        if not sg or not lg:
            out.append({"net": net, "a": str(src), "b": str(ld), "found": False,
                        "why": f"{ld if sg else src} has no pad on {net}"})
            continue
        cut = [n for n in lg if n not in dist]
        if cut:
            out.append({"net": net, "a": str(src), "b": str(ld), "found": False, "why": f"{src} and {lab[cut[0]]} not connected"})
            continue
        l0 = max(lg, key=lambda n: (dist[n], lab[n]))
        chain, lmm, lw, lsq, vias, _n, _e, _seq, _ins = _path(adj, pad_nodes, [], vmap, rows, sg[0], l0, prev, dist, lab)
        contrib = sorted(({"layer": i["layer"], "w": i["w"], "mm": i["mm"], "sq": i["mm"] / i["w"], "kind": i["kind"]}
                          for _p, _u, i in chain if i and i.get("kind") in ("seg", "zone") and i.get("w")),
                         key=lambda c: -c["sq"])
        out.append({"net": net, "a": str(src), "b": str(ld), "pad": lab[l0], "found": True, "layer_sq": lsq,
                    "via_list": [dict(vmap[u]) for u, _k in vias], "contrib": contrib})
    return out


# ------------------------------------------------------------------ data + verdict
def _family(table, net, legs=None):
    from . import budget
    return budget.family_of(table, net, legs)


def resolve_budget(cons, master, ps_per_mm=None):
    """-> (budget dict of the KEYS above, labels {key: where it came from}, family or None). The body is budget.pair_limits:
    review (evaluate) and the budget table (budget.pair_rows) read the same limits."""
    from . import budget
    return budget.pair_limits(cons, master, ps_per_mm)


def verdict(value, target, mx):
    """-> (verdict, margin to max or None, over target or None)."""
    if value is None or (target is None and mx is None):
        return "n/a", None, None
    if mx is not None and value > mx:
        return "fail", mx - value, (value - target) if target is not None else None
    if target is not None and value > target:
        return "over-target-pass", (mx - value) if mx is not None else None, value - target
    return "ok", (mx - value) if mx is not None else None, None


_RANK = {"n/a": 0, "ok": 1, "over-target-pass": 2, "fail": 3}


def _metric(name, unit, value, target, mx, label, digits=1, minimum=False):
    """minimum=True: `mx` is a floor (fail below it) and `target` a floor too; margin/over count the shortfall."""
    if minimum:
        v, margin, over = verdict(None if value is None else -value, None if target is None else -target, None if mx is None else -mx)
    else:
        v, margin, over = verdict(value, target, mx)
    return {"minimum": minimum, "name": name, "unit": unit, "value": value, "target": target, "max": mx, "margin": margin,
            "over_target": over, "verdict": v, "source": label, "digits": digits}


def _legs(pairs_map, names):
    """[(master, partner or None)] for every net, each pair once."""
    seen, out = set(), []
    for n in sorted(names):
        if n in seen:
            continue
        q = pairs_map.get(n)
        seen.add(n)
        if q and q in names:
            seen.add(q)
            out.append(tuple(sorted((n, q), reverse=True)))   # P (or DP) first
        else:
            out.append((n, None))
    return out


_NKEY = {"xtalk": "xtalk_db_max", "spacing": "spacing_w_min", "broadside": "broadside_mm_max", "plane": "plane_gap_mm_max",
         "return_via": "return_via_mm", "noisy": "noisy_mm"}


def _routing_board(board, cache):
    """routediag.load dict of the board file (copper, zones with fills, pads), once per evaluate."""
    if "B" not in cache:
        from . import routediag as RD
        cache["B"] = RD.load(board.GetFileName())
    return cache["B"]


def dcdrop_row(d, path, rows):
    """One [[dcdrop]] entry `d` + one path (measured by measure_src_load / measure_paths, or planned by
    powerbudget.synth_path) -> the row (kind "dc"). The ONE DC-drop verdict: planner and review share it (LESSONS rule 5)."""
    p = path
    row = {"name": d["net"], "kind": "dc", "legs": [d["net"]], "family": None, "labels": {}, "metrics": []}
    if not p.get("found"):
        row.update(verdict="n/a", why=p.get("why", "no path"))
    else:
        r = dc_resistance_mohm(p, rows, float(d.get("temp_c", 20.0)), float(d.get("plating_um", DEFAULT_PLATING_UM)))
        mv = r * float(d["current_a"])
        m = _metric("dcdrop", "mV", mv, d.get("drop_mv_target"), d.get("drop_mv_max"), f"[[dcdrop]] {d.get('source', 'NO SOURCE')}", 1)
        m.update(r_mohm=r, current_a=float(d["current_a"]))
        row.update(metrics=[m], verdict=m["verdict"], paths=[p])
    return row


def dcdrop_rows(board, cons, rows):
    """One row per [[dcdrop]] entry (kind "dc"): DC drop on the routed copper vs its budget. The ONE DC-drop computation
    (evaluate and power.dcdrop_status both call it)."""
    out = []
    dc = [d for d in cons.get("dcdrop", []) or []]
    if dc:
        meas2 = measure_paths(board, sorted({d["net"] for d in dc if not d.get("from")}), rows, zones=True)
        for d in dc:
            if d.get("from"):
                ld = d.get("to") or []
                ps = measure_src_load(board, d["net"], rows, d["from"], [ld] if isinstance(ld, str) else ld)
                bad = [p for p in ps if not p["found"]]
                ps = [p for p in ps if p["found"]]
                p = max(ps, key=lambda q: dc_resistance_mohm(q, rows)) if ps and not bad else (bad[0] if bad else {"found": False, "why": "no `to` loads"})
            else:
                p = (meas2.get(d["net"]) or [{"found": False}])[0]
            out.append(dcdrop_row(d, p, rows))
    return out


def evaluate(board, cons, spec=None, nets=None):
    """-> rows [{name, kind pair|net|dc, legs, metrics [..], verdict, family, labels}] for every net / pair that a
    [channel.F] family covers (differential pairs only; a net outside a pair never gets channel metrics), plus every [[dcdrop]] entry. `spec` (the spec JSON) is accepted for the caller's
    convenience and not needed: pairs come from the board's net names (netclass._pairs)."""
    if isinstance(board, str):
        from . import kicad_io as IO
        board = IO.load(board)
    from . import netclass as NC
    cons = cons or {}
    rows, planes, _src = stack_rows(cons, board.GetFileName())
    allnets = sorted({str(n) for n in board.GetNetsByName().keys() if str(n)})
    pm = NC._pairs(set(allnets), cons)
    cover = [n for n in allnets if _family(cons.get("channel"), n)]
    if nets:
        cover = [n for n in cover if n in set(nets)]
    _cache = {}
    legs = [l for l in _legs(pm, set(cover)) if l[1]]            # differential pairs only (netclass pair detection)
    meas = measure_paths(board, sorted({x for l in legs for x in l}), rows, mode="all")
    out = []
    for lg in legs:
        names = list(lg)
        master = names[0]
        row = {"name": os_commonname(names), "kind": "pair", "legs": names, "metrics": []}
        from . import netlen as NL
        try:
            o = NL.orient(_routing_board(board, _cache), names[0], names[1], meas, cons) if len(meas[names[0]]) + len(meas[names[1]]) > 2 else None
        except (OSError, ValueError):                                     # an in-memory board with no file: the plain pairing
            o = None
        ors = o["ors"] if o else orientations(meas[names[0]], meas[names[1]])      # one end-to-end path: the plain pairing
        if not ors:
            why = "; ".join(f"{n}: {meas[n][0].get('why', 'no path')}" for n in names if not meas[n][0].get("found")) or "no matching pad-to-pad path"
            row.update(verdict="n/a", family=_family(cons["channel"], master), labels={}, why=why)
            out.append(row)
            continue
        paths = [q for pq, nq, _l in ors for q in (pq, nq) if q]
        lay = {}
        for q in paths:
            for l, m in q["layer_mm"].items():
                lay[l] = lay.get(l, 0.0) + m
        mean_ps = IM.route_delay_ps_per_mm(rows, set(planes), lay)
        b, lab, fam = resolve_budget(cons, master, mean_ps)
        f = b.get("freq_ghz", DEFAULT_FREQ_GHZ)
        pmx = {id(q): path_metrics(q, rows, planes, f, b) for q in paths}
        row.update(family=fam, labels=lab, paths=paths)
        if "loss_db_max" in b or "loss_db_target" in b:
            ls = [m["loss_db"] for m in pmx.values()]
            row["metrics"].append(_metric("loss", "dB", None if None in ls else max(ls), b.get("loss_db_target"), b.get("loss_db_max"), lab.get("loss_db_max") or lab.get("loss_db_target"), 2))
        if "skew_ps_max" in b or "skew_ps_target" in b:
            per, mms = [], []
            for pq, nq, lbl in ors:
                dp, dn = pmx[id(pq)]["delay_ps"], (pmx[id(nq)]["delay_ps"] if nq else None)
                per.append((lbl, abs(dp - dn) if dp is not None and dn is not None else None))
                mms.append(abs(pq["length_mm"] - nq["length_mm"]) if nq else None)
            vals = [v for _l, v in per if v is not None]
            m = _metric("skew", "ps", max(vals) if vals else None, b.get("skew_ps_target"), b.get("skew_ps_max"), lab.get("skew_ps_max") or lab.get("skew_ps_target"), 2)
            if vals:
                m["mm"] = mms[[v for _l, v in per].index(max(vals))]          # the same orientation, in mm (KiCad DRC units)
            if len(per) > 1 or any(v is None for _l, v in per):
                m["detail"] = "; ".join(f"{l} {'unmatched' if v is None else format(v, '.1f')}" for l, v in per)
            row["metrics"].append(m)
        if "length_mm_max" in b or "length_mm_target" in b:
            row["metrics"].append(_metric("length", "mm", max(q["length_mm"] for q in paths), b.get("length_mm_target"), b.get("length_mm_max"), lab.get("length_mm_max") or lab.get("length_mm_target"), 1))
        if "stub_mm_max" in b:
            from . import pairaudit as PA
            try:
                vs = max([r["stub_mm"] for r in PA.via_stubs(_routing_board(board, _cache), names)] or [0.0])      # via barrel stubs: the one function
            except (OSError, ValueError):                                 # an in-memory board with no file
                vs = 0.0
            tp = max([x["mm"] for x in (o["taps"] + o["spurs"])] + [0.0]) if o else 0.0                  # taps and duplicated-row spurs (NL.orient)
            row["metrics"].append(_metric("stub", "mm", max(max(q["stub_max_mm"] for q in paths), vs, tp), None, b["stub_mm_max"], lab["stub_mm_max"], 2))
        if any(k in b for k in CN.NOISE_KEYS):
            B = _routing_board(board, _cache)
            if "cls" not in _cache:
                from . import netclass as NC
                _cache["cls"] = NC.classify(B, cons, spec)
            for x in CN.analyse(B, rows, planes, names, b, cons, spec, _cache["cls"]):
                m = _metric(x["name"], x["unit"], x["value"], x["target"], x["limit"], lab.get(_NKEY.get(x["name"])) or
                            f"[channel.{fam}] (noise)", x["digits"], x["minimum"])
                m["detail"] = (m.get("detail") or "") + x["detail"]
                row["metrics"].append(m)
        row["verdict"] = max((m["verdict"] for m in row["metrics"]), key=_RANK.get, default="n/a")
        out.append(row)
    out += dcdrop_rows(board, cons, rows)
    return out


def os_commonname(names):
    """PCIE_TX_P + PCIE_TX_N -> "PCIE_TX_P/N"; USB_DP_J + USB_DM_J -> "USB_DP_J/DM_J" (cut at a "_" boundary)."""
    if len(names) == 1:
        return names[0]
    c = len(__import__("os.path", fromlist=["x"]).commonprefix(names))
    c = names[0].rfind("_", 0, c) + 1
    return names[0] + "/" + names[1][c:]


def _fmt(m):
    d = m["digits"]
    v = ("none" if str(m.get("detail", "")).startswith("no ") else "?") if m["value"] is None else f"{m['value']:.{d}f}"
    if m.get("minimum"):
        mx = "" if m["max"] is None else f" (min {m['max']:g})"
        s = f"{m['name']} {v} {m['unit']}{mx}"
    else:
        mx = "" if m["max"] is None else f"/{m['max']:g}"
        s = f"{m['name']} {v}{mx} {m['unit']}"
    if m.get("mm") is not None:
        s += f" ({m['mm']:.2f} mm)"
    if m["verdict"] == "over-target-pass":
        s += f" (target {m['target']:g}, +{m['over_target']:.{d}f} {'short' if m.get('minimum') else 'over'}: pass)"
    elif m["verdict"] == "fail":
        s += f" (FAIL, {-m['margin']:.{d}f} {'under min' if m.get('minimum') else 'over max'})"
    if m.get("detail"):
        s += f" [{m['detail']}]"
    return s


def line(row):
    if row["kind"] == "dc":
        if not row["metrics"]:
            return f"DCDROP {row['name']}: not measured ({row.get('why')})"
        m = row["metrics"][0]
        return f"DCDROP {row['name']}: {_fmt(m)} at {m['current_a']:g} A, R {m['r_mohm']:.2f} mohm [{row['verdict']}]"
    if not row["metrics"]:
        return f"CHANNEL {row['name']}: not measured ({row.get('why', 'no budget keys')})"
    return f"CHANNEL {row['name']}: " + ", ".join(_fmt(m) for m in row["metrics"]) + f" [{row['verdict']}]"


def report(rows, log=print, labels=True):
    """One line per pair / net / dcdrop, then (labels) where each number's budget came from. Returns the fail count."""
    nfail = 0
    for r in rows:
        log(line(r))
        nfail += r["verdict"] == "fail"
        if labels:
            for m in r["metrics"]:
                if m["source"] and (m["source"].startswith("fallback") or "PLACEHOLDER" in m["source"] or "NO SOURCE" in m["source"]):
                    log(f"    {m['name']} budget: {m['source']}")
    return nfail


def check(board_path, cons):
    """Review section: [(level, code, msg, refs)] -> fail = CHANNEL_MAX (FAIL), over target = CHANNEL_TARGET (INFO, passes,
    says how far over), a [channel.F] number with no `source` = CHANNEL_SOURCE (WARN). Nothing when the constraints have
    no [channel.*] / [[dcdrop]]."""
    cons = cons or {}
    out = []
    for f, e in (cons.get("channel") or {}).items():
        if any(k in e for k in KEYS) and not e.get("source"):
            out.append(("WARN", "CHANNEL_SOURCE", f"[channel.{f}] sets budget numbers with no `source`", []))
        elif str(e.get("source", "")).upper().startswith("PLACEHOLDER"):
            out.append(("WARN", "CHANNEL_SOURCE", f"[channel.{f}] budget is a PLACEHOLDER, not from the parts", []))
    for d in cons.get("dcdrop", []) or []:
        if not (d.get("from") and d.get("to")):
            out.append(("WARN", "CHANNEL_DCDROP_PAIR", f"dcdrop {d.get('net')}: no from/to, worst pad pair used", [d.get("net")]))
    if not (cons.get("channel") or cons.get("dcdrop")):
        return out
    for r in evaluate(board_path, cons):
        if r["kind"] == "dc" and r["verdict"] == "n/a":       # a budget that could not be measured is never silent
            out.append(("WARN", "CHANNEL_DCDROP_UNMEASURED", f"dcdrop {r['name']}: not measured ({r.get('why') or 'no budget number'})", r["legs"]))
        for m in r["metrics"]:
            if m["verdict"] == "fail":
                out.append(("FAIL", "CHANNEL_MAX", f"{r['name']}: {_fmt(m)} [{m['source']}]", r["legs"]))
            elif m["verdict"] == "over-target-pass":
                out.append(("INFO", "CHANNEL_TARGET", f"{r['name']}: {_fmt(m)} [{m['source']}]", r["legs"]))
    return out


# ------------------------------------------------------------------ placement hook
def predict(endpoints, layer, budget, stack=None, detour=DETOUR, w_mm=0.15):
    """Pre-routing estimate for one connection. endpoints [(x_mm, y_mm), (x_mm, y_mm)]; budget = resolve_budget()[0];
    stack = (rows, planes) to add loss and delay. -> {length_mm, loss_db, delay_ps, length_margin_mm, loss_margin_db,
    max_mismatch_mm (skew budget as copper on this layer), verdict}. Not wired into placement yet."""
    (x0, y0), (x1, y1) = endpoints
    ln = (abs(x1 - x0) + abs(y1 - y0)) * detour
    out = {"length_mm": ln, "loss_db": None, "delay_ps": None, "max_mismatch_mm": None}
    mx = budget.get("length_mm_max")
    out["length_margin_mm"] = None if mx is None else mx - ln
    out["loss_margin_db"] = None
    if stack:
        rows, planes = stack
        lo = loss_db_mm(rows, planes, layer, budget.get("freq_ghz", DEFAULT_FREQ_GHZ), w_mm)
        dl = delay_ps_mm(rows, planes, layer, w_mm)
        if lo and dl:
            out["loss_db"] = ln * (lo[0] + lo[1]) + float(budget.get("connector_db", 0.0))
            out["delay_ps"] = ln * dl
            if "loss_db_max" in budget:
                out["loss_margin_db"] = budget["loss_db_max"] - out["loss_db"]
            if "skew_ps_max" in budget:
                out["max_mismatch_mm"] = budget["skew_ps_max"] / dl
    vs = [verdict(ln, budget.get("length_mm_target"), mx)[0]]
    if out["loss_db"] is not None:
        vs.append(verdict(out["loss_db"], budget.get("loss_db_target"), budget.get("loss_db_max"))[0])
    out["verdict"] = max(vs, key=_RANK.get)
    return out
