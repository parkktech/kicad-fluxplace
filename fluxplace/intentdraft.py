"""`fluxplace intent-draft BOARD [--spec S] [--constraints C] [--out FILE]`: PROPOSE an `[intent]` section for the constraints.

A draft for the owner to edit, never applied by the tool. Every block carries its evidence:
  # derived: ...   read from the board / spec (footprint names, rule areas, net classes, the netlist); written as live TOML
  # guess: ...     a plausible reading the files do not prove (a number, a role); written COMMENTED OUT (`#? `), so pasting
                   the draft never applies a guess: the owner uncomments what is right.
What it looks for: connectors by entry direction (connectors.py), parts that carry their own keep-out rule area (antenna
modules), RF chains (nets of class rf, walked part to part), IC support groups (clusters.py), board-to-board / card sockets
with stand-offs (rigid group + swept area) and the space under mezzanine connectors, switching converters near RF parts.
"""
import json
import math
import os
import re

from . import connectors as CN
from . import geom as GM
from . import intent as IT

_CARD = re.compile(r"M\.?2|NGFF|mPCIe|PCIe|card[-_ ]?edge|SIM|SD[-_ ]?card", re.I)
_B2B = re.compile(r"mezzanine|board[-_ ]?to[-_ ]?board|b2b|DF\d\d", re.I)
_STANDOFF = re.compile(r"^(H|MH|SO|ST)\d")


def _q(refs):
    return "[" + ", ".join(f'"{r}"' for r in refs) + "]"


