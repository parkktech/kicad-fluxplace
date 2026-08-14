#!/usr/bin/env python3
"""PCBWay order worksheet — every field on the quote form, pre-filled from the board.

The person who uploads the zip is not the person who drew the board. PCBWay's
"Get Assembly Quote" page asks ~40 questions, and roughly half of them have
exactly one correct answer that is already sitting in the .kicad_pcb (size,
layers, thickness, min track/space, min hole, which sides get populated, how
many unique/SMD/THT/fine-pitch parts). Left to a human, those get re-measured
by eye at 11pm and typed in wrong — a 6/6 mil selection on a 3.5 mil board is
a re-quote at best and a scrapped lot at worst.

So: read the design, emit a worksheet that mirrors the form field for field, in
page order, with three columns —

    Field | Options on the page | From this design | Your choice

— where "From this design" is derived and cited, and anything the design cannot
answer is marked CHOOSE rather than guessed. It renders to Word via fabdoc so
the buyer can tick boxes on a phone next to the browser.

Used by `fluxplace deliver` (automatic, alongside the fab brief) and standalone:
    python3 -m fluxplace.pcbway --board B.kicad_pcb --fab-dir fab/ --out-dir dl/

The option lists below are transcribed from the live PCBWay form. If PCBWay
changes the page, edit them HERE, once — every generated worksheet follows.
"""
import csv
import datetime
import json
import os
import re

MIL = 0.0254                      # mm
FINE_PITCH_MM = 0.65              # <= this is "BGA/QFP" territory to an assembler
CHOOSE = "CHOOSE"                 # the design has no opinion — a human must decide

# ------------------------------------------------------------------ the form
# Assembly Service (top half of the Assembly tab)
OPT_SERVICE = ("Turnkey (PCBWay supply parts)",
               "Kitted or Consigned (customer supply parts)",
               "Combo (you supply some parts, PCBWay does the rest)")
OPT_ASM_BOARD_TYPE = ("Single pieces", "Panelized PCBs")
OPT_SIDES = ("Top side", "Bottom side", "Both sides")
OPT_YESNO = ("No", "Yes")
OPT_PACKAGE_BOX = ("Neutral box", "Custom box")
# PCB Specifications (lower half of the same page)
OPT_PCB_BOARD_TYPE = ("Single pieces", "Panel by Customer", "Panel by Supplier")
OPT_LAYERS = ("1", "2", "4", "6", "8", "10", "12", "14")
OPT_MATERIAL = ("FR-4", "Aluminum", "Rogers", "HDI (buried/blind vias)",
                "Copper Base")
OPT_TG = ("TG 130-140", "TG 150-160", "TG 170-180", "S1000H TG150",
          "S1000-2M TG170")
OPT_THICKNESS = ("0.2", "0.3", "0.4", "0.6", "0.8", "1.0", "1.2", "1.6", "2.0",
                 "2.4", "2.6", "2.8", "3.0", "3.2")
# (track, space) in mil — ordered finest first; we pick the first tier that COVERS
# the design, i.e. whose limit is <= what the board actually contains.
OPT_TRACK_SPACE = ("3/3mil", "4/4mil", "5/5mil", "6/6mil", "8/8mil")
OPT_HOLE = ("0.15mm", "0.2mm", "0.25mm", "0.3mm", "0.8mm", "1.0mm", "No Drill")
OPT_MASK = ("Green", "Red", "Yellow", "Blue", "White", "Black", "Purple",
            "Matte black", "Matte green", "None")
OPT_SILK = ("White", "Black", "Yellow", "None")
OPT_UV = ("None", "Single-sided: Top", "Single-sided: Bottom", "Double-sided")
OPT_FINISH = ("HASL with lead", "HASL lead free", "Immersion gold (ENIG)",
              "OSP", "Hard gold", "Immersion silver (Ag)",
              "HASL lead free + Selective immersion gold",
              "HASL lead free + Selective Hard gold", "Immersion tin",
              "Immersion gold + Selective Hard gold", "ENEPIG",
              "None/Plain copper")
OPT_GOLD_THICKNESS = ("1U", "2U", "3U")
OPT_VIA = ("Tenting vias", "Plugged vias with solder mask", "Vias not covered")
OPT_COPPER = ("Bare board (0 oz)", "1 oz", "2 oz", "3 oz", "4 oz", "5 oz",
              "6 oz", "7 oz", "8 oz", "9 oz", "10 oz", "11 oz", "12 oz",
              "13 oz")
OPT_REMOVE_NO = ("No", "Yes (extra $1.5)", "Specify a location")


def _opts(seq):
    return " / ".join(seq)


# ------------------------------------------------------------------- parsing
# Deliberately pcbnew-free: `deliver` runs under whatever python has python-docx,
# which is usually NOT KiCad's. Everything here is text off the .kicad_pcb.
def _floats(pat, text):
    return [float(x) for x in re.findall(pat, text)]


def _blocks(text, tag):
    """Yield every balanced `(tag ...)` s-expression in `text`.

    Non-greedy regex is NOT good enough here: a gr_line contains a nested
    (stroke ...) block, so `\\(gr_line(.*?)\\)` stops at the stroke's closing
    paren and the `(layer "Edge.Cuts")` line — the thing being tested for —
    never appears in the match. That silently produced a board with no outline
    and a blank Size field on the worksheet.
    """
    out = []
    for m in re.finditer(r"\(%s[\s(]" % re.escape(tag), text):
        depth, in_str, j = 0, False, m.start()
        while j < len(text):
            c = text[j]
            if in_str:
                if c == '"' and text[j - 1] != "\\":
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    out.append(text[m.start():j + 1])
                    break
            j += 1
    return out


