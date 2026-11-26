"""procs.atomic_copy: a reader of the destination never sees a truncated file; writers never share a temp slot."""
import os, sys, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import procs, gaps


def test_parallel_writers_and_readers_never_see_a_partial_board(tmp_path):
    dst = str(tmp_path / "board.kicad_pcb")
    bodies = []
    for i in range(4):
        src = tmp_path / f"src{i}.kicad_pcb"
        body = ("(kicad_pcb %d\n" % i) + ("(segment x)\n" * 60000) + ")\n"
        src.write_text(body)
        bodies.append(body)
    procs.atomic_copy(str(tmp_path / "src0.kicad_pcb"), dst)
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            t = open(dst).read()
            if t not in bodies:
                bad.append(len(t))

    def writer(i):
        for _ in range(25):
            procs.atomic_copy(str(tmp_path / f"src{i}.kicad_pcb"), dst)

    rs = [threading.Thread(target=reader) for _ in range(3)]
    ws = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    [t.start() for t in rs + ws]
    [t.join() for t in ws]
    stop.set()
    [t.join() for t in rs]
    assert not bad
    assert sorted(os.listdir(tmp_path)) == ["board.kicad_pcb"] + [f"src{i}.kicad_pcb" for i in range(4)]   # no .part left


def test_gaps_copy_has_no_shared_tmp_name(tmp_path):
    """Regression: gaps._copy used dst + ".tmp", one slot for every concurrent writer of dst."""
    src = tmp_path / "a"
    src.write_text("x" * 1000)
    seen = []
    orig = os.replace
    import fluxplace.procs as P
    P.os.replace = lambda a, b: (seen.append(a), orig(a, b))[1]
    try:
        gaps._copy(str(src), str(tmp_path / "dst"))
        gaps._copy(str(src), str(tmp_path / "dst"))
    finally:
        P.os.replace = orig
    assert len(set(seen)) == 2 and all(not s.endswith("dst.tmp") for s in seen)
