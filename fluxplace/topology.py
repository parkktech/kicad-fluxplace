"""Derive signal-flow structure from a CommGraph: hub, branches, forks, lint.

Board-agnostic — nothing here knows about any specific project's net names.
"""
from collections import deque
from .graph import is_passive, kind_of


class Topology:
    def __init__(self, hub, branches, forks, isolated, graph):
        self.hub = hub                # ref of the highest-degree node (the trunk)
        self.branches = branches      # list of Branch, biggest first
        self.forks = forks            # refs with key-degree >= 3
        self.isolated = isolated      # key refs with NO signal lanes (lint: bug or pure-power)
        self.graph = graph


class Branch:
    def __init__(self, root, members, order, to_hub):
        self.root = root             # the branch's edge connector (or seed)
        self.members = members       # set of all refs in the branch (incl. passives)
        self.order = order           # ordered edge -> hub
        self.to_hub = to_hub         # does it actually reach the hub?

    def __len__(self):
        return len(self.members)


def _signal_adj(graph):
    """Full part-level adjacency (incl. passives) over signal nets."""
    from collections import defaultdict
    adj = defaultdict(set)
    for refs in graph.signal_nets.values():
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                adj[refs[i]].add(refs[j])
                adj[refs[j]].add(refs[i])
    return adj


def analyze(graph, prefer_hub=None):
    kg = graph.kg
    keys = graph.keys
    # hub = max weighted-degree key node (or caller override)
    hub = prefer_hub or (max(keys, key=lambda r: (graph.wdegree(r), graph.degree(r)))
                         if keys else None)
    forks = sorted([k for k in kg if graph.degree(k) >= 3],
                   key=lambda r: -graph.degree(r))
    isolated = [k for k in keys if graph.degree(k) == 0]

    adj = _signal_adj(graph)
    # branches = connected clusters after removing the hub
    seen = {hub} if hub else set()
    branches = []
    for r in graph.parts:
        if r in seen or r not in adj:
            continue
        cluster, q = set(), deque([r])
        seen.add(r)
        while q:
            c = q.popleft()
            cluster.add(c)
            for nb in adj[c]:
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        branches.append(_make_branch(cluster, adj, hub, graph))
    branches.sort(key=lambda b: -len(b))
    return Topology(hub, branches, forks, isolated, graph)


def _make_branch(cluster, adj, hub, graph):
    # deterministic root: sets iterate in salted-hash order, so sort before picking
    # (an unsorted pick here made entire layouts vary run to run)
    conns = sorted(r for r in cluster if kind_of(r) == "J")
    root = (conns[0] if conns
            else max(sorted(cluster), key=lambda r: (len(adj[r] & cluster), r)))
    dist = {root: 0}
    q = deque([root])
    while q:
        c = q.popleft()
        for nb in adj[c] & cluster:
            if nb not in dist:
                dist[nb] = dist[c] + 1
                q.append(nb)
    for r in cluster:
        dist.setdefault(r, 99)
    order = sorted(cluster, key=lambda r: (dist[r], is_passive(r), r))
    to_hub = any(hub in graph.kg.get(r, {}) for r in cluster) if hub else False
    return Branch(root, cluster, order, to_hub)


def summary(topo):
    g = topo.graph
    lines = [f"hub: {topo.hub}  (deg {g.degree(topo.hub)}, wdeg {g.wdegree(topo.hub):.1f})",
             f"forks (deg>=3): {', '.join(topo.forks) or 'none'}",
             f"branches: {len(topo.branches)}",
             f"signal nets: {len(g.signal_nets)}   power nets: {len(g.power_nets)}"]
    if topo.isolated:
        lines.append(f"LINT — key parts with NO signal lanes: {', '.join(topo.isolated)}")
    for b in topo.branches:
        flow = " -> ".join(r for r in b.order if not is_passive(r)) or b.root
        lines.append(f"  [{len(b):2}p] {b.root:6} {'->hub' if b.to_hub else '     '}  {flow}")
    return "\n".join(lines)