def _refkey(ref):
    """C7 before C12 before J3 — refdes sort a human recognises."""
    m = re.match(r"([A-Za-z_]*)(\d*)", ref or "")
    return (m.group(1), int(m.group(2) or 0), ref)


def _footprints(text):
    """-> [{ref, lib, side, pads, tht_pads, pitch}] for every footprint on the board.

    `tht` follows the same rule kicad_io uses (>=5 drilled pads, or most pads
    drilled) so "through-hole part" means one thing across the toolchain: a
    USB-C receptacle with 4 hold-down pegs is an SMT part with hand-soldered
    anchors, not a THT part, and PCBWay prices it as such.
    """
    out = []
    for f in _blocks(text, "footprint"):
        ref = re.search(r'\(property "Reference" "([^"]+)"', f)
        lib = re.search(r'\(footprint "([^"]+)"', f)
        pads = []
        for m in re.finditer(r'\(pad\s+"([^"]*)"\s+(\w+)\s+\w+', f):
            # np_thru_hole is a mechanical peg, not a joint. Counting it as a pad
            # halves the drilled ratio and lies about the part: a Micro-Fit with
            # 2 pins and 2 board locks read as 50% drilled -> "SMD part".
            if m.group(2) == "np_thru_hole":
                continue
            at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)",
                           f[m.end():m.end() + 300])
            pads.append((m.group(2), float(at.group(1)) if at else 0.0,
                         float(at.group(2)) if at else 0.0))
        drilled = sum(1 for p in pads if p[0] == "thru_hole")
        pitch = 999.0
        for i, (_, x1, y1) in enumerate(pads):
            for _, x2, y2 in pads[i + 1:]:
                d = abs(x1 - x2) + abs(y1 - y2)   # rotation-invariant, so the
                if 0.01 < d < pitch:              # footprint frame is fine
                    pitch = d
        attr = re.search(r"\(attr ([^)]*)\)", f)
        out.append(dict(
            ref=(ref.group(1) if ref else "?"),
            lib=(lib.group(1) if lib else ""),
            side=("B" if re.search(r'\(layer "B\.Cu"\)', f) else "F"),
            pads=len(pads), tht_pads=drilled, pitch=pitch,
            tht=bool(pads) and (drilled >= 5 or drilled / len(pads) > 0.5),
            # a DNP land is absent from the centroid file ON PURPOSE — it must not
            # be counted as a part, nor flagged as one the assembler forgot
            dnp=bool(attr and "dnp" in attr.group(1).split()),
        ))
    return out


def board_facts(path):
    """Geometry + design floors, straight off the .kicad_pcb (+ .kicad_pro rules)."""
    text = open(path, encoding="utf-8").read()
    f = {"board": os.path.basename(path)}

    cu = set(re.findall(r'"((?:F|B|In\d+)\.Cu)"', text))
    f["layers"] = len(cu) or None

    th = re.search(r"\(thickness\s+([\d.]+)\)", text)
    f["thickness_mm"] = float(th.group(1)) if th else None

    # Outline: bbox of every Edge.Cuts vertex PLUS the outline stroke, because the
    # board is cut on the outside of the line — this is the dimension KiCad reports
    # and the one the fab quotes. Rounded corners are arcs whose endpoints sit on
    # the straight edges, so vertices alone bound the shape correctly.
    xs, ys, strokes = [], [], []
    for tag in ("gr_line", "gr_arc", "gr_rect", "gr_poly", "gr_circle"):
        for body in _blocks(text, tag):
            if '"Edge.Cuts"' not in body:
                continue
            for x, y in re.findall(r"\((?:start|end|mid|center|xy|at)\s+"
                                   r"(-?[\d.]+)\s+(-?[\d.]+)\)", body):
                xs.append(float(x)); ys.append(float(y))
            strokes += _floats(r"\(width\s+([\d.]+)\)", body)
    if xs:
        edge = max(strokes) if strokes else 0.0
        f["size_mm"] = (round(max(xs) - min(xs) + edge, 1),
                        round(max(ys) - min(ys) + edge, 1))
    else:
        f["size_mm"] = None

    widths = [float(re.search(r"\(width\s+([\d.]+)\)", s).group(1))
              for s in _blocks(text, "segment")
              if re.search(r"\(width\s+([\d.]+)\)", s)]
    f["min_track_mm"] = min(widths) if widths else None

    vias = _blocks(text, "via")
    vd = [float(re.search(r"\(drill\s+([\d.]+)\)", v).group(1)) for v in vias
          if re.search(r"\(drill\s+([\d.]+)\)", v)]
    f["vias"] = len(vias)
    drills = _floats(r"\(drill\s+([\d.]+)\)", text)
    f["min_via_drill_mm"] = min(vd) if vd else None
    f["min_hole_mm"] = min(drills) if drills else None

    # clearance can't be measured from geometry cheaply — take the project's own
    # floor, which is what DRC actually enforced
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    f["min_clearance_mm"] = None
    if os.path.exists(pro):
        try:
            rules = json.load(open(pro))["board"]["design_settings"]["rules"]
            f["min_clearance_mm"] = rules.get("min_clearance")
            if f["min_track_mm"] is None:
                f["min_track_mm"] = rules.get("min_track_width")
        except Exception:
            pass

    fps = _footprints(text)
    f["footprints"] = fps
    f["dnp_refs"] = sorted((p["ref"] for p in fps if p["dnp"]), key=_refkey)
    placeable = [p for p in fps if p["pads"] and not p["dnp"]]
    f["tht_refs"] = sorted((p["ref"] for p in placeable if p["tht"]), key=_refkey)
    fine = [p for p in placeable
            if p["pitch"] <= FINE_PITCH_MM and p["pads"] >= 8]
    f["fine_refs"] = sorted((p["ref"] for p in fine), key=_refkey)
    # the pitch that sets the stencil and the paste process — over the fine-pitch
    # parts only, since the tightest pad gap on some passive says nothing useful
    f["finest_pitch_mm"] = min([p["pitch"] for p in fine], default=None)
    # bottom-terminated packages: the joints nobody can inspect optically, which
    # is the whole argument for paying for X-ray on a first article
    hidden = re.compile(r"(BGA|LGA|QFN|DFN|[UW]?SON|CSP)", re.I)
    f["hidden_joint_refs"] = sorted((p["ref"] for p in placeable
                                     if hidden.search(p["lib"])), key=_refkey)
    f["mixed_refs"] = sorted((p["ref"] for p in placeable
                              if p["tht_pads"] and not p["tht"]), key=_refkey)
    return f


