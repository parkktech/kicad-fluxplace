"""Tree-flow current solve of one power rail (W40). Pure geometry + graph, no pcbnew: power.py feeds it plain data.

The rail's copper (track segments, vias, pads, zone fills) becomes a graph: nodes = junctions (track ends that touch, a via, a
pad, a zone fill), edges = track pieces (a segment is split where another end, via or pad lands on its side). The source pads
are one root. The current through an edge is what lies downstream of it:

  * bridge edge (a tree branch): exactly the sum of the loads behind it;
  * edge inside a loop / pour mesh (2-edge-connected block): bounded by the loads of the whole block plus everything behind it
    (conservative: the split inside the mesh is not solved, no edge of a mesh can carry more than what the mesh feeds);
  * a piece with no source path (open copper) or a rail with no source pad on its copper: the loads of its own connected copper.

A load whose current is unknown (`amps` None) makes every edge upstream of it unknown (flow None, with the refs): "unknown" only
where a load on that branch is unknown. Loads that are None-free and zero (caps, gates) cost nothing. """
import math

TOUCH_MM = 0.02                      # two track ends closer than this are one junction (power.NECK_JOIN_MM)


def _seg_len(g):
    return math.hypot(g["x2"] - g["x1"], g["y2"] - g["y1"])


class _UF:
    def __init__(self, n=0):
        self.p = list(range(n))

    def add(self):
        self.p.append(len(self.p))
        return len(self.p) - 1

    def find(self, i):
        p = self.p
        while p[i] != i:
            p[i] = p[p[i]]
            i = p[i]
        return i

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def _layers_ok(a, b):
    return a is None or b is None or bool(a & b)


