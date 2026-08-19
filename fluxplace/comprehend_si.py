"""Circuit comprehension — infer the electrical intent tables the rest of the
pipeline consumes, from nothing but the netlist + geometry (the same tables a
Quilter-class tool shows for review):

  power / power_traces  which nets are planes vs routed rails (graph classes)
  pairs                 differential pairs {slave: master} by naming convention
  bypass                [(cap, ic, rail, dist_mm)] every rail-to-GND cap and the
                        pin it decouples (si.infer_bypass)
  crystals              [{crystal, parent, nets, load_caps}] resonators and the
                        oscillator pins they belong to

Placement consumes these (decap/crystal adjacency), routing consumes them
(pair hugging, rail widths), verification consumes them (skew, proximity).
Emit as TOML for engineer review; constraints.toml overrides win downstream.
"""


def crystals(parts, net_members):
    """Detect 2-terminal resonators: ref Y*, or a footprint/value naming a
    crystal/resonator. Parent = the IC sharing the most crystal nets (its OSC
    pins). Load caps = 2-pin capacitors from a crystal net to GND."""
    out = []
    for r, p in parts.items():
        label = (p.get("footprint", "") + " " + p.get("value", "")).lower()
        if not (r[:1] == "Y" or "crystal" in label or "resonator" in label
                or "xtal" in label):
            continue
        pn = [n for n in p.get("pins", {}) if n != "GND"]
        if not 1 <= len(pn) <= 2:
            continue
        cands = {}
        for n in pn:
            for q in net_members.get(n, []):
                if q != r and q[:1] == "U":
                    cands[q] = cands.get(q, 0) + 1
        if not cands:
            continue
        parent = max(sorted(cands), key=lambda q: cands[q])
        lcaps = sorted(c for c, cp in parts.items()
                       if c[:1] == "C" and len(cp.get("pins", {})) == 2
                       and "GND" in cp["pins"] and set(cp["pins"]) & set(pn))
        out.append(dict(crystal=r, parent=parent, nets=sorted(pn),
                        load_caps=lcaps))
    return sorted(out, key=lambda c: c["crystal"])


def comprehend(parts, nets, graph):
    """The full inference bundle. `nets` = {net: [refs]} from read_board."""
    from . import si as SI
    from .graph import diff_pairs
    pairs = {s: m for s, m in diff_pairs(
        {n: len(v) for n, v in graph.signal_nets.items()}).items()
        if m in graph.signal_nets}
    return dict(
        power=sorted(graph.power_nets),
        power_traces=sorted(getattr(graph, "power_traces", {})),
        pairs=pairs,
        bypass=SI.infer_bypass(parts, nets, set(graph.power_nets)),
        crystals=crystals(parts, nets),
    )


def to_toml(comp):
    """Reviewable TOML rendering (matches the constraints.toml vocabulary where
    the sections overlap, so a reviewed line can be pasted across)."""
    L = ["# fluxplace circuit comprehension — review, then override in your"]
    L.append("# constraints.toml (engineer numbers always win downstream)\n")
    L.append("[inferred]")
    L.append(f"power_planes = {comp['power']!r}")
    L.append(f"power_traces = {comp['power_traces']!r}\n")
    for s, m in sorted(comp["pairs"].items()):
        L.append(f'[inferred.pair."{m}"]')
        L.append(f'negative = "{s}"\n')
    for c, ic, rail, d in sorted(comp["bypass"]):
        L.append(f'[inferred.bypass.{c}]')
        L.append(f'decouples = "{ic}"')
        L.append(f'rail = "{rail}"')
        L.append(f"distance_mm = {d:.1f}\n")
    for cl in comp["crystals"]:
        L.append(f'[inferred.crystal.{cl["crystal"]}]')
        L.append(f'parent = "{cl["parent"]}"')
        L.append(f'nets = {cl["nets"]!r}')
        L.append(f'load_caps = {cl["load_caps"]!r}\n')
    return "\n".join(L) + "\n"
