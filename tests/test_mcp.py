"""MCP server + audit tests.

These are deliberately protocol-level and pcbnew-free where possible: the
JSON-RPC surface and the schema derivation are what break silently when the CLI
changes, and they can both be exercised without a board.
"""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxplace import mcp_server as M


class TestSchemaDerivation(unittest.TestCase):
    """The tool schemas come from the live CLI parser. If that link breaks the
    MCP surface silently drifts from what fluxplace can actually do."""

    def test_tools_are_derived_from_the_cli(self):
        tools = M.build_tools()
        self.assertTrue(tools, "no tools derived at all")
        names = {t["name"] for t in tools}
        self.assertIn("fluxplace_doctor", names)
        self.assertIn("fluxplace_drc_scope", names)

    def test_long_running_hidden_by_default(self):
        default = {t["name"] for t in M.build_tools(expose_long=False)}
        every = {t["name"] for t in M.build_tools(expose_long=True)}
        self.assertNotIn("fluxplace_auto", default)
        self.assertIn("fluxplace_auto", every)
        self.assertLess(len(default), len(every))

    def test_required_args_are_marked_required(self):
        t = next(t for t in M.build_tools() if t["name"] == "fluxplace_netlist")
        self.assertIn("board", t["inputSchema"]["properties"])
        self.assertIn("board", t["inputSchema"].get("required", []))

    def test_store_true_becomes_boolean(self):
        t = next(t for t in M.build_tools() if t["name"] == "fluxplace_drc_scope")
        self.assertEqual(t["inputSchema"]["properties"]["full"]["type"], "boolean")

    def test_int_option_becomes_integer(self):
        every = M.build_tools(expose_long=True)
        found = False
        for t in every:
            for name, frag in t["inputSchema"]["properties"].items():
                if frag.get("type") == "integer":
                    found = True
        self.assertTrue(found, "no integer-typed option derived from any command")

    def test_every_policy_entry_names_a_real_command(self):
        import cli
        subs = M._subparsers(cli.build_parser())
        for cmd in M.POLICY:
            self.assertIn(cmd, subs, "POLICY names %r which the CLI does not have" % cmd)


class TestToolNaming(unittest.TestCase):
    def test_roundtrip(self):
        for cmd in ("doctor", "drc-scope", "comprehend-intent", "verify-models"):
            self.assertEqual(M.cmd_from_tool(M.tool_name(cmd)), cmd)

    def test_foreign_names_rejected(self):
        self.assertIsNone(M.cmd_from_tool("something_else"))


class TestArgvConstruction(unittest.TestCase):
    def setUp(self):
        import cli
        self.subs = M._subparsers(cli.build_parser())

    def test_flags_and_values(self):
        argv = M._argv_for("drc-scope", {"board": "/b.kicad_pcb", "full": True},
                           self.subs["drc-scope"])
        self.assertEqual(argv[0], "drc-scope")
        self.assertIn("--board", argv)
        self.assertIn("/b.kicad_pcb", argv)
        self.assertIn("--full", argv)

    def test_false_boolean_is_omitted(self):
        argv = M._argv_for("drc-scope", {"board": "/b.kicad_pcb", "full": False},
                           self.subs["drc-scope"])
        self.assertNotIn("--full", argv)

    def test_none_is_omitted(self):
        argv = M._argv_for("drc-scope", {"board": "/b.kicad_pcb", "report": None},
                           self.subs["drc-scope"])
        self.assertNotIn("--report", argv)

    def test_unknown_key_is_ignored_not_fatal(self):
        argv = M._argv_for("drc-scope", {"board": "/b", "bogus": "x"},
                           self.subs["drc-scope"])
        self.assertNotIn("bogus", argv)


