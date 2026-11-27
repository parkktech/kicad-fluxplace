"""fabconform: house limits -> guarded fixes (track widen, via ring), per-house verdicts, the whole flow on a pcbnew mini board."""
import pytest

from fluxplace import fabconform as FX, fabcaps as FC


def _lims(layers=6):
    return FX.house_limits(FX.resolve_houses("jlcpcb,pcbway"), layers)


def test_resolve_houses_bare_vendor_is_standard_tier():
    assert FX.resolve_houses("jlcpcb, pcbway-fine-drill") == [("jlcpcb", "jlcpcb-standard"), ("pcbway", "pcbway-fine-drill")]
    with pytest.raises(FC.FabProfileError):
        FX.resolve_houses("nosuchfab")


def test_strictest_is_the_larger_minimum():
    l = _lims()
    assert FX.strictest(l, "track_min") == max(x["track_min"] for x in l.values())
    assert FX.strictest(l, "via_annular_min") == max(x["via_annular_min"] for x in l.values())


_VIA = "\t(via\n\t\t(at 1 2)\n\t\t(size 0.45)\n\t\t(drill 0.2)\n\t\t(layers \"F.Cu\" \"B.Cu\")\n\t\t(net \"A\")\n\t\t(uuid \"u1\")\n\t)\n"
_SEG = "\t(segment\n\t\t(start 0 0)\n\t\t(end 3 0)\n\t\t(width 0.088)\n\t\t(layer \"F.Cu\")\n\t\t(net \"A\")\n\t\t(uuid \"u2\")\n\t)\n"


def test_parse_and_apply_edits_touch_only_the_named_fields():
    text = "(kicad_pcb\n\t(version 1)\n" + _SEG + _VIA + ")\n"
    segs, vias = FX.parse(text)
    assert len(segs) == 1 and len(vias) == 1 and segs[0]["w"] == 0.088 and vias[0]["drill"] == 0.2
    out = FX.apply_edits(text, [(segs[0]["span"], {"width": 0.1}), (vias[0]["span"], {"size": 0.455, "drill": 0.2})])
    s2, v2 = FX.parse(out)
    assert s2[0]["w"] == 0.1 and v2[0]["size"] == 0.455 and (s2[0]["x2"], s2[0]["net"]) == (3.0, "A")
    assert out.replace("0.1)", "0.088)").replace("0.455", "0.45") == text


def test_via_candidates_reach_ring_smallest_pad_growth_first_and_keep_power_drill():
    v = {"size": 0.45, "drill": 0.25}
    c = FX.via_candidates(v, 0.127, 0.2, 0.45, keep_drill=False)
    assert all((p - d) / 2 >= 0.127 - 1e-9 and p >= 0.45 for p, d, _l in c)
    assert [x[:2] for x in c][0] == (0.455, 0.2)                        # smaller drill, almost no pad growth
    assert all(d == 0.25 for _p, d, _l in FX.via_candidates(v, 0.127, 0.2, 0.45, keep_drill=True))
    assert FX.via_candidates({"size": 0.6, "drill": 0.3}, 0.127, 0.2, 0.45, False) == [] or all(
        (p - d) / 2 >= 0.127 - 1e-9 for p, d, _l in FX.via_candidates({"size": 0.6, "drill": 0.3}, 0.127, 0.2, 0.45, False))


def test_track_ladder_strictest_first():
    assert FX.track_ladder(0.088, _lims()) == [0.1, 0.09]
    assert FX.track_ladder(0.095, _lims()) == [0.1]
    assert FX.track_ladder(0.2, _lims()) == []


def test_house_verdicts_ok_option_impossible():
    hs, l = FX.resolve_houses("jlcpcb,pcbway"), _lims()
    v = FX.classify("track", 0.09, hs, 6, l)                              # PCBWay Advanced local 3.5/3.5 mil = 0.0889
    assert v["jlcpcb"]["status"] == "ok" and v["pcbway"]["status"] == "option" and FX.action_of(v, True) == "ORDER-OPTION"
    v = FX.classify("track", 0.085, hs, 6, l)
    assert v["pcbway"]["status"] == "impossible" and FX.action_of(v, True) == "IMPOSSIBLE"
    v = FX.classify("via_ring", 0.125, hs, 6, l)                          # JLC standard 0.125 ok; PCBWay standard 0.127 -> medium difficulty
    assert v["jlcpcb"]["status"] == "ok" and v["pcbway"]["status"] == "option" and "capabilities.html" in v["pcbway"]["source"]
    assert FX.action_of(v, False) == "ORDER-OPTION"
    v = FX.classify("via_ring", 0.1, hs, 6, l)                            # below PCBWay medium (0.1016): impossible there; JLC registry hard 0.05
    assert v["pcbway"]["status"] == "impossible" and v["jlcpcb"]["status"] == "ok"
    assert FX.action_of(FX.classify("track", 0.1, hs, 6, l), True) == "FIXED"
    assert FX.action_of(FX.classify("track", 0.1, hs, 6, l), False) == "OK"