def place_facts(fab_dir):
    """Side split from the pick-and-place file — the assembler's own part list."""
    pos = os.path.join(fab_dir, "place", "pos.csv")
    if not os.path.exists(pos):
        return {}
    rows = list(csv.DictReader(open(pos, encoding="utf-8")))
    key = "Side" if rows and "Side" in rows[0] else "side"
    top = sum(1 for r in rows if (r.get(key) or "").lower().startswith("t"))
    bot = sum(1 for r in rows if (r.get(key) or "").lower().startswith("b"))
    return {"placements": len(rows), "top": top, "bottom": bot,
            "pos_refs": [r.get("Ref") or r.get("ref") for r in rows]}


def bom_facts(paths, board_refs=None):
    """Unique-part count = BOM lines carrying an MPN (what PCBWay calls 'kinds').

    Only counts files that are THIS BOARD's BOM. `deliver` is also handed the
    harness/panel BOM — cable ends, panel connectors, things the assembler never
    sees — and counting those inflates every number the quote is priced from.
    The test is structural: a board BOM has a Refs column naming footprints that
    exist on the board.
    """
    lines, mpns, used = 0, [], []
    for p in paths or ():
        if not p or not os.path.exists(p):
            continue
        try:
            rows = list(csv.DictReader(open(p, encoding="utf-8")))
        except Exception:
            continue
        if not rows:
            continue
        col = next((c for c in ("Refs", "refs", "Reference", "Designator")
                    if c in rows[0]), None)
        if not col:
            continue
        if board_refs is not None and not any(
                set((r.get(col) or "").split()) & board_refs for r in rows):
            continue
        used.append(os.path.basename(p))
        for r in rows:
            if (r.get("MPN") or r.get("mpn") or "").strip():
                lines += 1
                mpns.append(r["MPN"].strip() if r.get("MPN") else r["mpn"].strip())
    return {"unique_parts": lines or None, "mpns": mpns, "bom_files": used}


def sourcing_facts(path):
    """Parts PCBWay should NOT be asked to buy, from a `fluxplace sourcing --json`
    report: anything not comfortably in distributor stock is a part we consign or
    a part their sourcing desk silently substitutes."""
    if not path or not os.path.exists(path):
        return {}
    try:
        rep = json.load(open(path))
    except Exception:
        return {}
    consign = []
    for mpn, r in sorted(rep.items()):
        v = r.get("verdict")
        if v in ("LEAD", "RISK", "NONE", "LOW"):
            consign.append((mpn, v, " ".join(r.get("refs", []))))
    return {"consign": consign}


# ------------------------------------------------------------- derived picks
def track_space_tier(track_mm, clearance_mm):
    """Smallest form option that COVERS the design. Rounding this the wrong way is
    the expensive mistake: picking 6/6mil on a 3.5mil board gets the job quoted
    cheap, then re-quoted or fabricated out of spec."""
    have = [v for v in (track_mm, clearance_mm) if v]
    if not have:
        return CHOOSE, ""
    tight = min(have)
    for opt in reversed(OPT_TRACK_SPACE):          # coarsest first
        limit = float(opt.split("/")[0]) * MIL
        if limit <= tight + 1e-9:
            pick = opt
            break
    else:
        pick = OPT_TRACK_SPACE[0]
    note = "design has %.3f mm track / %s mm space (%.2f / %s mil)" % (
        track_mm or 0, ("%.3f" % clearance_mm) if clearance_mm else "?",
        (track_mm or 0) / MIL, ("%.2f" % (clearance_mm / MIL)) if clearance_mm else "?")
    if tight < float(pick.split("/")[0]) * MIL:
        note += " — BELOW the finest tier on the form; confirm with PCBWay"
    return pick, note