class TestProtocol(unittest.TestCase):
    def _rpc(self, messages, expose_long=False):
        out = io.StringIO()
        srv = M.Server(expose_long=expose_long, out=out)
        srv.serve(io.StringIO("\n".join(json.dumps(m) for m in messages)))
        return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]

    def test_initialize(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}}])
        self.assertEqual(len(r), 1)
        res = r[0]["result"]
        self.assertEqual(res["serverInfo"]["name"], "fluxplace")
        self.assertIn("tools", res["capabilities"])
        self.assertTrue(res["instructions"])

    def test_notifications_get_no_response(self):
        """A JSON-RPC notification has no id and MUST NOT be answered. Replying
        to one corrupts the stream for strict clients."""
        r = self._rpc([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(r, [])

    def test_tools_list_shape(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        tools = r[0]["result"]["tools"]
        self.assertTrue(tools)
        for t in tools:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            # internal bookkeeping must not leak into the protocol
            self.assertNotIn("_kind", t)
            self.assertNotIn("_cmd", t)

    def test_unknown_method_is_a_jsonrpc_error(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 9, "method": "no/such"}])
        self.assertEqual(r[0]["error"]["code"], -32601)

    def test_parse_error(self):
        out = io.StringIO()
        M.Server(out=out).serve(io.StringIO("{not json\n"))
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)

    def test_ping(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 3, "method": "ping"}])
        self.assertEqual(r[0]["result"], {})

    def test_long_running_tool_is_refused_by_default(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "fluxplace_auto", "arguments": {}}}])
        res = r[0]["result"]
        self.assertTrue(res["isError"])
        self.assertIn("long-running", res["content"][0]["text"])

    def test_unknown_tool_is_an_error_result_not_a_crash(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "fluxplace_nope", "arguments": {}}}])
        self.assertTrue(r[0]["result"]["isError"])

    def test_doctor_runs_through_the_protocol(self):
        r = self._rpc([{"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                        "params": {"name": "fluxplace_doctor",
                                   "arguments": {"problems_only": True}}}])
        res = r[0]["result"]
        self.assertFalse(res["isError"])
        self.assertIn("fluxplace preflight", res["content"][0]["text"])


class TestDrcScope(unittest.TestCase):
    """The audit that an outside reviewer had to teach us to run."""

    def _project(self, severities):
        import tempfile
        d = tempfile.mkdtemp()
        board = os.path.join(d, "b.kicad_pcb")
        with open(board, "w") as fh:
            fh.write("(kicad_pcb)")
        pro = os.path.join(d, "b.kicad_pro")
        with open(pro, "w") as fh:
            json.dump({"board": {"design_settings":
                                 {"rule_severities": severities}}}, fh)
        return board

    def test_fab_critical_ignore_is_called_narrow(self):
        from fluxplace import audit
        b = self._project({"solder_mask_bridge": "ignore", "annular_width": "ignore",
                           "clearance": "error"})
        r = audit.drc_scope(b)
        self.assertEqual(r["rules_ignored"], 2)
        self.assertEqual(len(r["fab_critical_ignored"]), 2)
        self.assertTrue(r["verdict"].startswith("NARROW"))

    def test_harmless_ignore_is_only_qualified(self):
        from fluxplace import audit
        b = self._project({"tuning_profile_track_geometries": "ignore",
                           "clearance": "error"})
        r = audit.drc_scope(b)
        self.assertTrue(r["verdict"].startswith("QUALIFIED"))
        self.assertEqual(r["fab_critical_ignored"], [])

    def test_nothing_ignored_is_full(self):
        from fluxplace import audit
        b = self._project({"clearance": "error", "solder_mask_bridge": "warning"})
        self.assertTrue(audit.drc_scope(b)["verdict"].startswith("FULL"))

    def test_no_project_is_unknown_not_a_pass(self):
        """The dangerous failure is reporting 'fine' when we could not look."""
        import tempfile
        from fluxplace import audit
        d = tempfile.mkdtemp()
        b = os.path.join(d, "lonely.kicad_pcb")
        with open(b, "w") as fh:
            fh.write("(kicad_pcb)")
        self.assertTrue(audit.drc_scope(b)["verdict"].startswith("UNKNOWN"))

    def test_report_ignored_checks_are_surfaced(self):
        import tempfile
        from fluxplace import audit
        b = self._project({"annular_width": "ignore"})
        rp = os.path.join(tempfile.mkdtemp(), "drc.json")
        with open(rp, "w") as fh:
            json.dump({"violations": [], "unconnected_items": [],
                       "ignored_checks": [{"key": "annular_width"}]}, fh)
        r = audit.drc_scope(b, rp)
        self.assertEqual(r["report"]["violations"], 0)
        self.assertIn("annular_width", r["report"]["ignored_checks_declared"])


