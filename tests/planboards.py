"""Synthetic .kicad_pcb text for the plan tests (pure text, no pcbnew): 2 signal layers, 100 x 80 mm outline."""
HEAD = '(kicad_pcb (version 20260206) (generator "pcbnew")\n(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
EDGE = '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))\n'


def pad(num, dx, dy, net="", w=0.5, h=0.5, typ="smd"):
    n = f' (net "{net}")' if net else ""
    return f'(pad "{num}" {typ} rect (at {dx} {dy}) (size {w} {h}) (layers "F.Cu"){n})'


def fp(ref, x, y, pads, angle=0.0, court=None):
    cy = ""
    if court:
        x0, y0, x1, y1 = court
        cy = f'(fp_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
    return (f'(footprint "X" (layer "F.Cu") (at {x} {y} {angle}) (property "Reference" "{ref}") {cy} '
            f'{" ".join(pads)})\n')


def keepout(x0, y0, x1, y1, name="wall"):
    return (f'(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (name "{name}") (hatch edge 0.5) '
            f'(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed)) '
            f'(polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))\n')


def write(tmp_path, body, name="b.kicad_pcb"):
    p = tmp_path / name
    p.write_text(HEAD + EDGE + body + ")\n")
    return str(p)


def column_ic(ref, x, y, nets, pitch=0.5, east=None):
    """Dual-row fine-pitch part: a west column (pads 0.8 x 0.3 at x-1.5, nets) and an east column (x+1.5, `east` nets or
    E<i>) plus a GND exposed pad between them; each column escapes outward."""
    n = len(nets)
    east = east or [f"E{ref}{i}" for i in range(n)]
    pads = [pad(str(i + 1), -1.5, (i - (n - 1) / 2) * pitch, net, 0.8, 0.3) for i, net in enumerate(nets)]
    pads += [pad(str(n + i + 1), 1.5, (i - (n - 1) / 2) * pitch, net, 0.8, 0.3) for i, net in enumerate(east)]
    pads.append(pad("EP", 0.0, 0.0, "GND", 1.4, n * pitch - 0.2))       # exposed pad: closes the inward lanes
    return fp(ref, x, y, pads, court=(-2.2, -(n * pitch) / 2 - 0.5, 2.2, (n * pitch) / 2 + 0.5))


def wall(ref, x, y, n, net_prefix="W", pitch=1.0, w=0.4, h=0.8):
    """A column of foreign pads (one net each) forming a wall."""
    pads = [pad(str(i + 1), 0.0, (i - (n - 1) / 2) * pitch, f"{net_prefix}{i}", w, h) for i in range(n)]
    return fp(ref, x, y, pads)
