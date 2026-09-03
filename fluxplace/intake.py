"""Design INTAKE: interview the user about external interfaces, connectors,
and mounting before any placement work, and write `design_intent.json` that
the rest of the pipeline (and the human) can hold the design against.

Questions per external interface:
  - on-board connector TYPE (latching families first; solder pads an option)
  - EDGE-mounted or REMOTE (harness to a panel/enclosure face)
  - if REMOTE and the user does not specify a panel connector, a sealed
    latching default is chosen by interface kind (vehicle boards: Deutsch
    DT-compatible for power, mini-XLR for audio, SMA bulkhead for RF)

Mounting:
  - holes or not; corners-equal-inset pattern or free placement; size

Everything runs on injected `ask`/`say` callables so it is testable and can
be driven non-interactively from an answers JSON (agent- and CI-friendly).
"""
import json

__all__ = ["ON_BOARD", "PANEL_DEFAULTS", "ENV_LOCATION", "run", "load_intent",
           "ask_environment"]

# choice key -> (label, concrete part guidance)
ON_BOARD = {
    "jst-xa": ("JST XA (2.5mm, POSITIVE LATCH — signal/low power)",
               "B0xB-XASK-1 header + XAP-0xV-1 housing; stdlib Connector_JST"),
    "jst-gh": ("JST GH (1.25mm, latch — fine-pitch signal)",
               "BM0xB-GHS-TBT; stdlib Connector_JST"),
    "microfit": ("Molex Micro-Fit 3.0 (latch — power)",
                 "43650-xx15 vertical / 43045 RA; crimps 43030"),
    "terminal": ("Screw terminal block (field-wireable power)",
                 "Phoenix MKDS / Wuerth 401B; 5.0mm pitch"),
    "pinheader": ("0.1in pin header (bench/debug ONLY — friction fit)",
                  "flagged by lint under vibration policy"),
    "solder-pads": ("Solder pads / castellated edge (wires soldered direct)",
                    "no connector cost; strain-relief the harness"),
    "usb-c": ("USB-C receptacle", "GCT USB4105 class"),
    "sma-edge": ("SMA edge/vertical jack (RF)", "50R launch, pour pullback"),
    "ufl": ("U.FL (RF, board-to-pigtail)", "Hirose U.FL-R-SMT-1"),
    "other": ("Other (describe)", ""),
}

# interface kind -> preferred SEALED PANEL connector when remote+unspecified
PANEL_DEFAULTS = {
    "power": "Amphenol AT04-2P-MM01 flange receptacle (Deutsch DT-compatible"
             ", latch + seal; vehicle-harness standard)",
    "audio": "5-pin mini-XLR panel (TA5/TB5 series — latching)",
    "rf": "SMA bulkhead jack + U.FL pigtail to the board",
    "data": "M12 circular or sealed USB-C bulkhead",
    "signal": "M12 circular (A-coded) or mini-XLR panel",
}

KINDS = ("power", "audio", "rf", "data", "signal")


def _choice(ask, say, prompt, options, default=None):
    keys = list(options)
    for i, k in enumerate(keys, 1):
        label = options[k][0] if isinstance(options[k], tuple) else options[k]
        say(f"  {i}. {label}")
    raw = (ask(f"{prompt} [1-{len(keys)}"
               f"{', default ' + default if default else ''}]: ") or "").strip()
    if not raw and default:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(keys):
        return keys[int(raw) - 1]
    return raw if raw in options else (default or keys[0])


def run(ask=input, say=print, answers=None):
    """Interview -> intent dict. `answers` (dict) short-circuits questions:
    {"interfaces": [...], "mounting": {...}} entries are taken as given."""
    if answers and "interfaces" in answers and "mounting" in answers:
        # taken as given; a missing environment surfaces later as the
        # review gate's ENV_UNDEFINED warning rather than a prompt here
        return dict(answers)

    intent = {"interfaces": [], "mounting": {}}
    say("== External interfaces (connectors) ==")
    while True:
        name = (ask("Interface name (power/comm/radio/... , empty to finish): ")
                or "").strip()
        if not name:
            break
        kind = _choice(ask, say, f"Kind of '{name}'",
                       {k: k for k in KINDS}, default="signal")
        say(f"On-board connector for '{name}':")
        conn = _choice(ask, say, "Choose", ON_BOARD,
                       default="microfit" if kind == "power" else "jst-xa")
        detail = ""
        if conn == "other":
            detail = (ask("Describe the connector: ") or "").strip()
        where = _choice(ask, say, f"'{name}' placement",
                        {"edge": "board EDGE (direct external access)",
                         "remote": "REMOTE — harness to a panel/enclosure face"},
                        default="remote")
        panel = None
        if where == "remote":
            panel = (ask(f"Panel connector for '{name}' "
                         "(empty = sealed latching default): ") or "").strip()
            if not panel:
                panel = PANEL_DEFAULTS.get(kind, PANEL_DEFAULTS["signal"])
                say(f"  -> defaulting to: {panel}")
        intent["interfaces"].append(dict(
            name=name, kind=kind, board_connector=conn,
            board_connector_note=detail or ON_BOARD[conn][1],
            placement=where, panel_connector=panel))

    say("== Mounting ==")
    holes = _choice(ask, say, "Mounting holes",
                    {"yes": "yes", "no": "no"}, default="yes") == "yes"
    m = {"holes": holes}
    if holes:
        m["pattern"] = _choice(
            ask, say, "Pattern",
            {"corners": "4 corners, equal inset from the board edges",
             "free": "does not matter / place where space allows"},
            default="corners")
        m["size"] = _choice(ask, say, "Screw size",
                            {"M2.5": "M2.5", "M3": "M3", "M2": "M2"},
                            default="M3")
        m["inset_mm"] = float(ask("Corner inset in mm [default 4.0]: ")
                              or "4.0")
    intent["mounting"] = m
    intent["environment"] = ((answers or {}).get("environment")
                             or ask_environment(ask, say))
    return intent


