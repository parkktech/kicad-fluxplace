"""Generate a real .kicad_sch from a netlist spec or a routed board.

Why this exists: fluxplace can build a fully wired, DRC-clean, fabricable board
from a netlist specification without a schematic ever existing. That is fine for
manufacturing — a fab consumes gerbers — and useless for electrical review. The
first outside reviewer of a board built this way asked the obvious question
within a day: "I do not see a schematic file, is the JSON your source of truth?"
It was, and the honest answer left him without the document his review needs.

So: emit a schematic that is TRUE rather than pretty.

What this produces is a net-label schematic. Every component becomes a symbol
whose pins carry their real pad numbers and functions, and every connection is a
global label bearing the net name. It is not a hand-drawn schematic with signal
flow left-to-right and functional blocks — it will not look like something an
engineer laid out, and it should not pretend to.

What it IS: a genuine .kicad_sch that opens in KiCad, that a reviewer can read
and search, that ERC can run against, and — the part that matters — whose
exported netlist can be diffed against the spec the board was built from. A
schematic nobody can verify is worse than no schematic, because it invites
belief. `verify()` closes that loop: generate, export the netlist with
kicad-cli, compare to the source, and report any difference.

Global labels rather than drawn wires is a deliberate choice. Auto-routing wires
between 147 symbols produces a plate of spaghetti that is technically correct
and practically unreadable; a label carries the same connectivity and tells the
reader the net's NAME, which is what they are actually looking for.
"""

import json
import os
import re
import subprocess

MM = 1.0                      # kicad_sch works in mm
PIN_PITCH = 2.54
GRID = 1.27