def hole_tier(hole_mm):
    """The form declares the SMALLEST drill in the design, so take the largest
    option that still sits at or under what the board actually contains."""
    if not hole_mm:
        return CHOOSE, ""
    cover = [o for o in OPT_HOLE[:-1]
             if float(o.replace("mm", "")) <= hole_mm + 1e-9]
    pick = cover[-1] if cover else OPT_HOLE[0]
    note = "smallest drill in the design is %.2f mm" % hole_mm
    if not cover:
        note += " — under the finest option on the form; confirm with PCBWay"
    return pick, note


def thickness_pick(mm):
    """Exact match only. "%.1f" would round a 1.55 mm stackup onto the 1.6 mm
    button and nobody would ever know the board changed."""
    if not mm:
        return CHOOSE, ""
    hit = [o for o in OPT_THICKNESS if abs(float(o) - mm) < 1e-6]
    return ((hit[0], "board file says %.2f mm" % mm) if hit else
            (CHOOSE, "board file says %.3f mm — not a button on the form, ask "
                     "PCBWay for a custom thickness" % mm))


def collect(board=None, fab_dir=None, boms=(), sourcing=None, quantity=None,
            name=None):
    """Everything the worksheet knows, from whatever inputs exist."""
    name = name or (os.path.splitext(os.path.basename(board))[0]
                    if board else "board")
    f = {"name": re.sub(r"-fab$", "", name),
         "date": datetime.date.today().isoformat(),
         "quantity": quantity}
    if board and os.path.exists(board):
        f.update(board_facts(board))
    if fab_dir:
        f.update(place_facts(fab_dir))
    f.update(bom_facts(boms, board_refs={p["ref"] for p in f.get("footprints", [])}
                       or None))
    f.update(sourcing_facts(sourcing))

    # placements: prefer the pick-and-place file (mounting holes and DNP parts are
    # already excluded there); fall back to footprints with pads
    fps = f.get("footprints") or []
    if "placements" not in f and fps:
        placeable = [p for p in fps if p["pads"]]
        f["placements"] = len(placeable)
        f["top"] = sum(1 for p in placeable if p["side"] == "F")
        f["bottom"] = sum(1 for p in placeable if p["side"] == "B")
    if fps and f.get("pos_refs"):
        # Count over the parts the assembler is ACTUALLY handed. Mounting holes
        # and fiducials are correctly absent from the pick-and-place file — but
        # anything ELSE the board has and pos.csv doesn't is a part that gets
        # soldered by nobody unless the order notes say so (KiCad drops a
        # footprint from pos export on an "exclude from position files"
        # attribute, and a hand-fit THT antenna is the classic victim).
        mech = re.compile(r"MountingHole|Fiducial|TestPoint", re.I)
        placed = set(f["pos_refs"])
        missing = [p for p in fps if p["pads"] and p["ref"] not in placed
                   and not p["dnp"] and not mech.search(p["lib"])]
        f["unplaced_refs"] = sorted((p["ref"] for p in missing), key=_refkey)
        if missing:
            f["placements"] = f.get("placements", 0) + len(missing)
            f["top"] = f.get("top", 0) + sum(1 for p in missing if p["side"] == "F")
            f["bottom"] = f.get("bottom", 0) + sum(1 for p in missing if p["side"] == "B")
            placed |= {p["ref"] for p in missing}
        for k in ("tht_refs", "fine_refs", "hidden_joint_refs", "mixed_refs"):
            f[k] = [r for r in f.get(k, []) if r in placed]
    f["tht_count"] = len(f.get("tht_refs", []))
    if f.get("placements") is not None:
        f["smd_count"] = f["placements"] - f["tht_count"]
    return f


def _sides_pick(f):
    top, bot = f.get("top"), f.get("bottom")
    if top is None or bot is None:
        return CHOOSE, ""
    if top and bot:
        return "Both sides", "%d parts on top, %d on the bottom" % (top, bot)
    return ("Top side" if top else "Bottom side",
            "%d parts, one side only" % (top or bot))


