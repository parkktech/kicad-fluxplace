"""Board audit: the questions a reviewer asks that nothing else answers.

Every function here exists because of a real miss on a real board, not because
it seemed like a nice feature:

  drc_scope()   An outside reviewer looked at a board we had signed off as
                "0 DRC violations at all severities" and pointed out that 13 of
                62 rules were set to `ignore` in the .kicad_pro — including
                solder-mask bridging and annular width, the two that bite at
                assembly on fine-pitch parts. A rule set to ignore is not
                reported at ANY severity, so the clean report was true and
                narrow at the same time, and nothing said so. This reports the
                scope of a DRC result, and can re-run with every check enabled.

  netlist()     The board that shipped for review had no schematic at all — it
                was generated from a netlist spec, so there was no .kicad_sch to
                send. The reviewer needs connectivity regardless. This reads it
                back out of the routed board.

  stackup()     That same board had no stackup defined: no dielectric, no Er,
                inner layers typed as generic signal. So its 100R differential
                and 50R microstrip intent was unverifiable from the files, even
                though the netclass geometry was right there. This reports what
                is actually defined and says plainly when impedance cannot be
                checked.
"""

import json
import os
import re
import subprocess
import tempfile


# --------------------------------------------------------------------------
# DRC scope
# --------------------------------------------------------------------------

# Checks that must be ACTIVE before a package goes to a fab. Each one has an
# assembly-stage failure mode that no other check catches.
FAB_CRITICAL = {
    "solder_mask_bridge":    "mask slivers between fine-pitch pads — the classic "
                             "solder-bridge source, invisible to copper clearance checks",
    "annular_width":         "thin annular rings survive DRC and die at drill breakout",
    "hole_clearance":        "drill hits copper",
    "hole_near_hole":        "drills too close to tear out the web between them",
    "copper_edge_clearance": "copper too near the routed edge",
    "courtyards_overlap":    "parts that physically collide at assembly",
    "missing_courtyard":     "a part with no courtyard is excluded from collision checks",
    "track_dangling":        "stubs that are not connected to anything",
    "via_dangling":          "vias connecting nothing",
}


def read_severities(project_path):
    """rule_severities from a .kicad_pro, or {}."""
    try:
        with open(project_path) as fh:
            d = json.load(fh)
    except Exception:
        return {}
    return (d.get("board", {}).get("design_settings", {})
             .get("rule_severities", {})) or {}


def project_for(board_path):
    """The .kicad_pro beside a .kicad_pcb."""
    return os.path.splitext(board_path)[0] + ".kicad_pro"


def drc_scope(board_path, report_path=None):
    """What a DRC result on this board did and did not examine.

    Reads the project's rule severities directly, and the `ignored_checks` array
    KiCad 10 helpfully writes into its own JSON report — which is the datum we
    were ignoring while printing 'PASS'.
    """
    pro = project_for(board_path)
    sev = read_severities(pro)
    ignored = sorted(k for k, v in sev.items() if v == "ignore")
    active = sorted(k for k, v in sev.items() if v != "ignore")

    out = {
        "board": board_path,
        "project": pro if os.path.exists(pro) else None,
        "rules_total": len(sev),
        "rules_active": len(active),
        "rules_ignored": len(ignored),
        "ignored": ignored,
        "fab_critical_ignored": [
            {"check": k, "why_it_matters": FAB_CRITICAL[k]}
            for k in ignored if k in FAB_CRITICAL
        ],
    }

    if report_path and os.path.exists(report_path):
        try:
            with open(report_path) as fh:
                rep = json.load(fh)
            out["report"] = {
                "path": report_path,
                "violations": len(rep.get("violations", [])),
                "unconnected": len(rep.get("unconnected_items", [])),
                "ignored_checks_declared": [c.get("key") for c in
                                            rep.get("ignored_checks", [])],
                "severities_included": rep.get("included_severities"),
            }
        except Exception as e:
            out["report"] = {"path": report_path, "error": str(e)}

    crit = out["fab_critical_ignored"]
    if not sev:
        out["verdict"] = "UNKNOWN — no rule_severities found; KiCad defaults apply"
    elif crit:
        out["verdict"] = ("NARROW — %d check(s) that matter for fabrication are "
                          "ignored. A clean report here does not mean what it "
                          "looks like." % len(crit))
    elif ignored:
        out["verdict"] = ("QUALIFIED — %d check(s) ignored, none of them "
                          "fab-critical." % len(ignored))
    else:
        out["verdict"] = "FULL — every rule is evaluated."
    return out