if __name__ == "__main__":
    unittest.main()


class TestImpedance(unittest.TestCase):
    """The solver that found six RF nets at 74 ohm on a board that had already
    passed DRC, netlist verification and a fab quote."""

    def test_known_50_ohm_geometry(self):
        from fluxplace import stackup as ST
        # 0.33 mm over 0.2 mm FR4 prepreg is the classic ~50 ohm microstrip
        z = ST.microstrip_z0(0.3304, 0.2, 0.035, 4.4)
        self.assertAlmostEqual(z, 50.0, delta=1.0)

    def test_impedance_falls_as_width_rises(self):
        from fluxplace import stackup as ST
        wide = ST.microstrip_z0(0.5, 0.2, 0.035, 4.4)
        narrow = ST.microstrip_z0(0.1, 0.2, 0.035, 4.4)
        self.assertLess(wide, narrow)

    def test_solver_inverts_the_forward_calc(self):
        from fluxplace import stackup as ST
        for target in (40.0, 50.0, 75.0, 90.0):
            w = ST.solve_microstrip_width(target, 0.2, 0.035, 4.4)
            self.assertIsNotNone(w, "no width found for %s ohm" % target)
            self.assertAlmostEqual(ST.microstrip_z0(w, 0.2, 0.035, 4.4),
                                   target, delta=0.5)

    def test_differential_is_near_twice_single_ended_but_lower(self):
        from fluxplace import stackup as ST
        z0 = ST.microstrip_z0(0.2, 0.2, 0.035, 4.4)
        zd = ST.diff_microstrip_z(0.2, 0.2, 0.2, 0.035, 4.4)
        self.assertLess(zd, 2 * z0)          # coupling always pulls it down
        self.assertGreater(zd, 1.2 * z0)

    def test_tighter_gap_lowers_differential_impedance(self):
        from fluxplace import stackup as ST
        loose = ST.diff_microstrip_z(0.2, 0.5, 0.2, 0.035, 4.4)
        tight = ST.diff_microstrip_z(0.2, 0.1, 0.2, 0.035, 4.4)
        self.assertLess(tight, loose)

    def test_profiles_sum_to_their_nominal_thickness(self):
        from fluxplace import stackup as ST
        for name in ST.profile_names():
            t = ST.total_thickness(name)
            self.assertAlmostEqual(t, 1.6, delta=0.05,
                                   msg="%s totals %s mm" % (name, t))

    def test_rf_net_detection(self):
        from fluxplace import stackup as ST
        for n in ("RF_GNSS", "ANT_BIAS", "GNSS_PPS", "U_FL_IN"):
            self.assertTrue(ST.looks_rf(n), n)
        for n in ("SPK_COMM_P", "+3V3", "SDA"):
            self.assertFalse(ST.looks_rf(n), n)

    def test_stackup_sexp_is_wellformed(self):
        from fluxplace import stackup as ST
        s = ST.render_stackup_sexp("pcbway-4l-1.6")
        self.assertEqual(s.count("("), s.count(")"), "unbalanced parens")
        self.assertIn("epsilon_r", s)
        self.assertIn('(type "copper")', s)

    def test_apply_refuses_to_clobber_an_existing_stackup(self):
        import tempfile
        from fluxplace import stackup as ST
        p = os.path.join(tempfile.mkdtemp(), "b.kicad_pcb")
        with open(p, "w") as fh:
            fh.write("(kicad_pcb\n\t(setup\n\t\t(stackup\n\t\t)\n\t)\n)")
        r = ST.apply_to_board(p, "pcbway-4l-1.6", backup=False)
        self.assertFalse(r["changed"])
        self.assertIn("already", r["reason"])

    def test_apply_inserts_into_setup(self):
        import tempfile
        from fluxplace import stackup as ST
        p = os.path.join(tempfile.mkdtemp(), "b.kicad_pcb")
        with open(p, "w") as fh:
            fh.write("(kicad_pcb\n\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n)")
        r = ST.apply_to_board(p, "pcbway-4l-1.6", backup=False)
        self.assertTrue(r["changed"])
        with open(p) as fh:
            out = fh.read()
        self.assertIn("(stackup", out)
        self.assertIn("epsilon_r", out)