# ----------------------------------------------------------------- worksheet
def worksheet(f, zip_name=None, extra_notes=()):
    """Render the whole form as markdown. fabdoc turns it into the .docx."""
    L = []
    A = L.append
    size = f.get("size_mm")
    size_s = ("%.1f x %.1f mm" % size) if size else CHOOSE
    qty = str(f["quantity"]) if f.get("quantity") else "____"
    sides, sides_why = _sides_pick(f)
    track, track_why = track_space_tier(f.get("min_track_mm"),
                                        f.get("min_clearance_mm"))
    hole, hole_why = hole_tier(f.get("min_hole_mm"))
    thick, thick_why = thickness_pick(f.get("thickness_mm"))
    fine = f.get("fine_refs") or []
    finish = ("Immersion gold (ENIG)" if fine else "HASL lead free")
    finish_why = ("%d fine-pitch parts (<= %.2f mm): %s — HASL's uneven surface "
                  "bridges at this pitch" % (len(fine), FINE_PITCH_MM,
                                             ", ".join(fine))) if fine else \
                 "no fine-pitch parts on the board"
    consign = f.get("consign") or []
    service = (OPT_SERVICE[2] if consign else OPT_SERVICE[0])
    service_why = ("%d part(s) are not safely buyable by a third party — see §5"
                   % len(consign)) if consign else \
                  "no sourcing flags recorded; confirm against the BOM"
    panel_hint = ""
    if size and min(size) < 50:
        panel_hint = " (a side is under 50 mm — PCBWay will suggest panelising)"
    elif f.get("quantity") and f["quantity"] > 20:
        panel_hint = " (over 20 pcs — PCBWay will suggest panelising)"

    A("# PCBWay order worksheet — %s" % f["name"])
    A("")
    A("Generated %s from `%s`. Fill the right-hand column, then copy it into the "
      "form. **Start on the Assembly tab** — it carries the PCB specification "
      "section too, so the bare board and the assembly are one order, not two."
      % (f["date"], f.get("board", "the board file")))
    A("")
    A("Values in *From this design* are measured off the board file — they are "
      "not suggestions, and changing one changes the board. Anything marked "
      "%s has no answer in the design: it is a commercial or process decision "
      "for whoever orders." % CHOOSE)
    A("")

    A("## 1. What to upload where")
    A("")
    A("PCBWay's assembly page has **four separate upload fields**. The files in "
      "this folder are numbered to match them — upload each one into the field "
      "with its number, and nothing anywhere else.")
    A("")
    A("| # | Field on the page | Upload this | Accepts |")
    A("|---|---|---|---|")
    A("| 1 | Upload Gerber file | `%s` | .rar .zip .7z, max 50 MB |"
      % (f.get("slot_gerbers") or (zip_name or "the CAM zip")))
    A("| 2 | Parts List (BOM) Upload | `%s` | .rar .zip .7z .xls .xlsx .csv |"
      % (f.get("slot_bom") or "the assembly BOM .csv"))
    A("| 3 | Upload Centroid file | `%s` | .rar .zip .7z .xls .xlsx .csv |"
      % (f.get("slot_centroid") or "the centroid .csv"))
    A("| 4 | Upload assembly other files | `%s` | assembly-related documents |"
      % (f.get("slot_notes") or "the assembly instructions .docx"))
    A("")
    A("The centroid is **not** inside the gerber zip — PCBWay wants it in its "
      "own field, and a pick-and-place buried in the CAM archive is a file "
      "their assembly desk never opens. Anything PCB-fabrication related does "
      "go inside the zip, which is where the DRC report and manifest are.")
    A("")
    A("**Do not upload** this worksheet, the submission brief, or any "
      "off-board/harness BOM. Those are yours.")
    A("")

    A("## 2. Assembly Service")
    A("")
    A("| Field | Options on the page | From this design | Your choice |")
    A("|---|---|---|---|")
    A("| 3 flexible options | %s | **%s** — %s | |" % (_opts(OPT_SERVICE), service, service_why))
    A("| Board type | %s | **Single pieces**%s | |" % (_opts(OPT_ASM_BOARD_TYPE), panel_hint))
    A("| Assembly side(s) | %s | **%s** — %s | |" % (_opts(OPT_SIDES), sides, sides_why))
    A("| Quantity | (pcs, total single boards) | %s | |" % qty)
    A("| Contains sensitive components/parts | %s | **Yes** — %s | |"
      % (_opts(OPT_YESNO), _sensitive_why(f)))
    A("| Do you accept alternatives/substitutes made in China? | %s | **No** — see the warning below | |"
      % _opts(OPT_YESNO))
    A("| Select PCBWay's PCB Order# / PO No. | %s | %s — only if you are pairing this with an existing PCB order | |"
      % (_opts(OPT_YESNO), CHOOSE))
    A("")
    A("**Do not answer Yes to substitutes.** A same-land part is not a same-part: "
      "connectors and magjacks in particular share an industry-standard footprint "
      "across vendors with incompatible pinouts. A wrong-pinout substitute solders "
      "in perfectly, passes every test the assembler runs, and arrives dead. If "
      "they cannot source something, the answer is to ask us, not to swap it.")
    A("")

    A("## 3. Other Parameters (part counts)")
    A("")
    A("The form says these can be left blank. Do not leave them blank — they are "
      "what the price is built from, and they are all known:")
    A("")
    A("| Field | From this design | Your choice |")
    A("|---|---|---|")
    A("| Number of Unique Parts | %s | |" % (f.get("unique_parts") or CHOOSE))
    A("| Number of SMD Parts | %s | |" % (f.get("smd_count")
                                          if f.get("smd_count") is not None else CHOOSE))
    A("| Number of BGA/QFP Parts | %d%s | |"
      % (len(fine), (" — " + ", ".join(fine)) if fine else ""))
    A("| Number of Through-Hole Parts | %d%s | |"
      % (f.get("tht_count", 0),
         (" — " + ", ".join(f.get("tht_refs") or [])) if f.get("tht_refs") else ""))
    A("")
    if f.get("mixed_refs"):
        A("- Mixed-technology parts (SMT body, through-hole anchors, counted as "
          "SMD above but they need hand joints): %s" % ", ".join(f["mixed_refs"]))
    if f.get("dnp_refs"):
        A("- **Do not populate: %s.** These lands are on the board and in the "
          "gerbers on purpose, and they are excluded from `place/pos.csv` on "
          "purpose. Fitting them is a defect, not a favour."
          % ", ".join(f["dnp_refs"]))
    if f.get("unplaced_refs"):
        A("- **On the board but NOT in `place/pos.csv`: %s.** KiCad excludes "
          "these from the centroid file, so an assembler working from that file "
          "alone ships the board without them. Decide per part: fit it by hand "
          "(§5 says so) or order it DNP on purpose — but say which, because "
          "silence gets you the second one." % ", ".join(f["unplaced_refs"]))
    if f.get("placements") is not None:
        A("- Total placements: %d (%s top, %s bottom)."
          % (f["placements"], f.get("top", "?"), f.get("bottom", "?")))
    if f.get("bottom"):
        A("- Double-sided reflow: the bottom side runs first or gets glued — "
          "confirm they are quoting a two-pass process, not one.")
    A("")

    A("## 4. Customized Services and Advanced Options (assembly)")
    A("")
    A("Costs here are NOT in the online quotation — every Yes turns the quote "
      "into a manual one.")
    A("")
    A("| Field | Options | From this design | Your choice |")
    A("|---|---|---|---|")
    A("| Depanel the boards by delivery | %s | No (ordering single pieces) | |" % _opts(OPT_YESNO))
    A("| Conformal coating | %s | %s — a process/environment call, not a design one | |" % (_opts(OPT_YESNO), CHOOSE))
    A("| Press-fit assembly | %s | No — no press-fit parts on the board | |" % _opts(OPT_YESNO))
    A("| Cable wire harness assembly | %s | No — harness parts are ordered separately | |" % _opts(OPT_YESNO))
    A("| Package box | %s | Neutral box | |" % _opts(OPT_PACKAGE_BOX))
    A("| Flying Probe Testing | %s | **Yes** — bare-board e-test on a %s-layer design is worth it | |"
      % (_opts(OPT_YESNO), f.get("layers", "multi")))
    A("| Function test | %s | %s — needs a test procedure and fixture from us | |" % (_opts(OPT_YESNO), CHOOSE))
    A("| Firmware loading | %s | %s — only if you send an image and a procedure | |" % (_opts(OPT_YESNO), CHOOSE))
    A("| Box build assembly | %s | %s | |" % (_opts(OPT_YESNO), CHOOSE))
    A("| Number of X-ray test | (count) | %s | |" % _xray_why(f))
    A("")

    A("## 5. Detailed information of assembly — paste this into the box")
    A("")
    A("> " + _assembly_note(f, extra_notes))
    A("")
    if consign:
        A("Parts to consign (send yourself) rather than let PCBWay buy — a "
          "distributor-thin part is exactly where a silent substitution happens:")
        A("")
        A("| MPN | Sourcing verdict | Used by |")
        A("|---|---|---|")
        for mpn, verdict, refs in consign:
            A("| %s | %s | %s |" % (mpn, verdict, refs))
        A("")

    A("## 6. PCB Specifications")
    A("")
    A("| Field | Options on the page | From this design | Your choice |")
    A("|---|---|---|---|")
    A("| Board type | %s | Single pieces | |" % _opts(OPT_PCB_BOARD_TYPE))
    A("| Different design in panel | 1 / 2 / 3 / 4 / 5 / 6 / custom | 1 | |")
    A("| Size (single) | Length x Width, mm | **%s** | |" % size_s)
    A("| Quantity (single) | (pcs) | %s | |" % qty)
    A("| Layers | %s | **%s** | |" % (_opts(OPT_LAYERS), f.get("layers") or CHOOSE))
    A("| Material | %s | FR-4 | |" % _opts(OPT_MATERIAL))
    A("| FR4-TG | %s | TG 150-160 | |" % _opts(OPT_TG))
    A("| Thickness | %s mm | **%s** — %s | |" % (" / ".join(OPT_THICKNESS), thick, thick_why))
    A("| Min track/spacing | %s | **%s** — %s | |" % (_opts(OPT_TRACK_SPACE), track, track_why))
    A("| Min hole size | %s | **%s** — %s | |" % (_opts(OPT_HOLE), hole, hole_why))
    A("| Solder mask | %s | Green | |" % _opts(OPT_MASK))
    A("| Silkscreen | %s | White | |" % _opts(OPT_SILK))
    A("| UV printing Multi-color | %s | None | |" % _opts(OPT_UV))
    A("| Edge connector | %s | No | |" % _opts(OPT_YESNO))
    A("| Surface finish | %s | **%s** — %s | |" % (_opts(OPT_FINISH), finish, finish_why))
    A("| Thickness of Immersion Gold | %s | 1U | |" % _opts(OPT_GOLD_THICKNESS))
    A("| Via process | %s | Tenting vias — the gerbers already have the vias mask-covered | |" % _opts(OPT_VIA))
    A("| Finished copper | %s | 1 oz outer | |" % _opts(OPT_COPPER))
    A("| Remove product No. | %s | %s — 'Yes' keeps the board clean; otherwise tell them a spot on the back silk | |"
      % (_opts(OPT_REMOVE_NO), CHOOSE))
    A("| Castellated holes / Edge plating / Impedance control | (advanced options) | No / No / %s | |" % _impedance(f))
    A("")
    A("Leave the *\"we may change HASL to ENIG at our discretion\"* tick box "
      "alone — it only ever upgrades the finish, and this board needs ENIG.")
    A("")

    A("## 7. Before you click Calculate")
    A("")
    A("- The online price excludes PCB fabrication of the assembly lot and the "
      "components themselves; the real number arrives after their engineer "
      "reviews the files. Expect the quote to move.")
    A("- If any field above came back **%s**, decide it before submitting — an "
      "unanswered field becomes their default, not ours." % CHOOSE)
    A("- If their review asks to change copper, drills, or a part number, stop "
      "and come back to us rather than approving it in the portal.")
    for n in extra_notes:
        A("- %s" % n)
    A("")
    return "\n".join(L)