def drc_full(board_path, kicad_cli="kicad-cli", out_path=None, log=print):
    """Run DRC with EVERY check enabled, on a throwaway copy of the project.

    The severities live in the .kicad_pro, so enabling them means editing it.
    We copy the project rather than touch the user's, because an audit that
    mutates the thing it audits is not an audit.
    """
    pro = project_for(board_path)
    if not os.path.exists(pro):
        return {"error": "no .kicad_pro beside the board; cannot override severities"}

    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.basename(os.path.splitext(board_path)[0])
        tboard = os.path.join(tmp, base + ".kicad_pcb")
        tpro = os.path.join(tmp, base + ".kicad_pro")
        import shutil
        shutil.copy2(board_path, tboard)
        shutil.copy2(pro, tpro)

        with open(tpro) as fh:
            d = json.load(fh)
        rs = d.setdefault("board", {}).setdefault("design_settings", {}) \
              .setdefault("rule_severities", {})
        flipped = [k for k, v in rs.items() if v == "ignore"]
        for k in flipped:
            rs[k] = "error"
        with open(tpro, "w") as fh:
            json.dump(d, fh, indent=2)

        rpt = out_path or os.path.join(tmp, "drc-full.json")
        cmd = [kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
               "--output", rpt, tboard]
        log("re-running DRC with %d previously-ignored check(s) enabled" % len(flipped))
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as e:
            return {"error": "kicad-cli drc failed: %s" % e}

        if not os.path.exists(rpt):
            return {"error": "no DRC report produced"}
        with open(rpt) as fh:
            rep = json.load(fh)

        by_type = {}
        for v in rep.get("violations", []):
            by_type[v.get("type")] = by_type.get(v.get("type"), 0) + 1

        result = {
            "enabled_for_this_run": flipped,
            "violations": len(rep.get("violations", [])),
            "unconnected": len(rep.get("unconnected_items", [])),
            "by_type": by_type,
            "newly_surfaced": {k: n for k, n in by_type.items() if k in flipped},
            "report_path": rpt if out_path else None,
        }
        if out_path:
            result["report_path"] = out_path
        return result


# --------------------------------------------------------------------------
# netlist
# --------------------------------------------------------------------------

def netlist(board_path, fmt="text"):
    """Read connectivity back out of a routed board.

    For a board with no schematic this IS the netlist. Even when a schematic
    exists, this is what the copper actually says, which is the more useful
    document when the two disagree.
    """
    import pcbnew
    board = pcbnew.LoadBoard(board_path)

    fps = {}
    for fp in board.GetFootprints():
        fps[fp.GetReference()] = fp

    def refkey(r):
        m = re.match(r"([A-Za-z_]+)(\d*)", r)
        return (m.group(1), int(m.group(2) or 0)) if m else (r, 0)

    nets = {}
    comps = []
    for ref in sorted(fps, key=refkey):
        fp = fps[ref]
        pos = fp.GetPosition()
        pins = []
        for pad in sorted(fp.Pads(), key=lambda p: p.GetNumber()):
            n = pad.GetNetname()
            pins.append({"pad": pad.GetNumber(), "net": n or None})
            if n:
                nets.setdefault(n, []).append("%s.%s" % (ref, pad.GetNumber()))
        comps.append({
            "ref": ref,
            "value": fp.GetValue(),
            "footprint": str(fp.GetFPIDAsString()),
            "side": "back" if fp.IsFlipped() else "front",
            "x": round(pcbnew.ToMM(pos.x), 3),
            "y": round(pcbnew.ToMM(pos.y), 3),
            "pins": pins,
        })

    data = {
        "board": board_path,
        "components": len(comps),
        "nets": len(nets),
        "connected_pads": sum(len(v) for v in nets.values()),
        "net_table": {k: sorted(v) for k, v in sorted(nets.items())},
        "component_table": comps,
    }
    if fmt == "json":
        return data
    return _netlist_text(data)