def build(segs, vias, pads, zones=()):
    """-> (uf, edges, pad_node, terminals). edges = [(u, v, seg_index, length_mm)] with u, v node ids (union-find nodes, resolve
    with uf.find); pad_node[i] = node id of pads[i]."""
    uf = _UF()
    T = []                                              # terminals: dict(x, y, layers (set | None = all), rad, box, node, seg, kind)

    def term(x, y, layers, rad, kind, seg=None, box=None):
        T.append({"x": x, "y": y, "layers": layers, "rad": rad, "kind": kind, "seg": seg, "box": box, "node": uf.add()})
        return len(T) - 1
    ends = []
    for i, g in enumerate(segs):
        L = {g["layer"]}
        ends.append((term(g["x1"], g["y1"], L, TOUCH_MM, "end", i), term(g["x2"], g["y2"], L, TOUCH_MM, "end", i)))
    for x, y, r in vias:
        term(x, y, None, max(r, TOUCH_MM), "via")
    pad_t = []
    for p in pads:
        pad_t.append(term(p["x"], p["y"], p.get("layers"), 0.0, "pad", box=p.get("box")))
    cell = 1.0
    grid = {}
    for k, t in enumerate(T):
        grid.setdefault((int(t["x"] // cell), int(t["y"] // cell)), []).append(k)

    def near(x, y, reach):
        c = int(math.ceil(reach / cell))
        cx, cy = int(x // cell), int(y // cell)
        for dx in range(-c, c + 1):
            for dy in range(-c, c + 1):
                yield from grid.get((cx + dx, cy + dy), ())
    for k, t in enumerate(T):                           # point contacts: ends / vias by radius
        if t["kind"] == "pad":
            continue
        for j in near(t["x"], t["y"], max(t["rad"], 0.0) + 0.5):
            u = T[j]
            if j <= k or u["kind"] == "pad" or not _layers_ok(t["layers"], u["layers"]):
                continue
            if math.hypot(t["x"] - u["x"], t["y"] - u["y"]) <= max(t["rad"], u["rad"]):
                uf.union(t["node"], u["node"])
    for k in pad_t:                                     # a pad takes every terminal of a shared layer inside its box
        t = T[k]
        bx = t["box"]
        for j, u in enumerate(T):
            if j == k or not _layers_ok(t["layers"], u["layers"]):
                continue
            if bx is not None and bx[0] <= u["x"] <= bx[2] and bx[1] <= u["y"] <= bx[3]:
                uf.union(t["node"], u["node"])
            elif bx is None and math.hypot(t["x"] - u["x"], t["y"] - u["y"]) <= TOUCH_MM:
                uf.union(t["node"], u["node"])
    edges = []
    for i, g in enumerate(segs):                        # split each segment where something lands on its side
        L = _seg_len(g)
        a, z = ends[i]
        cuts = []
        if L > 0.02:
            ux, uy = (g["x2"] - g["x1"]) / L, (g["y2"] - g["y1"]) / L
            half = float(g.get("width") or 0.0) / 2 + TOUCH_MM
            for j in near((g["x1"] + g["x2"]) / 2, (g["y1"] + g["y2"]) / 2, L / 2 + half + 0.5):
                u = T[j]
                if j in (a, z) or u["seg"] == i or not _layers_ok({g["layer"]}, u["layers"]):
                    continue
                if u["kind"] == "pad":
                    continue
                s = (u["x"] - g["x1"]) * ux + (u["y"] - g["y1"]) * uy
                d = abs(-(u["x"] - g["x1"]) * uy + (u["y"] - g["y1"]) * ux)
                if TOUCH_MM < s < L - TOUCH_MM and d <= half:
                    cuts.append((s, j))
            for k in pad_t:                             # a pad whose centre sits on the track side
                u = T[k]
                if not _layers_ok({g["layer"]}, u["layers"]):
                    continue
                s = (u["x"] - g["x1"]) * ux + (u["y"] - g["y1"]) * uy
                d = abs(-(u["x"] - g["x1"]) * uy + (u["y"] - g["y1"]) * ux)
                if TOUCH_MM < s < L - TOUCH_MM and d <= half:
                    cuts.append((s, k))
        cuts.sort()
        chain = [(0.0, a)] + cuts + [(L, z)]
        for (s0, n0), (s1, n1) in zip(chain, chain[1:]):
            edges.append((T[n0]["node"], T[n1]["node"], i, s1 - s0))
    for zn in zones:                                    # one node per zone fill layer
        node = uf.add()
        for t in T:
            if (t["layers"] is None or zn["layer"] in t["layers"]) and zn["contains"](t["x"], t["y"]):
                uf.union(node, t["node"])
    return uf, edges, [T[k]["node"] for k in pad_t], T


RHO_MM = 1.72e-5                     # copper resistivity, ohm * mm at 20 C
MAX_SOLVE_NODES = 4000


def _kirchhoff(E, segs, roots, pad_node, pads, f, blk, reach, subu):
    """Edge currents (A, absolute) of the resistive network: every track piece a resistor rho * L / (w * t) (t = seg["t_mm"],
    default 1 oz), vias / pads / pour fills ideal nodes, source nodes at 0 V, each load a current sink. Only when the loads
    reached from the source are all known; {} otherwise (or when the copper is too big for the dense solve). -> {edge index: A}."""
    try:
        import numpy as np
    except ImportError:                                 # pragma: no cover
        return {}
    if any(subu.get(b_) for b_ in reach):
        return {}
    nodes = sorted({x for u, v, _i, _l, virt in E for x in (u, v) if blk.get(x) in reach})
    if not nodes or len(nodes) > MAX_SOLVE_NODES:
        return {}
    ix = {n: j for j, n in enumerate(nodes)}
    n = len(nodes)
    G = np.zeros((n, n))
    g_of = {}
    for k, (u, v, i, ln, virt) in enumerate(E):
        if u == v or u not in ix or v not in ix:
            continue
        if virt:
            g = 1e9
        else:
            sg = segs[i]
            g = max(float(sg.get("width") or 0.0), 0.02) * float(sg.get("t_mm") or 0.035) / (RHO_MM * max(ln, 0.05))
        g_of[k] = g
        a, b = ix[u], ix[v]
        G[a, a] += g
        G[b, b] += g
        G[a, b] -= g
        G[b, a] -= g
    inj = np.zeros(n)
    for nd, p in zip(pad_node, pads):
        nd = f(nd)
        if p["role"] == "load" and nd in ix and p.get("amps"):
            inj[ix[nd]] -= float(p["amps"])
    fixed = {ix[r] for r in roots if r in ix}
    free = [j for j in range(n) if j not in fixed]
    if not fixed or not free:
        return {}
    Gf = G[np.ix_(free, free)] + np.eye(len(free)) * 1e-9
    try:
        vf = np.linalg.solve(Gf, inj[free])
    except np.linalg.LinAlgError:
        return {}
    V = np.zeros(n)
    V[free] = vf
    return {k: abs(g * (V[ix[E[k][0]]] - V[ix[E[k][1]]])) for k, g in g_of.items()}


def solve(segs, vias, pads, zones=(), cap=None):
    """Current through every segment. pads: [{ref, x, y, layers, box, role: "source" | "load", amps: float | None}] (a load with
    amps None is unknown). -> {"flow": [ {amps | None, unk: [refs], mode: "tree"|"mesh"|"unreached"|"nosource"|"dangling"} per
    segment ], "roots": n source pads, "loads": n}. `cap`: the source's total (a branch never carries more than the source
    gives); None = no cap."""
    uf, edges, pad_node, _T = build(segs, vias, pads, zones)
    f = uf.find
    adj = {}
    E = []                                              # (u, v, seg, len, virtual)
    for u, v, i, ln in edges:
        u, v = f(u), f(v)
        E.append((u, v, i, ln, False))
    roots = sorted({f(n) for n, p in zip(pad_node, pads) if p["role"] == "source"})
    for r in roots[1:]:                                 # several source pads: one virtual root, joined by virtual edges
        E.append((roots[0], r, None, 0.0, True))
    for k, (u, v, _i, _l, _vr) in enumerate(E):
        adj.setdefault(u, [])
        adj.setdefault(v, [])
        if u != v:                                      # a self-loop piece (both ends in one pour): parallel to the pour, bounded by its block
            adj[u].append((v, k))
            adj[v].append((u, k))
    # bridges (iterative Tarjan on the multigraph)
    disc, low, bridge = {}, {}, set()
    for s0 in list(adj):
        if s0 in disc:
            continue
        disc[s0] = low[s0] = len(disc)
        stack = [(s0, -1, iter(adj[s0]))]
        while stack:
            u, pe, it = stack[-1]
            adv = False
            for v, k in it:
                if k == pe:
                    continue
                if v in disc:
                    low[u] = min(low[u], disc[v])
                else:
                    disc[v] = low[v] = len(disc)
                    stack.append((v, k, iter(adj[v])))
                    adv = True
                    break
            if not adv:
                stack.pop()
                if stack:
                    w = stack[-1][0]
                    low[w] = min(low[w], low[u])
                    if low[u] > disc[w]:
                        bridge.add(pe)
    # 2-edge-connected blocks
    blk = {}
    nb = 0
    for s0 in adj:
        if s0 in blk:
            continue
        blk[s0] = nb
        st = [s0]
        while st:
            u = st.pop()
            for v, k in adj[u]:
                if k not in bridge and v not in blk:
                    blk[v] = nb
                    st.append(v)
        nb += 1
    load = [0.0] * nb
    unk = [set() for _ in range(nb)]
    nload = [0] * nb
    for n, p in zip(pad_node, pads):
        if p["role"] != "load":
            continue
        n = f(n)
        if n not in blk:                                # a lone pad with no copper: nothing to carry
            continue
        b_ = blk[n]
        nload[b_] += 1
        if p.get("amps") is None:
            unk[b_].add(p["ref"])
        else:
            load[b_] += float(p["amps"])
    tadj = {}                                           # bridge tree between blocks
    for k in bridge:
        u, v = E[k][0], E[k][1]
        tadj.setdefault(blk[u], []).append((blk[v], k))
        tadj.setdefault(blk[v], []).append((blk[u], k))
    sub, subu, subn, parent, order = {}, {}, {}, {}, []
    seen = set()
    rblk = [blk[r] for r in roots if r in blk]

    def walk(root):
        seen.add(root)
        parent[root] = None
        st = [root]
        while st:
            u = st.pop()
            order.append(u)
            for v, k in tadj.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    parent[v] = (u, k)
                    st.append(v)
    for r in rblk[:1]:
        walk(r)
    reach = set(seen)
    for b_ in reversed(order):                          # leaves first
        sub[b_] = sub.get(b_, 0.0) + load[b_]
        subu[b_] = subu.get(b_, set()) | unk[b_]
        subn[b_] = subn.get(b_, 0) + nload[b_]
        pr = parent[b_]
        if pr is not None:
            sub[pr[0]] = sub.get(pr[0], 0.0) + sub[b_]
            subu[pr[0]] = subu.get(pr[0], set()) | subu[b_]
            subn[pr[0]] = subn.get(pr[0], 0) + subn[b_]
    # copper not reached from the source: its own connected block tree, the whole total (no direction known)
    comp_tot, comp_unk, comp_n = {}, {}, {}
    cid = {}
    for s0 in adj:
        if blk[s0] in reach or s0 in cid:
            continue
        st, c = [s0], s0
        cid[s0] = c
        mem = []
        while st:
            u = st.pop()
            mem.append(u)
            for v, _k in adj[u]:
                if v not in cid:
                    cid[v] = c
                    st.append(v)
        comp_tot[c] = 0.0
        comp_unk[c] = set()
        comp_n[c] = 0
        for b_ in {blk[u] for u in mem}:
            comp_tot[c] += load[b_]
            comp_unk[c] |= unk[b_]
            comp_n[c] += nload[b_]
    ik = _kirchhoff(E, segs, roots, pad_node, pads, f, blk, reach, subu) if roots else {}
    flow = [None] * len(segs)
    for k, (u, v, i, _ln, virt) in enumerate(E):
        if virt or i is None:
            continue
        if blk[u] in reach:
            if k in bridge:
                child = blk[v] if parent.get(blk[v]) and parent[blk[v]][1] == k else blk[u]
                amps, us, n, mode = sub[child], subu[child], subn[child], "tree"
            else:
                amps, us, n, mode = sub[blk[u]], subu[blk[u]], subn[blk[u]], "mesh"
                if k in ik and not us:                  # the loops are solved (Kirchhoff on the copper), never above the bound
                    amps, mode = min(amps, ik[k]), "mesh-solve"
        else:
            c = cid[u]
            amps, us, n = comp_tot[c], comp_unk[c], comp_n[c]
            mode = "unreached" if roots else "nosource"
        if n == 0:
            mode = "dangling"
        cur = {"amps": None if us else (min(amps, cap) if cap else amps), "unk": sorted(us), "mode": mode, "loads": n}
        old = flow[i]
        if old is None:
            flow[i] = cur
        else:                                           # a segment split into pieces: the worst piece rules
            if old["amps"] is None or cur["amps"] is None:
                old["amps"] = None
                old["unk"] = sorted(set(old["unk"]) | set(cur["unk"]))
            else:
                old["amps"] = max(old["amps"], cur["amps"])
            if cur["mode"] in ("mesh", "mesh-solve") or old["mode"] == "dangling":
                old["mode"] = cur["mode"] if old["mode"] not in ("mesh", "mesh-solve") else old["mode"]
                old["loads"] = max(old["loads"], cur["loads"])
    for i, g in enumerate(segs):
        if flow[i] is None:                             # a zero-length / self-loop piece: no flow figure of its own
            flow[i] = {"amps": 0.0, "unk": [], "mode": "dangling", "loads": 0}
    return {"flow": flow, "roots": len(roots), "loads": sum(1 for p in pads if p["role"] == "load")}