def test_house_rule_pro_edit_sets_house_space_not_project_preference():
    pd = {"net_settings": {"classes": [{"clearance": 0.12}]}}
    FX._house_pro_edit(_lims())(pd)
    assert pd["net_settings"]["classes"][0]["clearance"] == 0.1 and pd["board"]["design_settings"]["rules"]["min_via_annular_width"] == 0.127
    pd = {"net_settings": {"classes": [{"clearance": 0.2}]}}
    FX._relax_edit(_lims())(pd)
    assert pd["net_settings"]["classes"][0]["clearance"] == 0.1


def _mini(path, tracks, vias):
    import pcbnew
    b = pcbnew.CreateEmptyBoard()
    b.SetCopperLayerCount(6)
    b.GetDesignSettings().m_MinClearance = pcbnew.FromMM(0.08)
    P = lambda x, y: pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    nets = {}
    for n in sorted({t[0] for t in tracks} | {v[0] for v in vias}):
        nets[n] = pcbnew.NETINFO_ITEM(b, n)
        b.Add(nets[n])
    for n, a, z, w in tracks:
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(P(*a)); t.SetEnd(P(*z)); t.SetWidth(pcbnew.FromMM(w)); t.SetLayer(pcbnew.F_Cu); t.SetNet(nets[n])
        b.Add(t)
    for n, c, dia, dr in vias:
        v = pcbnew.PCB_VIA(b)
        v.SetPosition(P(*c)); v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(dia)); v.SetDrill(pcbnew.FromMM(dr))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(nets[n])
        b.Add(v)
    pcbnew.SaveBoard(str(path), b)


def test_run_fixes_track_and_via_and_leaves_the_rest_as_order_options(tmp_path):
    src, out = tmp_path / "a.kicad_pcb", tmp_path / "o.kicad_pcb"
    _mini(src, [("A", (5, 5), (15, 5), 0.088)], [("A", (15, 5), 0.45, 0.2), ("B", (30, 30), 0.45, 0.2), ("C", (40, 40), 0.52, 0.4)])
    rep = FX.run(str(src), {}, houses="jlcpcb,pcbway", out=str(out), log=lambda *_a: None)
    assert rep["accepted"]
    segs, vias = FX.parse(out.read_text())
    assert min(s["w"] for s in segs) >= 0.1 - 1e-9
    assert all((v["size"] - v["drill"]) / 2 >= 0.130 - 1e-9 for v in vias if v["net"] in ("A", "B"))
    t = [g for g in rep["rows"] if g["kind"] == "track"][0]
    assert t["action"] == "FIXED" and t["old"] == 0.088 and t["new"] == 0.1
    vias_rows = [g for g in rep["rows"] if g["kind"] == "via_ring"]
    assert {g["action"] for g in vias_rows} <= {"FIXED"} and sum(g["n"] for g in vias_rows) == 3
    notes = FX.order_notes(rep)
    assert set(notes) == {"jlcpcb", "pcbway"} and "None" in notes["pcbway"]


def test_audit_lists_every_sub_minimum_item_and_notes_split_options_from_confirmations(tmp_path):
    src = tmp_path / "a.kicad_pcb"
    _mini(src, [("A", (5, 5), (15, 5), 0.088), ("A", (5, 8), (15, 8), 0.2)], [("A", (30, 30), 0.45, 0.2), ("B", (40, 40), 0.6, 0.3)])
    rep = FX.audit(str(src), {}, houses="jlcpcb,pcbway", log=lambda *_a: None)
    assert rep["coverage"] == {"raw": 2, "listed": 2, "ok": True}
    opts, conf = FX.house_lines(rep, "pcbway")
    assert any("0.088" in x and "capabilities.html" in x for x in conf) and any("via annular ring" in x for x in opts)
    assert "Engineering confirmation" in FX.order_notes(rep)["pcbway"]
    assert FX.house_lines(rep, "jlcpcb")[1] == []


def test_deliver_helpers_write_house_notes_and_skip_without_fab_profile(tmp_path):
    import types
    import cli
    src, cons = tmp_path / "a.kicad_pcb", tmp_path / "c.toml"
    _mini(src, [("A", (5, 5), (15, 5), 0.088)], [("A", (30, 30), 0.45, 0.2)])
    cons.write_text("[fab]\nprofile = [\"jlcpcb-standard\", \"pcbway-standard\"]\n")
    a = types.SimpleNamespace(board=str(src), constraints=str(cons))
    rep = cli._deliver_fabconform(a)
    assert rep["coverage"]["ok"] and set(rep["houses"]) == {"jlcpcb-standard", "pcbway-standard"}
    cli._write_house_notes(rep, str(tmp_path), "pkg")
    assert "0.088" in (tmp_path / "pkg-order-notes-pcbway.md").read_text() and (tmp_path / "pkg-order-notes-jlcpcb.md").exists()
    cons.write_text("[route]\nmin_track_mm = 0.1\n")
    assert cli._deliver_fabconform(a) is None