class TestSixLayerAndPlanes(unittest.TestCase):
    """6-layer support and the reference-plane contract."""

    def test_six_layer_profiles_exist_and_hit_1_6mm(self):
        from fluxplace import stackup as ST
        for n in ("jlcpcb-6l-1.6", "pcbway-6l-1.6"):
            self.assertIn(n, ST.PROFILES)
            self.assertAlmostEqual(ST.total_thickness(n), 1.6, delta=0.002,
                                   msg="%s totals %s" % (n, ST.total_thickness(n)))

    def test_six_layer_has_six_copper_layers(self):
        from fluxplace import stackup as ST
        for n in ("jlcpcb-6l-1.6", "pcbway-6l-1.6"):
            cu = [l for l in ST.PROFILES[n]["layers"] if l["type"] == "copper"]
            self.assertEqual(len(cu), 6)

    def test_every_profile_declares_its_reference_layers(self):
        """A stackup that does not say which layers are planes cannot be
        checked against the copper, which is how the original miss happened."""
        from fluxplace import stackup as ST
        for n in ST.profile_names():
            if len(ST.PROFILES[n]["layers"]) > 3:      # 2-layer has no inner plane
                self.assertTrue(ST.PROFILES[n].get("reference_layers"),
                                "%s declares no reference_layers" % n)

    def test_thinner_dielectric_needs_a_narrower_50r_trace(self):
        from fluxplace import stackup as ST
        h4, er4 = ST.outer_dielectric("pcbway-4l-1.6")
        h6, er6 = ST.outer_dielectric("pcbway-6l-1.6")
        self.assertLess(h6, h4)
        w4 = ST.solve_microstrip_width(50, h4, 0.035, er4)
        w6 = ST.solve_microstrip_width(50, h6, 0.035, er6)
        self.assertLess(w6, w4)

    def test_diff_options_widen_the_trace_as_the_gap_opens(self):
        from fluxplace import stackup as ST
        h, er = ST.outer_dielectric("pcbway-6l-1.6")
        opts = ST.solve_diff_options(100.0, h, 0.035, er)
        self.assertGreater(len(opts), 3)
        widths = [o["width_mm"] for o in opts]
        self.assertEqual(widths, sorted(widths), "wider gap must allow wider trace")
        for o in opts:
            self.assertAlmostEqual(o["achieved_z"], 100.0, delta=0.5)

    def test_min_feature_flagging(self):
        from fluxplace import stackup as ST
        h, er = ST.outer_dielectric("pcbway-6l-1.6")
        opts = ST.solve_diff_options(100.0, h, 0.035, er, min_feature=0.2)
        self.assertTrue(any(not o["manufacturable"] for o in opts),
                        "a 0.2mm floor should rule some options out")