def assembly_notes(f, extra=""):
    """The document for PCBWay's FOURTH upload slot ("other assembly files").

    Everything here is an instruction to the assembler, derived from the board:
    what not to fit, what is hand-soldered, what we consign, what must never be
    substituted. It is deliberately separate from the buyer's worksheet — that
    one is for filling in a form, this one travels with the job.
    """
    L = []
    A = L.append
    A("# Assembly instructions — %s" % f["name"])
    A("")
    A("Generated %s from `%s`. This document accompanies the BOM and the "
      "centroid file. If anything here conflicts with a verbal instruction or "
      "an assumption, THIS document wins — please ask us rather than deciding."
      % (f["date"], f.get("board", "the board file")))
    A("")

    A("## 1. Board")
    A("")
    A("| | |")
    A("|---|---|")
    if f.get("size_mm"):
        A("| Size | %.1f x %.1f mm |" % f["size_mm"])
    A("| Layers | %s |" % (f.get("layers") or "?"))
    if f.get("thickness_mm"):
        A("| Thickness | %.1f mm |" % f["thickness_mm"])
    sides, _ = _sides_pick(f)
    A("| Assembly sides | %s |" % sides)
    A("| Placements | %s (%s top / %s bottom) |"
      % (f.get("placements", "?"), f.get("top", "?"), f.get("bottom", "?")))
    A("| Unique parts | %s |" % (f.get("unique_parts") or "see BOM"))
    A("")

    A("## 2. Do not populate")
    A("")
    if f.get("dnp_refs"):
        A("**%s — DO NOT FIT.** The lands exist in the gerbers on purpose and "
          "these parts are deliberately absent from the centroid file. Fitting "
          "them is a defect. They carry a DO-NOT-POPULATE line in the BOM."
          % ", ".join(f["dnp_refs"]))
    else:
        A("Every part on the BOM is populated. Nothing is DNP.")
    A("")

    A("## 3. Process notes")
    A("")
    if f.get("top") and f.get("bottom"):
        A("- **Double-sided reflow.** Both faces carry parts; quote a two-pass "
          "process, not one.")
    if f.get("fine_refs"):
        A("- **Fine pitch: %s.**%s These set the stencil and the paste process."
          % (", ".join(f["fine_refs"]),
             (" Finest pitch on the board is %.2f mm." % f["finest_pitch_mm"])
             if f.get("finest_pitch_mm") else ""))
    if f.get("tht_refs"):
        A("- **Through-hole: %s.** Hand-solder or selective wave; everything "
          "else is SMT reflow. Each one has a position and rotation in the "
          "centroid file." % ", ".join(f["tht_refs"]))
    if f.get("mixed_refs"):
        A("- **Mixed technology: %s.** SMT body with through-hole anchors — "
          "reflow the body, hand-solder the anchors." % ", ".join(f["mixed_refs"]))
    if f.get("hidden_joint_refs"):
        A("- **Bottom-terminated packages: %s.** Joints under the package that "
          "no optical inspection can see — the parts to X-ray on a first "
          "article." % ", ".join(f["hidden_joint_refs"]))
    A("- Rotations follow KiCad conventions; expect the usual CAM rotation "
      "corrections on QFN and SOT parts. Please tell us what you changed.")
    A("")

    A("## 4. Parts we supply (do not buy these)")
    A("")
    if f.get("consign"):
        A("| MPN | Why | Used by |")
        A("|---|---|---|")
        for mpn, verdict, refs in f["consign"]:
            why = {"NONE": "not carried by any distributor",
                   "LEAD": "catalogued but 0 stock / long lead",
                   "RISK": "EOL / NRND",
                   "LOW": "distributor stock too thin to trust"}.get(verdict, verdict)
            A("| %s | %s | %s |" % (mpn, why, refs))
        A("")
        A("These arrive from us. Do not source alternates for them.")
    else:
        A("None — every line on the BOM is a turnkey buy.")
    A("")

    A("## 5. Substitutions — the rule")
    A("")
    A("**Do not substitute any part on land-pattern fit alone.** Several "
      "packages on this board share an industry-standard land with "
      "vendor-specific pinouts: a wrong-pinout part solders in perfectly, "
      "passes every electrical test on your line, and arrives dead. If a part "
      "cannot be sourced, stop and contact us — we will consign it rather than "
      "accept a substitution.")
    A("")
    if extra:
        A(extra.strip())
        A("")
    return "\n".join(L)


