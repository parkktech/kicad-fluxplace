"""silk check + --fix over real boards found on this machine, with NO constraints (a bare .kicad_pcb: any layer
count, sides, outline, rotation, no silk text at all). Missing boards are skipped. Every command runs in a subprocess
with a hard timeout so the suite cannot hang. Default: the two smallest boards; FLUXPLACE_SILK_MATRIX=all runs every one.
Needs pcbnew under /usr/bin/python3 (silk_helper.py)."""
import glob, json, os, re, shutil, subprocess, sys
import pytest
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
from fluxplace import silk as SK

CANDIDATES = sorted(set(
    glob.glob("/var/kicad/utv-comms-bridge/hardware/utv-comms/utv-comms-v1[356].kicad_pcb")
    + glob.glob("/var/www/html/razor-cm5-board/hardware/razor-cm5/razor-cm5.kicad_pcb")
    + glob.glob("/var/www/html/razor-detector-board/hardware/*/*.kicad_pcb")
    + sorted(glob.glob("/usr/share/kicad/demos/*/*.kicad_pcb"))[:4]), key=os.path.getsize)
BOARDS = CANDIDATES if os.environ.get("FLUXPLACE_SILK_MATRIX") == "all" else CANDIDATES[:2]
TIMEOUT = 300
HARD_REF = ("SILK_OFF_BOARD", "SILK_OVER_PAD", "SILK_OVER_HOLE", "SILK_OVERLAP", "SILK_UNDER_PART")
SIG = r'''
import sys, json, hashlib, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
h = lambda o: hashlib.sha1(repr(sorted(o)).encode()).hexdigest()
fp = [(f.GetReference(), f.GetPosition().x, f.GetPosition().y, f.GetOrientation().AsDegrees(), f.GetLayerName()) for f in b.GetFootprints()]
pads = [(f.GetReference(), p.GetNumber(), p.GetNetname(), p.GetPosition().x, p.GetPosition().y) for f in b.GetFootprints() for p in f.Pads()]
tr = [(t.GetClass(), t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y, t.GetLayerName(), t.GetNetname(), t.GetWidth()) for t in b.GetTracks()]
zn = [(z.GetNetname(), z.GetLayerName(), z.GetBoundingBox().GetArea()) for z in b.Zones()]
print("SIG" + json.dumps([h(fp), h(pads), h(tr), h(zn)]))
'''


def _have_pcbnew():
    return subprocess.run([SK.PY, "-c", "import pcbnew"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not (BOARDS and _have_pcbnew()), reason="no sample boards / no pcbnew")


def _silk(*args):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "silk", *args], capture_output=True, text=True,
                       timeout=TIMEOUT)
    assert r.returncode in (0, 1), r.stderr[-500:]                  # 0 clean, 1 findings; anything else is a crash
    assert "Traceback" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-800:]
    return r.stdout + r.stderr


def _counts(txt):
    return {c: len(re.findall(r"\b" + c + r"\b", txt)) for c in SK.CODES}


def _sig(p):
    r = subprocess.run([SK.PY, "-c", SIG, p], capture_output=True, text=True, timeout=TIMEOUT)
    return json.loads(r.stdout.split("SIG", 1)[1])


@pytest.mark.parametrize("src", BOARDS, ids=lambda p: os.path.basename(p)[:-10])
def test_check_and_fix_on_real_boards(src, tmp_path):
    b = str(tmp_path / "in.kicad_pcb")
    out = str(tmp_path / "out.kicad_pcb")
    shutil.copy(src, b)
    before = _counts(_silk(b))
    _silk(b, "--fix", "--out", out)
    assert os.path.exists(out)
    after = _counts(_silk(out))
    hard = lambda c: sum(c[k] for k in HARD_REF)
    assert hard(after) <= hard(before)
    assert _sig(b) == _sig(out)                                      # only silk texts moved/hidden: parts, pads, tracks, vias, zones identical