def draft(B, cons=None, spec=None):
    """-> (text, stats)."""
    from . import clusters as CL, netclass as NC
    parts = B["parts"]
    out = ["# intent draft: EDIT before use. `# derived:` lines are read from the board; `#? ` lines are GUESSES (commented out).",
           "[intent]", ""]
    stats = {"derived": 0, "guess": 0, "ask": 0}

    def block(kind, why, lines):
        stats[kind] += 1
        out.append(f"# {kind}: {why}")
        out.extend(lines if kind == "derived" else ["#? " + l for l in lines])
        out.append("")

    # ---- connectors
    rows = CN.table(B, cons, CN.answers_of(cons))
    for r in rows:
        ref, p = r["ref"], parts[r["ref"]]
        if r["entry"] == "side":
            g = IT.edge_gaps(B, p["court"])
            s = min(g, key=g.get)
            at_edge = g[s] <= 1.5
            lines = ["[[intent.edge]]", f'refs = ["{ref}"]', f'side = "{s if at_edge else "any"}"', 'facing = "out"']
            if r["key"]:
                lines.append(f'front = "{r["key"]}"')
            if g[s] < 0:
                lines.append(f"overhang_mm = {math.ceil(-g[s] * 10) / 10:g}")
            where = f"sits on the {s} edge now" if at_edge else f"is {g[s]:.1f} mm from the nearest edge now"
            block("derived" if r["how"] in ("name", "declared", "answer") else "guess",
                  f"{ref} is a side-entry connector ({r['how']}: {r['fp']}); it {where}", lines)
        elif r["entry"] == "unknown":
            stats["ask"] += 1
            out += [f"# ask: {ref} ({r['fp']}): entry direction not derivable; answer connector-entry:{r['fp']} = side|top",
                    f'#? [intent.connectors.{ref}]', f'#? entry = "top"', ""]
    tops = [r["ref"] for r in rows if r["entry"] == "top"]
    if tops:
        out += [f"# derived: top-entry connectors (plug from above; kept out from under height regions / swept areas): {', '.join(tops)}", ""]

    # ---- parts with their own keep-out rule area (antenna end)
    for ref in sorted(parts):
        if any(r["ref"] == ref for r in rows):
            continue
        f = IT.front_vec(B, cons, ref)
        if f is None:
            continue
        key = CN.local_key(parts[ref], f)
        block("derived", f"{ref} ({str(parts[ref]['fp']).split(':')[-1]}) carries a keep-out rule area on its {key} side "
              f"(an antenna or a mating zone): that side should face out across an edge",
              ["[[intent.edge]]", f'refs = ["{ref}"]', 'side = "any"', 'facing = "out"', f'front = "{key}"'])

    # ---- RF chains
    cls = NC.classify(B, cons, spec)
    pads = NC.net_pads(B)
    rf = sorted(n for n, i in cls.items() if i["class"] == "rf")
    rfset = set(rf)
    ends = sorted(r for r in {q["ref"] for n in rf for q in pads.get(n, ())}
                  if parts[r]["n_pads"] >= 3 or CN.is_connector(r, parts[r]))
    chains, shunts, done = [], set(), set()
    for a0 in ends:                                  # walk from every end through series two-pad parts to the next end
        for n0 in sorted({q["net"] for q in parts[a0]["pads"] if q["net"] in rfset}):
            path, net, used = [a0], n0, {n0}
            while net:
                nxt_net = None
                on = sorted({q["ref"] for q in pads[net]} - set(path))
                end = next((r for r in on if r in ends), None)
                for r in on:
                    if r in ends:
                        continue
                    other = [q["net"] for q in parts[r]["pads"] if q["net"] != net]
                    if parts[r]["n_pads"] == 2 and other and other[0] in rfset and other[0] not in used and not end:
                        path.append(r)
                        nxt_net = other[0]
                        used.add(nxt_net)
                        break
                    shunts.add(r)
                if end:
                    path.append(end)
                    break
                net = nxt_net
            key = frozenset((path[0], path[-1]))
            if len(path) >= 2 and path[-1] in ends and key not in done:
                done.add(key)
                chains.append(path)
    for path in chains:
        nets = sorted({n for n in rf if {q["ref"] for q in pads[n]} & set(path)})
        bud = [cls[n]["budget"] for n in nets if cls[n].get("budget")]
        now = sum(IT.link(B, a, b)[0] for a, b in zip(path, path[1:]))
        lines = ["[[intent.near]]", f"chain = {_q(path)}"]
        block("derived", f"RF chain over net(s) {', '.join(nets[:6])} (class rf); pad-to-pad {now:.1f} mm now", lines + ["hard = false"]
              + ([f"max_mm = {sum(bud):g}        # sum of the nets' class budgets"] if bud else []))
        if not bud:
            block("guess", "no budget on these nets: a length limit is the owner's number", [f"# max_mm for chain {path[0]}..{path[-1]}"])

    # ---- IC support
    cl, _ = CL.build(B, cons, spec)
    n_sup = sum(1 for c in cl for s in c["supports"] if s["sure"])
    far = CL.far_parts(cl)
    if n_sup:
        block("derived", f"{len(cl)} IC(s) own {n_sup} support part(s) by the netlist (decoupling, feedback, boot, switch node); "
              f"{len(far)} beyond their limit now", ["[intent.support]", "follow = true", "decap_hard = true"])

    # ---- card sockets with stand-offs, mezzanine connectors
    holes = [r for r in sorted(parts) if _STANDOFF.match(r) or not any(not q.get("np") for q in parts[r]["pads"])]
    for ref in sorted(parts):
        name = str(parts[ref]["fp"]).split(":")[-1]
        if _CARD.search(name) and parts[ref]["n_pads"] >= 6:
            c = parts[ref]["court"]
            near = [h for h in holes if GM.rect_gap(c, parts[h]["court"]) <= 90.0 and (
                abs((parts[h]["court"][1] + parts[h]["court"][3]) / 2 - (c[1] + c[3]) / 2) <= (c[3] - c[1]) / 2 + 1.0
                or abs((parts[h]["court"][0] + parts[h]["court"][2]) / 2 - (c[0] + c[2]) / 2) <= (c[2] - c[0]) / 2 + 1.0)]
            if near:
                block("guess", f"{ref} ({name}) is a card socket and {', '.join(near[:4])} line(s) up with it: socket + stand-off(s) "
                      f"move as one body and the card between them is occupied space; which stand-off is used and the clearance "
                      f"under the card are the owner's",
                      ["[[intent.group]]", f'name = "{ref}_card"', f"refs = {_q([ref] + near[:2])}", 'swept = "all"',
                       "swept_max_height_mm = 1.0"])
        elif (_B2B.search(name) or (CN.is_connector(ref, parts[ref]) and parts[ref]["n_pads"] >= 60
                                    and any(r["ref"] == ref and r["entry"] != "side" for r in rows))):
            block("guess", f"{ref} ({name}, {parts[ref]['n_pads']} pads) looks like a board-to-board connector: a module sits above "
                  f"it; the module outline and the height under it are not in the board file",
                  ["[[intent.region]]", f'name = "under_{ref}_module"', f'under = "{ref}"', "margin_mm = 10.0",
                   f'side = "{parts[ref]["side"][0]}"', "max_height_mm = 1.5"])

    # ---- switching converters vs RF parts
    regs = sorted({c["ic"] for c in cl if c.get("regulator")})
    sw = sorted(set(regs) | {s["ref"] for c in cl if c.get("regulator") for s in c["supports"] if s["cls"] == "switch_node"})
    rfp = sorted({r for p in chains for r in p})
    if sw and rfp:
        d = min(GM.rect_gap(parts[a]["court"], parts[b]["court"]) for a in sw for b in rfp)
        block("guess", f"switching converter parts ({', '.join(sw[:6])}) and RF chain parts ({', '.join(rfp[:6])}) are "
              f"{d:.1f} mm apart now; the distance to keep is the owner's number",
              ["[[intent.away]]", f"a = {_q(sw)}", f"b = {_q(rfp)}", "min_mm = 15.0", "hard = false"])
    return "\n".join(out).rstrip() + "\n", stats


def cmd_intent_draft(a):
    from . import constraints as C, routediag as RD
    spec = json.load(open(a.spec)) if a.spec else None
    cons = C.load(a.constraints)
    B = RD.load(a.board)
    text, st = draft(B, cons, spec)
    path = a.out or os.path.splitext(a.board)[0] + ".intent-draft.toml"
    with open(path, "w") as fh:
        fh.write(text)
    print(f"intent-draft: {st['derived']} derived block(s), {st['guess']} guess(es) (commented out), {st['ask']} to ask; "
          f"wrote {path} (a draft: edit it, then paste into the constraints)")
    if getattr(a, "full_output", False):
        print(text)


def add_parser(sub):
    p = sub.add_parser("intent-draft", help="propose an [intent] section from the board + spec: derived lines live, guesses "
                                            "commented out (docs/INTENT.md)")
    p.add_argument("board")
    p.add_argument("--constraints", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--out", default=None, help="draft file (default BOARD.intent-draft.toml)")
    p.add_argument("--full", dest="full_output", action="store_true", help="also print the draft")
    p.set_defaults(fn=cmd_intent_draft)