def _sensitive_why(f):
    hits = []
    if f.get("fine_refs"):
        hits.append("fine-pitch parts (%s)" % ", ".join(f["fine_refs"][:4]))
    if f.get("hidden_joint_refs"):
        hits.append("bottom-terminated packages")
    return ("moisture/ESD-sensitive: " + "; ".join(hits)) if hits else \
           "assume yes unless the BOM is all passives"


def _xray_why(f):
    hidden = f.get("hidden_joint_refs") or []
    if not hidden:
        return "0 — no hidden-joint packages"
    return ("%d suggested for the first article — %s have joints under the "
            "package that no optical inspection can see; drop to 0 on repeat "
            "builds" % (len(hidden), ", ".join(hidden)))


def _impedance(f):
    return ("%s — required only if the brief says so; ask us before agreeing "
            "to a stackup change" % CHOOSE)


def _assembly_note(f, extra_notes=()):
    bits = ["%s: %s-layer carrier, %s." % (
        f["name"], f.get("layers", "?"),
        ("%.1f x %.1f mm" % f["size_mm"]) if f.get("size_mm") else "see gerbers")]
    if f.get("top") is not None:
        bits.append("Assembly is double-sided (%d top / %d bottom placements)."
                    % (f.get("top", 0), f.get("bottom", 0))
                    if f.get("top") and f.get("bottom") else
                    "Assembly is single-sided.")
    if f.get("fine_refs"):
        bits.append("Fine-pitch parts (%s) set the stencil and the paste "
                    "process%s." % (", ".join(f["fine_refs"]),
                                    (" — finest pitch on the board is %.2f mm"
                                     % f["finest_pitch_mm"])
                                    if f.get("finest_pitch_mm") else ""))
    if f.get("tht_refs"):
        bits.append("Through-hole parts (%s) are hand-solder or selective-wave; "
                    "everything else is SMT reflow." % ", ".join(f["tht_refs"]))
    if f.get("mixed_refs"):
        bits.append("Mixed technology (SMT body, through-hole anchors): %s — "
                    "reflow the body, hand-solder the anchors."
                    % ", ".join(f["mixed_refs"]))
    if f.get("unplaced_refs"):
        bits.append("Absent from place/pos.csv but still to be fitted by hand: "
                    "%s — take the location from the assembly drawing and BOM, "
                    "and do not treat their absence from the centroid file as "
                    "DNP." % ", ".join(f["unplaced_refs"]))
    if f.get("dnp_refs"):
        bits.append("DO NOT POPULATE: %s — the lands exist by design and are "
                    "deliberately left off the centroid file."
                    % ", ".join(f["dnp_refs"]))
    bits.append("Do not substitute any part on land-pattern fit alone: several "
                "packages here share an industry-standard land with vendor-specific "
                "pinouts. If a part cannot be sourced, contact us and we will "
                "consign it.")
    bits += list(extra_notes)
    return " ".join(bits)


