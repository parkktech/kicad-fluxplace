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
