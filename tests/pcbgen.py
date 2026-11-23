"""Synthetic .kicad_pcb builder for the placement-quality tests (pure text)."""
HEAD = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
        '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (31 "B.Cu" signal))\n')


def pad(num, dx, dy, net="", w=0.5, h=0.5, typ="smd", ang=0.0):
    n = f' (net "{net}")' if net else ""
    return (f'(pad "{num}" {typ} rect (at {dx} {dy} {ang}) (size {w} {h}) '
            f'(layers "F.Cu"){n})')


def fp(ref, x, y, pads, angle=0.0, name="X", side="F", locked=False, court=None):
    lk = " (locked yes)" if locked else ""
    cy = ""
    if court:
        x0, y0, x1, y1 = court
        cy = (f'(fp_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.05) (type solid))'
              f' (layer "{side}.CrtYd"))')
    # pad angle in the file is absolute: add the footprint rotation
    pads = [p.replace(" 0.0) (size", f" {angle}) (size") if angle else p for p in pads]
    if side == "B":                                   # a bottom footprint has its copper on B.Cu
        pads = [p.replace('(layers "F.Cu")', '(layers "B.Cu")') for p in pads]
    return (f'(footprint "{name}" (layer "{side}.Cu"){lk} (at {x} {y} {angle}) '
            f'(property "Reference" "{ref}") {cy} {" ".join(pads)})\n')


def ic(ref, x, y, nets, pitch=0.5, **kw):
    """Dual-row IC: nets = list of net names, pads split left/right."""
    n = len(nets)
    half = (n + 1) // 2
    pads = []
    for i, net in enumerate(nets):
        side = -1 if i < half else 1
        k = i if i < half else n - 1 - i
        pads.append(pad(str(i + 1), side * 2.0, (k - (half - 1) / 2) * pitch, net, 0.8, min(0.3, pitch * 0.6)))
    return fp(ref, x, y, pads, **kw)


def two(ref, x, y, n1, n2, **kw):
    return fp(ref, x, y, [pad("1", -0.5, 0, n1, 0.5, 0.5), pad("2", 0.5, 0, n2, 0.5, 0.5)], **kw)


def board(tmp_path, body, name="b.kicad_pcb", outline=(0, 0, 100, 80), edge=None):
    x0, y0, x1, y1 = outline
    edge = edge or f'(gr_rect (start {x0} {y0}) (end {x1} {y1}) (layer "Edge.Cuts"))\n'
    p = tmp_path / name
    p.write_text(HEAD + edge + body + ")\n")
    return str(p)


def four_layers(path):
    """The 3-copper-layer HEAD is not a board kicad-cli loads (the skew authority, D71cx): add a full GND plane on In2.Cu. The signal
    layers stay F / In1 / B; apply it to a fixture whose pairs are audited."""
    txt = open(path).read().replace('(31 "B.Cu" signal)', '(6 "In2.Cu" signal) (31 "B.Cu" signal)')
    plane = '(zone (net 1) (net_name "GND") (layer "In2.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 100 0) (xy 100 80) (xy 0 80))))\n'
    open(path, "w").write(txt.rstrip().rstrip(")") + plane + ")\n")
    return path