class TestLayerMigration(unittest.TestCase):
    """4->6 layer promotion. The rules that stop it corrupting a board."""

    def test_plan_frees_the_two_reference_layers(self):
        from fluxplace import migrate
        p = migrate.plan_4l_to_6l()
        froms = {m["from"] for m in p["moves"]}
        tos = {m["to"] for m in p["moves"]}
        self.assertEqual(froms, {"In1.Cu", "In2.Cu"})
        self.assertEqual(tos, {"In2.Cu", "In3.Cu"})
        self.assertEqual({n["layer"] for n in p["new_planes"]}, {"In1.Cu", "In4.Cu"})
        self.assertEqual(p["unchanged"], ["F.Cu", "B.Cu"])

    def test_inner_move_order_avoids_collision(self):
        """In2->In3 must happen before In1->In2, or the second move overwrites
        tracks the first has not read yet."""
        import inspect as _i
        from fluxplace import migrate
        src = _i.getsource(migrate.migrate_4l_to_6l)
        self.assertLess(src.index('moved["In2.Cu->In3.Cu"] += 1'),
                        src.index('moved["In1.Cu->In2.Cu"] += 1'))

    def test_net_from_description(self):
        from fluxplace import migrate
        self.assertEqual(
            migrate._net_from_desc("Track [+5V] on In3.Cu, length 0.6 mm"), "+5V")
        self.assertEqual(
            migrate._net_from_desc("Via [GND] on F.Cu - B.Cu"), "GND")
        self.assertIsNone(migrate._net_from_desc("no brackets here"))

    def test_absorb_only_touches_inner_layers(self):
        """An outer-layer track terminates on outer-layer pads; moving it to a
        plane disconnects it from the pads it exists to reach. Caught in the
        field: absorbing two B.Cu tracks onto In4.Cu turned 0 unconnected
        into 2."""
        import inspect as _i
        from fluxplace import migrate
        src = _i.getsource(migrate.absorb_into_plane)
        self.assertIn("if t.GetLayer() not in inner:", src)

    def test_migration_refuses_blind_buried_vias(self):
        import inspect as _i
        from fluxplace import migrate
        src = _i.getsource(migrate.migrate_4l_to_6l)
        self.assertIn("blind_or_buried_vias", src)

    def test_converge_reports_failure_rather_than_looping_forever(self):
        import inspect as _i
        from fluxplace import migrate
        src = _i.getsource(migrate.converge)
        self.assertIn("max_rounds", src)
        self.assertIn('"converged": False', src)


class TestSourcingPolicy(unittest.TestCase):
    """DigiKey + Mouser only. Pinned, because this drifted once already: the
    dependency registry advertised 'LCSC sourcing' as a suite capability long
    after the code had stopped using it, and the jlcparts index behind LCSC
    search has been returning HTTP 404 for weeks at a time mid-project."""

    def test_policy_is_stated(self):
        from fluxplace import deps
        self.assertIn("DigiKey", deps.SOURCING_POLICY)
        self.assertIn("Mouser", deps.SOURCING_POLICY)
        self.assertIn("LCSC", deps.SOURCING_POLICY)

    def test_no_forbidden_source_is_advertised_as_a_capability(self):
        from fluxplace import deps
        for req in deps.REQUIREMENTS:
            why = (req.get("why") or "").lower()
            label = (req.get("label") or "").lower()
            for bad in deps.FORBIDDEN_SOURCES:
                self.assertNotIn(bad, label,
                                 "%s is advertised in a requirement label" % bad)
                if bad in why:
                    self.assertIn("not used", why,
                                  "%r appears in %r without disclaiming it"
                                  % (bad, req.get("key")))

    def test_sourcing_code_queries_only_digikey_and_mouser(self):
        """The gate's answer must not depend on a third-party mirror being up."""
        import os
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "fluxplace", "sourcing.py")).read()
        hosts = set(re.findall(r"https?://([A-Za-z0-9.\-]+)", src))
        for h in hosts:
            self.assertTrue(
                any(ok in h for ok in ("digikey.com", "mouser.com")),
                "sourcing.py reaches a non-DigiKey/Mouser host: %s" % h)

    def test_model_fetch_queries_only_digikey_and_mouser(self):
        import os
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "fluxplace", "models.py")).read()
        for h in set(re.findall(r"https?://([A-Za-z0-9.\-]+)", src)):
            self.assertFalse(
                any(bad in h.lower() for bad in ("lcsc", "easyeda", "snapeda",
                                                 "jlcparts", "octopart")),
                "models.py reaches a forbidden source: %s" % h)


