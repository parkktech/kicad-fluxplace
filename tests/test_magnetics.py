"""Magnetics wiring check on synthetic netlists (no board file needed)."""
import copy
import unittest

from fluxplace import magnetics as MG, review as R

CH = [dict(chip_p=str(3 * n + 2), chip_n=str(3 * n + 3), chip_ct=str(3 * n + 1),
           line_p=str(23 - 3 * n), line_n=str(22 - 3 * n), line_ct=str(24 - 3 * n)) for n in range(4)]
SPEC = {"components": [{"ref": "T3", "value": "HX5084NLT", "fp": "Pulse_H5084_SMD24",
                        "pinmap_source": "x.pdf#p2", "channels": CH}]}


def board(line=None, chip=None, term=None):
    """T3 + RJ45 J12 + R44..R47 + C60. line: {pad: net} overrides."""
    pads = {}
    for n in range(4):
        pads[str(3 * n + 1)] = "CT"
        pads[str(3 * n + 2)] = f"P{n}_P"
        pads[str(3 * n + 3)] = f"P{n}_N"
        pads[str(24 - 3 * n)] = f"CX{n}"
        pads[str(23 - 3 * n)] = f"L{n}_P"
        pads[str(22 - 3 * n)] = f"L{n}_N"
    pads.update(line or {})
    parts = {"T3": {"pads": pads, "footprint": "Pulse_H5084_SMD24"},
             "J12": {"pads": {str(i + 1): f"L{i // 2}_{'PN'[i % 2]}" for i in range(8)},
                     "footprint": "RJ45_Jack"},
             "C59": {"pads": {"1": "CT", "2": "GND"}, "footprint": "C_0402"}}
    for n in range(4):
        parts[f"R{44 + n}"] = {"pads": {"1": f"CX{n}", "2": "BOB"}, "footprint": "R_0402"}
    net_pads = {}
    for ref, p in parts.items():
        for num, net in p["pads"].items():
            net_pads.setdefault(net, []).append((ref, num))
    return parts, net_pads


def run(parts, net_pads, spec=SPEC, cons=None):
    return MG.check(parts, net_pads, spec, cons)


def codes(f, level=None):
    return sorted(x["code"] for x in f if level is None or x["level"] == level)


def renet(parts, over):
    parts = copy.deepcopy(parts)
    parts["T3"]["pads"].update(over)
    net_pads = {}
    for ref, p in parts.items():
        for num, net in p["pads"].items():
            net_pads.setdefault(net, []).append((ref, num))
    return parts, net_pads