def _netlist_text(d):
    L = []
    L.append("Connection list read from %s" % os.path.basename(d["board"]))
    L.append("%d components, %d nets, %d connected pads"
             % (d["components"], d["nets"], d["connected_pads"]))
    L.append("=" * 74)
    L.append("")
    L.append("NETS")
    L.append("-" * 74)
    for n, pads in d["net_table"].items():
        L.append("")
        L.append("%s   (%d pads)" % (n, len(pads)))
        line = "    "
        for p in pads:
            if len(line) + len(p) + 2 > 74:
                L.append(line.rstrip().rstrip(",")); line = "    "
            line += p + ", "
        if line.strip():
            L.append(line.rstrip().rstrip(","))
    L.append("")
    L.append("")
    L.append("COMPONENTS")
    L.append("-" * 74)
    L.append("%-8s %-22s %-42s %-6s %s" % ("REF", "VALUE", "FOOTPRINT", "SIDE", "POS"))
    for c in d["component_table"]:
        L.append("%-8s %-22s %-42s %-6s (%.3f, %.3f)"
                 % (c["ref"], c["value"][:22], c["footprint"][:42], c["side"],
                    c["x"], c["y"]))
    return "\n".join(L)


# --------------------------------------------------------------------------
# stackup
# --------------------------------------------------------------------------

def stackup(board_path):
    """What the board says about its own layer stack — and whether that is
    enough to verify controlled impedance.

    A netclass can specify a 0.19 mm differential pair all it likes; without
    dielectric thickness and Er, nobody can say whether that is 100 ohms.
    Saying so is more useful than reporting the geometry as if it were settled.
    """
    import pcbnew
    board = pcbnew.LoadBoard(board_path)

    with open(board_path) as fh:
        raw = fh.read()
    has_stackup = "(stackup" in raw
    has_dielectric = "dielectric" in raw or "epsilon" in raw

    layers = []
    for lid in board.GetEnabledLayers().CuStack():
        layers.append({"id": lid, "name": board.GetLayerName(lid)})

    zones = {}
    for z in board.Zones():
        seq = z.GetLayerSet().Seq()
        for i in range(len(seq)):
            nm = board.GetLayerName(seq[i])
            zones.setdefault(nm, set()).add(z.GetNetname() or "<none>")

    pro = project_for(board_path)
    classes, patterns = [], []
    try:
        with open(pro) as fh:
            d = json.load(fh)
        ns = d.get("net_settings", {})
        for c in ns.get("classes", []):
            classes.append({
                "name": c.get("name"),
                "track_width": c.get("track_width"),
                "clearance": c.get("clearance"),
                "via_diameter": c.get("via_diameter"),
                "via_drill": c.get("via_drill"),
                "diff_pair_width": c.get("diff_pair_width"),
                "diff_pair_gap": c.get("diff_pair_gap"),
            })
        patterns = ns.get("netclass_patterns", [])
    except Exception:
        pass

    out = {
        "board": board_path,
        "copper_layers": len(layers),
        "layers": layers,
        "board_thickness_mm": round(
            pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness()), 3),
        "stackup_defined": has_stackup,
        "dielectric_defined": has_dielectric,
        "plane_layers": {k: sorted(v) for k, v in sorted(zones.items())},
        "netclasses": classes,
        "netclass_patterns": patterns,
    }

    diff = [c for c in classes if c.get("diff_pair_width")]
    if not has_stackup or not has_dielectric:
        out["impedance_verifiable"] = False
        msg = ("NO — the board carries no stackup definition (no dielectric "
               "thickness, no Er), so controlled impedance cannot be checked "
               "from these files. KiCad defaults are in force.")
        if diff:
            msg += (" %d netclass(es) specify differential geometry "
                    "(%s), but whether that geometry lands on the intended "
                    "impedance depends entirely on the undefined stackup."
                    % (len(diff), ", ".join(c["name"] for c in diff)))
        out["impedance_note"] = msg
    else:
        out["impedance_verifiable"] = True
        out["impedance_note"] = ("Stackup and dielectric are defined; impedance "
                                 "can be computed from these files.")
    return out
