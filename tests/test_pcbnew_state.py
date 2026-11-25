"""pcbnew in-process state: Remove() of a discarded item leaves every later LoadBoard a raw SwigPyObject; Delete does not.
Each case runs in its own interpreter so a regression cannot poison the rest of the test run."""
import os
import subprocess
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MK = textwrap.dedent('''
    import sys, os
    import pcbnew
    b = pcbnew.CreateEmptyBoard()
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_POLY); s.SetLayer(pcbnew.B_Mask)
    s.SetPolyPoints([pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)) for x, y in ((10, 10), (30, 10), (30, 30), (10, 30))])
    b.Add(s)
    pcbnew.SaveBoard(os.path.join(sys.argv[1], "b.kicad_pcb"), b)
''')
# the board file is written by another interpreter: a process that CREATED a board does not show the decay
PRE = textwrap.dedent('''
    import sys, os, tomllib
    sys.path.insert(0, %r)
    import pcbnew
    from fluxplace import thermal, pipeline
    d = sys.argv[1]
    src = os.path.join(d, "b.kicad_pcb")
    cons = tomllib.loads('[thermal.t1]\\nrect = [5, 5, 40, 40]\\nside = "B"\\nnet = "GND"\\nmask_open = true\\n[thermal.t1.via]\\ndiameter = 0.46\\ndrill = 0.2\\npitch = 2.0\\n')
    kind = lambda p: type(pcbnew.LoadBoard(p)).__name__
''' % ROOT)


def _run(tmp_path, body):
    subprocess.run(["/usr/bin/python3", "-c", MK, str(tmp_path)], capture_output=True, text=True, timeout=120, check=True)
    r = subprocess.run(["/usr/bin/python3", "-c", PRE + textwrap.dedent(body), str(tmp_path)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout.strip()


def test_strip_covered_then_loadboard_is_a_board(tmp_path):
    out = _run(tmp_path, '''
        out = os.path.join(d, "o.kicad_pcb")
        res = thermal.strip_covered(src, cons, out)
        assert res["stripped"] == 1, res
        print(kind(out), kind(src))
    ''')
    assert out == "BOARD BOARD"


def test_recut_then_loadboard_is_a_board(tmp_path):
    out = _run(tmp_path, '''
        res = thermal.recut(src, cons, os.path.join(d, "o.kicad_pcb"))
        assert res["blocks"] == 1, res
        print(kind(src), kind(os.path.join(d, "o.kicad_pcb")))
    ''')
    assert out == "BOARD BOARD"


def test_guard_fails_a_stage_that_breaks_the_process_and_names_it(tmp_path):
    out = _run(tmp_path, '''
        ok = pipeline._pcbnew_state_guard("fanout", {"verdict": "pass", "board": src, "line": "x"})
        assert ok["verdict"] == "pass"
        b = pcbnew.LoadBoard(src)
        def fake_stage(b):                                   # discards an item with Remove(), like the old strip_covered
            for x in [x for x in b.GetDrawings()]:
                b.Remove(x)
        fake_stage(b)
        pcbnew.SaveBoard(os.path.join(d, "bad.kicad_pcb"), b)
        r = pipeline._pcbnew_state_guard("fake-stage", {"verdict": "pass", "board": src, "line": "x"})
        print(r["verdict"], "|", r["line"])
    ''')
    assert out.startswith("fail |") and "after fake-stage" in out