class T(unittest.TestCase):
    def test_correct(self):
        self.assertEqual(run(*board()), [])

    def test_line_on_ct_and_split(self):
        # the real bug: channel 1 cable side in the old module's order
        p, n = renet(board()[0], {"24": "L0_P", "23": "L0_N", "22": "CX0"})
        f = run(p, n)
        self.assertEqual(codes(f).count("MAG_LINE_ON_CT"), 2)
        self.assertIn("MAG_CHANNEL_SPLIT", codes(f))
        self.assertTrue(any("T3.24" in x["msg"] for x in f))
        self.assertTrue(any("T3.22" in x["msg"] for x in f))

    def test_split_channel(self):
        # channel 2 line pads carry pair 1's + and pair 2's -
        f = run(*renet(board()[0], {"20": "L1_P", "19": "L2_N", "17": "L1_N", "16": "L2_P"}))
        self.assertEqual(codes(f, "FAIL").count("MAG_CHANNEL_SPLIT"), 2)

    def test_polarity_swap_warns_or_fails(self):
        p, n = renet(board()[0], {"23": "L0_N", "22": "L0_P"})
        f = run(p, n)
        self.assertEqual(codes(f), ["MAG_POLARITY"])
        self.assertEqual(f[0]["level"], "WARN")
        f = run(p, n, cons={"magnetics": {"polarity": "strict"}})
        self.assertEqual(f[0]["level"], "FAIL")

    def test_bad_chip_ct(self):
        p, n = renet(board()[0], {"4": "CT2"})
        f = run(p, n)
        self.assertEqual(codes(f), ["MAG_CT_WIRING"])
        self.assertEqual(run(p, n, cons={"magnetics": {"chip_ct": "any"}}), [])

    def test_bad_line_ct(self):
        p, n = board()
        p["R45"]["pads"]["2"] = "OTHER"                      # not the shared termination net
        n = {}
        for ref, pp in p.items():
            for num, net in pp["pads"].items():
                n.setdefault(net, []).append((ref, num))
        f = run(p, n)
        self.assertEqual(codes(f), ["MAG_CT_WIRING"])
        # two channels sharing a CT net directly, no resistor of their own
        p2, n2 = renet(board()[0], {"21": "CX0"})
        self.assertIn("MAG_CT_WIRING", codes(run(p2, n2)))

    def test_no_channels_warns(self):
        spec = copy.deepcopy(SPEC)
        del spec["components"][0]["channels"]
        f = run(*board(), spec=spec)
        self.assertEqual(codes(f), ["MAG_NO_CHANNELS"])
        self.assertEqual(f[0]["level"], "WARN")
        spec["components"][0]["value"] = "100nF"
        spec["components"][0]["fp"] = "C_0402"
        parts, n = board()
        parts["T3"]["footprint"] = "C_0402"
        self.assertEqual(run(parts, n, spec=spec), [])

    def test_unresolved_and_unsourced(self):
        spec = copy.deepcopy(SPEC)
        spec["components"][0]["channels"][0]["line_p"] = "99"
        del spec["components"][0]["pinmap_source"]
        f = run(*board(), spec=spec)
        self.assertEqual(sorted(set(codes(f))), ["MAG_CHANNELS_UNSOURCED", "MAG_PIN_UNRESOLVED"])

    def test_review_hook(self):
        p, n = board()
        facts = {"parts": p, "net_pads": n}
        self.assertEqual(R.check_magnetics(facts, SPEC), [])
        p2, n2 = renet(p, {"24": "L0_P", "23": "L0_N", "22": "CX0"})
        self.assertTrue(R.check_magnetics({"parts": p2, "net_pads": n2}, SPEC, None))


def _pcb(parts, net_pads):
    nets = sorted(net_pads)
    idx = {n: i + 1 for i, n in enumerate(nets)}
    out = ['(kicad_pcb (version 20240108) (generator "pcbnew")', ' (general (thickness 1.6))',
           ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))', ' (net 0 "")']
    out += [f' (net {i} "{n}")' for n, i in idx.items()]
    for k, (ref, p) in enumerate(parts.items()):
        out.append(f' (footprint "x:{p["footprint"]}" (layer "F.Cu") (at {10 + 12 * k} 10)')
        out.append(f'  (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))')
        for j, (num, net) in enumerate(p["pads"].items()):
            out.append(f'  (pad "{num}" smd rect (at {j} 0) (size 0.5 0.5) (layers "F.Cu") (net {idx[net]} "{net}"))')
        out.append(' )')
    out.append(')')
    return "\n".join(out)


class CLI(unittest.TestCase):
    def _run(self, parts, n, tmp):
        import json, os, subprocess, sys
        b, s = os.path.join(tmp, "b.kicad_pcb"), os.path.join(tmp, "s.json")
        open(b, "w").write(_pcb(parts, n))
        json.dump(SPEC, open(s, "w"))
        root = os.path.join(os.path.dirname(__file__), "..")
        return subprocess.run([sys.executable, os.path.join(root, "cli.py"), "magnetics", b, "--spec", s],
                              capture_output=True, text=True)

    def test_cli_pass_and_fail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ok = self._run(*board(), tmp)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            p, n = renet(board()[0], {"24": "L0_P", "23": "L0_N", "22": "CX0"})
            bad = self._run(p, n, tmp)
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            self.assertIn("MAG_LINE_ON_CT", bad.stdout)


if __name__ == "__main__":
    unittest.main()