class TestDatasheetGate(unittest.TestCase):
    """A part whose datasheet cannot be read is a design bug, not a research
    inconvenience: its pinout cannot be verified, and an unverified pinout is
    what turns a correct land pattern into a board that arrives dead (D42)."""

    def test_pdf_magic_is_accepted(self):
        from fluxplace import sourcing as S
        import unittest.mock as mock
        class R:
            headers = {"Content-Type": "application/pdf"}
            def read(self, n): return b"%PDF-1.7 ..."
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch("urllib.request.urlopen", return_value=R()):
            ok, detail = S.datasheet_reachable("https://x/y.pdf")
        self.assertTrue(ok)

    def test_bot_check_html_is_not_reachable(self):
        """A 200 that returns HTML is a bot-check page. Calling that 'reachable'
        is worse than reporting nothing — it tells the engineer the document is
        available when it is not."""
        from fluxplace import sourcing as S
        import unittest.mock as mock
        class R:
            headers = {"Content-Type": "text/html"}
            def read(self, n): return b"<html><script>challenge</script>"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch("urllib.request.urlopen", return_value=R()):
            ok, detail = S.datasheet_reachable("https://x/y.pdf")
        self.assertFalse(ok)
        self.assertIn("bot check", detail)

    def test_no_url_is_reported_as_such(self):
        from fluxplace import sourcing as S
        ok, detail = S.datasheet_reachable(None)
        self.assertFalse(ok)
        self.assertIn("no datasheet URL", detail)

    def test_gate_verdict_names_the_blocked_parts(self):
        from fluxplace import sourcing as S
        import unittest.mock as mock
        with mock.patch.object(S, "datasheet_url", return_value="https://x/y.pdf"), \
             mock.patch.object(S, "datasheet_reachable", return_value=(False, "HTTP 403")), \
             mock.patch.object(S, "credentials", return_value={}):
            r = S.datasheet_gate(["PART-A", "PART-B"], log=lambda *_: None)
        self.assertEqual(r["blocked"], ["PART-A", "PART-B"])
        self.assertIn("cannot be verified", r["verdict"])


class TestFreeSlots(unittest.TestCase):
    """Placement search has to count through-hole parts as blocking BOTH sides.
    A radial cap in the pocket you want is just as in the way from the back as
    from the front, and a scan that misses it hands you an unbuildable board."""

    def test_tht_blocks_both_sides(self):
        import inspect as _i
        from fluxplace import migrate
        src = _i.getsource(migrate.free_slots)
        self.assertIn("PAD_ATTRIB_PTH", src)
        self.assertIn("if not (tht or f.IsFlipped() == back):", src)

    def test_ignore_lets_you_ask_what_if_i_moved_that(self):
        import inspect as _i
        from fluxplace import migrate
        self.assertIn("ignore", _i.signature(migrate.free_slots).parameters)


class TestSesImport(unittest.TestCase):
    """A Specctra session is the COMPLETE routing, not a patch."""

    def test_import_clears_existing_routing_by_default(self):
        import inspect as _i
        from fluxplace import ses
        sig = _i.signature(ses.import_into)
        self.assertIn("replace", sig.parameters)
        self.assertIs(sig.parameters["replace"].default, True)
        src = _i.getsource(ses.import_into)
        self.assertIn("board.Remove(t)", src)

    def test_the_reason_is_recorded_not_just_the_behaviour(self):
        """The failure mode presents as co-located holes, which points nowhere
        near the real cause; the docstring has to say so."""
        from fluxplace import ses
        self.assertIn("co-located", ses.import_into.__doc__)