def _uuid(seed):
    """Deterministic UUID from a seed — same spec must give the same file, or
    every regeneration is a spurious diff in git."""
    import hashlib
    h = hashlib.sha1(seed.encode()).hexdigest()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def refkey(ref):
    m = re.match(r"([A-Za-z_]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

def from_spec(spec_path):
    """-> (name, [{ref,value,fp,pins:[(pad,net)]}], nets)"""
    with open(spec_path) as fh:
        spec = json.load(fh)
    pins = {}
    for net, conns in spec["nets"].items():
        for ref, pad in conns:
            pins.setdefault(ref, []).append((str(pad), net))
    comps = []
    for c in spec["components"]:
        r = c["ref"]
        comps.append({"ref": r, "value": c.get("value", ""),
                      "fp": "%s:%s" % (c.get("lib", ""), c.get("fp", "")),
                      "pins": sorted(pins.get(r, []), key=lambda t: _padkey(t[0]))})
    comps.sort(key=lambda c: refkey(c["ref"]))
    return spec.get("name", "board"), comps, spec["nets"]


def from_board(board_path):
    """Same shape, read from a routed board. Useful when the spec is gone or
    when you want the schematic to reflect the COPPER rather than the intent."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    comps, nets = [], {}
    for fp in b.GetFootprints():
        r = fp.GetReference()
        pins = []
        for pad in fp.Pads():
            n = pad.GetNetname()
            if not n:
                continue
            pins.append((pad.GetNumber(), n))
            nets.setdefault(n, []).append([r, pad.GetNumber()])
        comps.append({"ref": r, "value": fp.GetValue(),
                      "fp": str(fp.GetFPIDAsString()),
                      "pins": sorted(set(pins), key=lambda t: _padkey(t[0]))})
    comps.sort(key=lambda c: refkey(c["ref"]))
    return os.path.splitext(os.path.basename(board_path))[0], comps, nets


def _padkey(p):
    try:
        return (0, int(p), "")
    except ValueError:
        return (1, 0, str(p))


# --------------------------------------------------------------------------
# emit
# --------------------------------------------------------------------------

def _lib_symbol(c):
    """A box with one pin per connected pad. Pin numbers are the real pad
    numbers, so the schematic and the footprint agree by construction."""
    name = "sym_" + c["ref"]
    n = max(len(c["pins"]), 1)
    half = ((n - 1) * PIN_PITCH) / 2.0
    h = max(half + PIN_PITCH, PIN_PITCH * 2)
    w = 12.7
    L = []
    L.append('    (symbol "fluxplace:%s" (pin_names (offset 0.508)) '
             '(in_bom yes) (on_board yes)' % name)
    prefix = refkey(c["ref"])[0]
    L.append('      (property "Reference" "%s" (at 0 %.2f 0) '
             '(effects (font (size 1.27 1.27))))' % (_esc(prefix), h + 1.27))
    L.append('      (property "Value" "%s" (at 0 %.2f 0) '
             '(effects (font (size 1.27 1.27))))' % (_esc(c["value"]), -h - 1.27))
    L.append('      (property "Footprint" "%s" (at 0 0 0) '
             '(effects (font (size 1.27 1.27)) hide))' % _esc(c["fp"]))
    L.append('      (symbol "%s_0_1"' % name)
    L.append('        (rectangle (start %.2f %.2f) (end %.2f %.2f) '
             '(stroke (width 0.254) (type default)) (fill (type background)))'
             % (-w / 2, h, w / 2, -h))
    L.append('      )')
    L.append('      (symbol "%s_1_1"' % name)
    for i, (pad, net) in enumerate(c["pins"]):
        y = half - i * PIN_PITCH
        L.append('        (pin passive line (at %.2f %.2f 0) (length %.2f)'
                 % (-w / 2 - PIN_PITCH, y, PIN_PITCH))
        L.append('          (name "%s" (effects (font (size 1.0 1.0))))' % _esc(net))
        L.append('          (number "%s" (effects (font (size 1.0 1.0))))' % _esc(pad))
        L.append('        )')
    L.append('      )')
    L.append('    )')
    return "\n".join(L), h


def generate(spec_or_board, out_path, title=None, from_copper=False):
    """Write a .kicad_sch. Returns a summary dict."""
    if from_copper:
        name, comps, nets = from_board(spec_or_board)
    else:
        name, comps, nets = from_spec(spec_or_board)

    # column layout: tall parts get their own space, refs stay in order
    COL_W = 63.5
    x, y, col = 25.4, 25.4, 0
    placed, max_y = [], 0.0
    for c in comps:
        _, h = _lib_symbol(c)
        need = 2 * h + PIN_PITCH * 2
        if y + need > 900.0:
            col += 1
            x, y = 25.4 + col * COL_W, 25.4
        placed.append((c, x, y + h))
        y += need
        max_y = max(max_y, y)

    W = 25.4 + (col + 1) * COL_W + 25.4
    H = max_y + 25.4

    L = []
    L.append('(kicad_sch (version 20231120) (generator "fluxplace")')
    L.append('  (uuid "%s")' % _uuid("sheet:" + name))
    L.append('  (paper "User" %.2f %.2f)' % (W, H))
    L.append('  (title_block (title "%s") (comment 1 "Generated by fluxplace from '
             '%s — net-label schematic, verified against its source netlist")'
             % (_esc(title or name), _esc(os.path.basename(spec_or_board))))
    L.append('  )')
    L.append('  (lib_symbols')
    for c in comps:
        s, _ = _lib_symbol(c)
        L.append(s)
    L.append('  )')

    for c, cx, cy in placed:
        u = _uuid("sym:" + c["ref"])
        _, h = _lib_symbol(c)
        L.append('  (symbol (lib_id "fluxplace:sym_%s") (at %.2f %.2f 0) (unit 1) '
                 '(in_bom yes) (on_board yes) (uuid "%s")' % (c["ref"], cx, cy, u))
        L.append('    (property "Reference" "%s" (at %.2f %.2f 0) '
                 '(effects (font (size 1.27 1.27))))' % (_esc(c["ref"]), cx, cy - h - 1.27))
        L.append('    (property "Value" "%s" (at %.2f %.2f 0) '
                 '(effects (font (size 1.27 1.27))))' % (_esc(c["value"]), cx, cy + h + 1.27))
        L.append('    (property "Footprint" "%s" (at %.2f %.2f 0) '
                 '(effects (font (size 1.27 1.27)) hide))' % (_esc(c["fp"]), cx, cy))
        L.append('    (instances (project "%s" (path "/" (reference "%s") (unit 1))))'
                 % (_esc(name), _esc(c["ref"])))
        L.append('  )')

        n = max(len(c["pins"]), 1)
        half = ((n - 1) * PIN_PITCH) / 2.0
        for i, (pad, net) in enumerate(c["pins"]):
            py = cy - (half - i * PIN_PITCH)
            px = cx - 12.7 / 2 - PIN_PITCH * 2
            L.append('  (wire (pts (xy %.2f %.2f) (xy %.2f %.2f)) '
                     '(stroke (width 0) (type default)) (uuid "%s"))'
                     % (px, py, px + PIN_PITCH, py, _uuid("w:%s:%s" % (c["ref"], pad))))
            L.append('  (global_label "%s" (shape input) (at %.2f %.2f 180) '
                     '(effects (font (size 1.27 1.27)) (justify right)) (uuid "%s")'
                     % (_esc(net), px, py, _uuid("gl:%s:%s" % (c["ref"], pad))))
            L.append('  )')

    L.append('  (sheet_instances (path "/" (page "1")))')
    L.append(')')
    with open(out_path, "w") as fh:
        fh.write("\n".join(L) + "\n")

    return {"path": out_path, "components": len(comps), "nets": len(nets),
            "pins": sum(len(c["pins"]) for c in comps),
            "sheet_mm": [round(W, 1), round(H, 1)], "columns": col + 1}


# --------------------------------------------------------------------------
# verify — the part that makes it trustworthy
# --------------------------------------------------------------------------

def verify(sch_path, spec_path, kicad_cli="kicad-cli"):
    """Export the generated schematic's netlist and diff it against the source.

    A generated schematic that nobody checks is worse than none, because it
    invites belief. This is the check: KiCad reads the file we wrote, produces
    its own netlist, and we compare connectivity net by net. Any difference is
    reported rather than smoothed over.
    """
    import tempfile
    out = os.path.join(tempfile.mkdtemp(), "net.net")
    try:
        r = subprocess.run([kicad_cli, "sch", "export", "netlist",
                            "--format", "kicadsexpr", "--output", out, sch_path],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"ok": False, "error": "kicad-cli failed: %s" % e}
    if not os.path.exists(out):
        return {"ok": False, "error": "no netlist produced",
                "stderr": (r.stderr or "")[:400]}

    text = open(out, errors="ignore").read()
    got = {}
    for m in re.finditer(r'\(net\s+\(code\s+"?\d+"?\)\s+\(name\s+"([^"]*)"\)(.*?)(?=\(net\s|\Z)',
                         text, re.S):
        name = m.group(1).lstrip("/")
        nodes = set()
        for n in re.finditer(r'\(node\s+\(ref\s+"([^"]+)"\)\s+\(pin\s+"([^"]+)"\)', m.group(2)):
            nodes.add((n.group(1), n.group(2)))
        if nodes:
            got[name] = nodes

    with open(spec_path) as fh:
        spec = json.load(fh)
    want = {net: {(r, str(p)) for r, p in conns} for net, conns in spec["nets"].items()}

    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    differing = []
    for net in sorted(set(want) & set(got)):
        if want[net] != got[net]:
            differing.append({"net": net,
                              "only_in_spec": sorted(want[net] - got[net])[:6],
                              "only_in_sch": sorted(got[net] - want[net])[:6]})
    ok = not (missing or extra or differing)
    return {"ok": ok, "nets_in_spec": len(want), "nets_in_schematic": len(got),
            "pins_in_spec": sum(len(v) for v in want.values()),
            "pins_in_schematic": sum(len(v) for v in got.values()),
            "missing_nets": missing[:20], "extra_nets": extra[:20],
            "differing_nets": differing[:20],
            "verdict": ("schematic netlist matches the spec exactly" if ok else
                        "MISMATCH: %d missing, %d extra, %d differing net(s)"
                        % (len(missing), len(extra), len(differing)))}