# --------------------------------------------------------------------- write
def slot_names(name):
    """PCBWay's four upload fields, as four filenames numbered to match them.

    Numbering is the whole point: the buyer reads the folder in the same order
    as the page, and a file cannot end up in the wrong field by accident.
    """
    return {"slot_gerbers": "1-GERBERS-%s.zip" % name,
            "slot_bom": "2-BOM-%s.csv" % name,
            "slot_centroid": "3-CENTROID-%s.csv" % name,
            "slot_notes": "4-ASSEMBLY-INSTRUCTIONS-%s.docx" % name}


def write(out_dir, f, zip_name=None, extra_notes=(), docx=True, title=None,
          assembly_extra="", log=print):
    """Write the worksheet and the assembly-instructions doc into `out_dir`."""
    os.makedirs(out_dir, exist_ok=True)
    from fluxplace import fabdoc
    made = []

    def _emit(md_path, out_docx, doc_title, doc_sub):
        made.append(md_path)
        if not docx:
            return
        if fabdoc.render(md_path, out_docx, doc_title, doc_sub):
            made.append(out_docx)
        else:
            log("    python-docx unavailable — %s is markdown only"
                % os.path.basename(md_path))

    base = "PCBWay-Order-Worksheet-%s" % f["name"]
    md = os.path.join(out_dir, base + ".md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(worksheet(f, zip_name=zip_name, extra_notes=extra_notes))
    _emit(md, os.path.join(out_dir, base + ".docx"), "PCBWay order worksheet",
          "%s — fill in, then submit" % (title or f["name"]))

    # slot 4: travels WITH the job, unlike the worksheet which stays with the buyer
    nbase = "4-ASSEMBLY-INSTRUCTIONS-%s" % f["name"]
    nmd = os.path.join(out_dir, nbase + ".md")
    with open(nmd, "w", encoding="utf-8") as fh:
        fh.write(assembly_notes(f, extra=assembly_extra))
    _emit(nmd, os.path.join(out_dir, nbase + ".docx"), "Assembly instructions",
          "%s — upload with the assembly order" % (title or f["name"]))

    for p in made:
        log("    pcbway -> %s" % p)
    return made


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--board")
    ap.add_argument("--fab-dir")
    ap.add_argument("--bom", action="append")
    ap.add_argument("--sourcing-json")
    ap.add_argument("--quantity", type=int)
    ap.add_argument("--name")
    ap.add_argument("--zip-name")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--no-docx", action="store_true")
    a = ap.parse_args()
    facts = collect(board=a.board, fab_dir=a.fab_dir, boms=a.bom,
                    sourcing=a.sourcing_json, quantity=a.quantity, name=a.name)
    write(a.out_dir, facts, zip_name=a.zip_name, docx=not a.no_docx)
