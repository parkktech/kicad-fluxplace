"""Communication graph from parts + nets. Pure Python — no pcbnew.

A `Part` is {ref, value, kind, w, h, x, y}. `nets` is {netname: [refs...]}.
The graph excludes power/ground (handled as planes, not springs) and weights each
signal net by criticality so diff pairs / buses pull harder than stray control lines.
"""
import re
from collections import defaultdict

# component-ref prefixes that are in-line passives (traversable / collapsible)
PASSIVE_KINDS = {"R", "C", "L", "D", "FB", "F", "Y", "RT", "TP", "MH", "MK"}

_POWER_RE = re.compile(
    r"^(GND|GNDA|AGND|DGND|PGND|VSS|EARTH|VBUS|VCC|VDD|VEE|VREF|VIN|VOUT|"
    r"VSYS|VBAT|\+|\-|V\d)", re.I)


def kind_of(ref):
    m = re.match(r"[A-Za-z]+", ref)
    return m.group(0) if m else "?"


def is_passive(ref):
    return kind_of(ref) in PASSIVE_KINDS


def is_power_net(name, fanout, big_fanout=12):
    """Heuristic: name looks like a rail/ground, OR a huge-fanout net that is almost
    certainly a plane. Kept deliberately conservative so real buses stay signals."""
    n = name.strip()
    if _POWER_RE.match(n):
        return True
    # very high fanout + a power-ish token anywhere
    if fanout >= big_fanout and re.search(r"(GND|PWR|VBUS|VDD|VCC|RAIL|3V3|5V|12V|1V\d)", n, re.I):
        return True
    return False


_HS_RE = re.compile(r"(PCIE|USB|ETH|MDI|LVDS|DDR|HDMI|DP_|MIPI|SATA|RGMII|RMII|SERDES|_P$|_N$|_DP$|_DM$|CLK)", re.I)


def net_weight(name, fanout):
    """Criticality weight. High-speed/diff pairs pull hardest; point-to-point next;
    fat buses least per-edge (so they don't dominate)."""
    if _HS_RE.search(name):
        w = 4.0
    elif fanout <= 3:
        w = 2.0            # tight point-to-point (a signal + its series R, a clock)
    else:
        w = 1.0
    # normalise by clique size so a 20-node net doesn't overwhelm via sheer edge count
    return w / max(1, fanout - 1)


def classify(nets, big_fanout=12):
    """Split nets into power vs signal. Returns (power_names:set, signal:{name:[refs]})."""
    power, signal = set(), {}
    for name, refs in nets.items():
        refs = sorted(set(refs))
        if not name or name.startswith("unconnected") or len(refs) < 2:
            continue
        if is_power_net(name, len(refs), big_fanout):
            power.add(name)
        else:
            signal[name] = refs
    return power, signal


def build(parts, nets, big_fanout=12):
    """Return a CommGraph over KEY nodes (passives collapsed into edges)."""
    power, signal = classify(nets, big_fanout)

    # weighted adjacency over ALL parts first
    w_adj = defaultdict(lambda: defaultdict(float))
    plain_adj = defaultdict(set)
    for name, refs in signal.items():
        wt = net_weight(name, len(refs))
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                a, b = refs[i], refs[j]
                w_adj[a][b] += wt
                w_adj[b][a] += wt
                plain_adj[a].add(b)
                plain_adj[b].add(a)

    keys = [r for r in parts if not is_passive(r)]
    keyset = set(keys)

    # collapse passives: key<->key edge if reachable through passives only
    kg = defaultdict(lambda: defaultdict(float))
    for k in keys:
        for start in plain_adj[k]:
            stack, seen, acc = [start], {k}, defaultdict(float)
            # carry the direct edge weight to the first hop, then flood through passives
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                if c in keyset:
                    kg[k][c] += w_adj[k].get(c, 0.0) or 1.0
                    continue
                for nn in plain_adj[c]:
                    if nn not in seen:
                        stack.append(nn)
    return CommGraph(parts, power, signal, kg, keys)


class CommGraph:
    def __init__(self, parts, power, signal, kg, keys):
        self.parts = parts
        self.power_nets = power
        self.signal_nets = signal
        self.kg = kg              # {ref: {ref: weight}} key-node graph
        self.keys = keys

    def degree(self, ref):
        return len(self.kg.get(ref, {}))

    def wdegree(self, ref):
        return sum(self.kg.get(ref, {}).values())

    def neighbors(self, ref):
        return dict(self.kg.get(ref, {}))