class TestPowerPlaneDeclaration(unittest.TestCase):
    """KiCad exports every copper layer as (type signal), including solid
    planes. A router reading that is being told it may route on the reference."""

    DSN = """(pcb x
  (structure
    (layer F.Cu
      (type signal)
    )
    (layer In1.Cu
      (type signal)
    )
    (layer In4.Cu
      (type signal)
    )
  )
)"""

    def _tmp(self):
        import tempfile, os
        p = os.path.join(tempfile.mkdtemp(), "b.dsn")
        with open(p, "w") as fh:
            fh.write(self.DSN)
        return p

    def test_declares_only_the_named_layers(self):
        from fluxplace import ses
        p = self._tmp()
        r = ses.declare_power_planes(p, ["In1.Cu", "In4.Cu"])
        self.assertEqual(sorted(r["declared"]), ["In1.Cu", "In4.Cu"])
        out = open(p).read()
        self.assertEqual(out.count("(type power)"), 2)
        # F.Cu must be left alone
        self.assertIn("(layer F.Cu\n      (type signal)", out)

    def test_reports_layers_it_could_not_find(self):
        from fluxplace import ses
        r = ses.declare_power_planes(self._tmp(), ["In1.Cu", "In9.Cu"])
        self.assertEqual(r["declared"], ["In1.Cu"])
        self.assertEqual(r["missed"], ["In9.Cu"])

    def test_reason_is_recorded(self):
        from fluxplace import ses
        self.assertIn("reference plane", ses.declare_power_planes.__doc__)


class TestOutputBudget(unittest.TestCase):
    """Every byte a tool prints lands in the caller's context and stays there.
    Unbounded output is a budget the caller never agreed to spend."""

    def test_short_output_is_untouched(self):
        from fluxplace import mcp_server as M
        t, spill = M.budget("hello", tool="x")
        self.assertEqual(t, "hello")
        self.assertIsNone(spill)

    def test_long_output_is_capped_and_spilled(self):
        import os
        from fluxplace import mcp_server as M
        big = "\n".join("line %d" % i for i in range(4000))
        t, spill = M.budget(big, tool="x", max_chars=2000)
        self.assertLess(len(t), len(big))
        self.assertTrue(spill and os.path.exists(spill))
        with open(spill) as fh:
            self.assertEqual(fh.read(), big, "the spill must be COMPLETE")
        os.unlink(spill)

    def test_truncation_is_announced_never_silent(self):
        """A silently-truncated result is the same failure as a DRC report that
        does not say what it skipped."""
        import os
        from fluxplace import mcp_server as M
        big = "\n".join("line %d" % i for i in range(4000))
        t, spill = M.budget(big, tool="x", max_chars=2000)
        self.assertIn("omitted", t)
        self.assertIn(spill, t)
        os.unlink(spill)

    def test_head_and_tail_both_survive(self):
        """The summary is at the top and the verdict at the bottom; a middle
        cut keeps both."""
        import os
        from fluxplace import mcp_server as M
        lines = ["FIRST"] + ["filler %d" % i for i in range(4000)] + ["VERDICT"]
        t, spill = M.budget("\n".join(lines), tool="x", max_chars=2000)
        self.assertIn("FIRST", t)
        self.assertIn("VERDICT", t)
        os.unlink(spill)

    def test_netlist_summary_is_far_cheaper_than_the_dump(self):
        import inspect as _i
        from fluxplace import audit
        self.assertTrue(hasattr(audit, "netlist_summary"))
        src = _i.getsource(audit.netlist_summary)
        self.assertIn("single_pad_nets", src)
        self.assertIn("largest_nets", src)


class TestManifestScope(unittest.TestCase):
    """The fab MANIFEST ships inside the gerber zip. A PASS in it that does not
    say what was checked is the exact defect an outside reviewer caught."""

    def test_manifest_reads_ignored_checks(self):
        import inspect as _i
        from fluxplace import fab
        src = _i.getsource(fab.emit)
        self.assertIn("ignored_checks", src)
        self.assertIn("NOT evaluated", src)

    def test_clean_scope_is_stated_positively_too(self):
        """'no checks ignored' has to be printed, not merely implied by silence."""
        import inspect as _i
        from fluxplace import fab
        self.assertIn("no checks ignored", _i.getsource(fab.emit))