ENV_LOCATION = {
    "bench": ("Indoor, bench or desk (0..+50 C, no vibration)",
              dict(temp_min_c=0, temp_max_c=50, vibration="low", moisture="dry")),
    "enclosed-outdoor": ("Sealed enclosure outdoors / vehicle cabin "
                         "(-30..+85 C, humid, vibration)",
                         dict(temp_min_c=-30, temp_max_c=85, vibration="high",
                              moisture="humid")),
    "exposed-vehicle": ("Vehicle exterior / UTV / marine (-40..+85 C, wet, "
                        "severe vibration and shock)",
                        dict(temp_min_c=-40, temp_max_c=85, vibration="high",
                             moisture="outdoor")),
    "industrial": ("Industrial cabinet (-20..+70 C, dust, some vibration)",
                   dict(temp_min_c=-20, temp_max_c=70, vibration="high",
                        moisture="humid")),
    "custom": ("Custom — enter the numbers", {}),
}

ENV_TRANSIENT = {
    "none": "Regulated supply / USB / battery only",
    "auto-12v": "12 V vehicle battery (load dump, ISO 7637 pulses)",
    "auto-24v": "24 V vehicle battery",
}


def ask_environment(ask, say):
    """Where will this product LIVE? Every part is derated against the answer
    and the review gate fails a part rated narrower than the product. This
    exists because a 0..+70 C LAN transformer reached an outdoor UTV board's
    external review with nothing having asked."""
    say("== Product environment ==")
    loc = _choice(ask, say, "Where does the product live", ENV_LOCATION,
                  default="enclosed-outdoor")
    env = dict(ENV_LOCATION[loc][1])
    env["location"] = loc
    if loc == "custom" or not env.get("temp_min_c") and env.get("temp_min_c") != 0:
        env["temp_min_c"] = float(ask("Minimum operating temperature C [-40]: ") or -40)
        env["temp_max_c"] = float(ask("Maximum operating temperature C [85]: ") or 85)
        env["vibration"] = _choice(ask, say, "Vibration",
                                   {"low": "low", "high": "high"}, default="high")
        env["moisture"] = _choice(ask, say, "Moisture",
                                  {"dry": "dry", "humid": "humid",
                                   "condensing": "condensing", "outdoor": "outdoor"},
                                  default="humid")
    env["transient"] = _choice(ask, say, "Input power transient class",
                               ENV_TRANSIENT, default="auto-12v")
    say(f"  -> env: {env['temp_min_c']:g}..{env['temp_max_c']:g} C, "
        f"vibration {env['vibration']}, moisture {env['moisture']}, "
        f"transient {env['transient']}")
    return env


def load_intent(path):
    return json.load(open(path))


HOLE_FP = {"M2": "MountingHole_2.2mm_M2_DIN965_Pad",
           "M2.5": "MountingHole_2.7mm_M2.5_DIN965_Pad_TopBottom",
           "M3": "MountingHole_3.2mm_M3_DIN965_Pad"}


def apply_mounting(board, intent, lib="/usr/share/kicad/footprints/"
                                      "MountingHole.pretty", log=print):
    """Add corner mounting holes per intent to a board WITH an outline.
    Locked, on GND when the net exists. Returns refs added."""
    import pcbnew
    m = intent.get("mounting", {})
    if not m.get("holes") or m.get("pattern") != "corners":
        return []
    bb = board.GetBoardEdgesBoundingBox()
    ins = pcbnew.FromMM(m.get("inset_mm", 4.0))
    corners = [(bb.GetLeft() + ins, bb.GetTop() + ins),
               (bb.GetRight() - ins, bb.GetTop() + ins),
               (bb.GetLeft() + ins, bb.GetBottom() - ins),
               (bb.GetRight() - ins, bb.GetBottom() - ins)]
    have = {f.GetReference() for f in board.GetFootprints()}
    gnd = board.FindNet("GND")
    added = []
    for i, (x, y) in enumerate(corners, 1):
        ref = f"H{i}"
        if ref in have:
            continue
        fp = pcbnew.FootprintLoad(lib, HOLE_FP[m.get("size", "M3")])
        fp.SetReference(ref)
        fp.SetValue(m.get("size", "M3") + " mount")
        board.Add(fp)
        fp.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        fp.SetLocked(True)
        if gnd:
            for p in fp.Pads():
                p.SetNet(gnd)
        added.append(ref)
        log(f"  mounting hole {ref} at corner {i}")
    return added
