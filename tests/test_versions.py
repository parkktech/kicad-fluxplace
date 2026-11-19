"""fluxplace versions: the board-version registry (tmp git repos, synthetic empty boards, DRC measure faked)."""
import json, os, shutil, subprocess, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import versions as VS

REAL = "/var/kicad/utv-comms-bridge/docs/resume/boards.json"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("FLUXPLACE_VERSIONS", raising=False)


@pytest.fixture
def fake(monkeypatch):
    """basename -> metric overrides; every other board measures 2 opens, 0 shorts, 5 errors."""
    table = {}

    def measure(board):
        m = {"unconnected": 2, "shorts": 0, "errors": 5, "track_opens": 0, "zone_islands": 0}
        m.update(table.get(os.path.basename(board), {}))
        return dict(m)
    monkeypatch.setattr(VS, "measure", measure)
    monkeypatch.setattr(VS, "copper_fingerprint", lambda p: os.path.basename(p))
    return table


def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    return path


def board(root, rel, txt="(kicad_pcb)"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt)
    return str(p)


def call(root, *argv):
    return VS.main([*argv, "--root", str(root)])


def init(root, v="W1", **extra):
    b = board(root, "%s/start.kicad_pcb" % v)
    call(root, "init", "--version", v, "--board", b, "--title", "first", "--decisions", "D1,D2")
    return b


def reg(root):
    return json.load(open(root / "docs/resume/boards.json"))


def set_config(root, **cfg):
    r = reg(root)
    r.setdefault("config", {}).update(cfg)
    json.dump(r, open(root / "docs/resume/boards.json", "w"), indent=1)


def commits(root):
    return subprocess.run(["git", "-C", str(root), "log", "--format=%B"], capture_output=True, text=True).stdout


def test_init_creates_registry_resume_links_and_commit(tmp_path, fake):
    root = git_repo(tmp_path)
    init(root)
    r = reg(root)
    assert r["active"] == "W1" and r["versions"]["W1"]["decisions"] == ["D1", "D2"]
    assert r["config"]["folder_pattern"] == "W{n}" and r["config"]["links"] == {} and r["config"]["commit"] is True
    assert os.path.realpath(root / "W1/CURRENT.kicad_pcb") == os.path.realpath(root / "W1/start.kicad_pcb")
    t = open(root / "docs/resume/RESUME-W1.md").read()
    assert "<!-- now -->" in t and "session 1" in t and "Board version: **W1**" in t
    assert "init registry" in commits(root) and "Co-Authored-By" not in commits(root)
    with pytest.raises(SystemExit):
        init(root)                                       # registry exists: refused


def test_init_without_git_skips_commit(tmp_path, fake):
    init(tmp_path)
    assert (tmp_path / "docs/resume/boards.json").exists()


def test_status_no_warnings(tmp_path, fake, capsys):
    init(tmp_path)
    capsys.readouterr()
    call(tmp_path, "status")
    out = capsys.readouterr().out
    assert "ACTIVE BOARD VERSION: W1" in out and "WARNING" not in out


def test_accept_refused_on_rising_opens_then_allow_worse(tmp_path, fake):
    root = git_repo(tmp_path)
    init(root)
    b2 = board(root, "W1/stageB/a.kicad_pcb")
    fake["a.kicad_pcb"] = {"unconnected": 3}
    with pytest.raises(SystemExit) as e:
        call(root, "accept", b2, "--note", "worse")
    assert "unconnected rose" in str(e.value)
    assert os.path.realpath(root / "W1/CURRENT.kicad_pcb") == os.path.realpath(root / "W1/start.kicad_pcb")
    call(root, "accept", b2, "--note", "accepted anyway", "--allow-worse", "--next", "route it")
    assert os.path.realpath(root / "W1/CURRENT.kicad_pcb") == os.path.realpath(b2)
    r = reg(root)
    assert r["versions"]["W1"]["current"] == "W1/stageB/a.kicad_pcb"
    assert r["versions"]["W1"]["history"][-1]["metrics"]["unconnected"] == 3
    t = open(root / "docs/resume/RESUME-W1.md").read()
    assert "Next step: route it" in t and "accepted anyway" in t
    assert "accept a.kicad_pcb" in commits(root)


def test_accept_better_board_moves_current(tmp_path, fake):
    root = git_repo(tmp_path)
    init(root)
    b2 = board(root, "W1/stageB/good.kicad_pcb")
    fake["good.kicad_pcb"] = {"unconnected": 1}
    call(root, "accept", b2, "--note", "better")
    assert os.path.realpath(root / "W1/CURRENT.kicad_pcb") == os.path.realpath(b2)


def test_reject_recorded_and_not_warned_as_newer(tmp_path, fake, capsys):
    root = git_repo(tmp_path)
    init(root)
    b3 = board(root, "W1/stageB/bad.kicad_pcb")
    t = os.path.getmtime(root / "W1/start.kicad_pcb") + 120
    os.utime(b3, (t, t))
    capsys.readouterr()
    call(root, "status")
    assert "newer than CURRENT" in capsys.readouterr().out
    call(root, "reject", b3, "--note", "drc-worse")
    assert reg(root)["versions"]["W1"]["rejected"][0]["note"] == "drc-worse"
    call(root, "status")
    assert "newer than CURRENT" not in capsys.readouterr().out


def test_step_rewrites_only_the_step_line(tmp_path, fake):
    root = git_repo(tmp_path)
    init(root)
    before = open(root / "docs/resume/RESUME-W1.md").read().splitlines()
    call(root, "step", "rip MIC_TAP_N1")
    after = open(root / "docs/resume/RESUME-W1.md").read().splitlines()
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    body = [(a, b) for a, b in changed if "## NOW (" not in a]                    # header carries the update time
    assert len(body) == 1 and body[0][1].startswith("- Step in progress") and "rip MIC_TAP_N1" in body[0][1]


def test_new_version_links_closes_old_and_switches(tmp_path, fake):
    root = git_repo(tmp_path)
    init(root)
    set_config(root, links={"lib": "../hardware/lib"})
    src = board(root, "W1/stageB/final.kicad_pcb")
    call(root, "new", "W2", "--from", src, "--reason", "rework", "--decisions", "D71ca")
    assert (root / "W2/start.kicad_pcb").exists()
    assert os.readlink(root / "W2/lib") == "../hardware/lib"
    r = reg(root)
    assert r["active"] == "W2" and r["versions"]["W1"]["status"] == "superseded"
    assert r["versions"]["W2"]["decisions"] == ["D71ca"] and r["versions"]["W2"]["parent"] == "W1"
    old = open(root / "docs/resume/RESUME-W1.md").read()
    assert "CLOSED" in old and "<!-- was-now -->" in old and "RESUME-W2.md" in old
    new = open(root / "docs/resume/RESUME-W2.md").read()
    assert "Board version: **W2**" in new and "Parent: W1" in new
    assert os.path.realpath(root / "W2/CURRENT.kicad_pcb") == os.path.realpath(root / "W2/start.kicad_pcb")


def test_new_refuses_folder_pattern_mismatch_and_duplicates(tmp_path, fake):
    init(tmp_path)
    src = board(tmp_path, "W1/stageB/x.kicad_pcb")
    with pytest.raises(SystemExit):
        call(tmp_path, "new", "V2", "--from", src, "--reason", "r")
    call(tmp_path, "new", "W2", "--from", src, "--reason", "r")
    with pytest.raises(SystemExit):
        call(tmp_path, "new", "W2", "--from", src, "--reason", "r")


def test_unregistered_folder_warning(tmp_path, fake, capsys):
    init(tmp_path)
    (tmp_path / "W9").mkdir()
    (tmp_path / "scratch").mkdir()
    capsys.readouterr()
    call(tmp_path, "status")
    out = capsys.readouterr().out
    assert "W9 exists but is not in the registry" in out and "scratch" not in out


def test_start_context_bounded_json_and_plain(tmp_path, fake, capsys):
    root = git_repo(tmp_path)
    init(root)
    call(root, "step", "x" * 3000)
    capsys.readouterr()
    call(root, "start-context", "--hook-json")
    doc = json.loads(capsys.readouterr().out)
    h = doc["hookSpecificOutput"]
    assert h["hookEventName"] == "SessionStart"
    assert len(h["additionalContext"]) < 2000
    assert "NOW" in h["additionalContext"] and "fluxplace versions" in h["additionalContext"]
    call(root, "start-context")
    assert len(capsys.readouterr().out) < 2000


def test_start_context_never_raises(tmp_path, capsys):
    call(tmp_path, "start-context", "--hook-json")                     # no registry
    doc = json.loads(capsys.readouterr().out)
    assert "WARNING" in doc["hookSpecificOutput"]["additionalContext"]
    (tmp_path / "docs/resume").mkdir(parents=True)
    (tmp_path / "docs/resume/boards.json").write_text("not json")
    call(tmp_path, "start-context")                                    # corrupt registry: one-line warning, no raise
    assert "WARNING: start-context" in capsys.readouterr().out


def test_custom_folder_pattern_rev(tmp_path, fake, capsys):
    init(tmp_path, v="rev1")
    set_config(tmp_path, folder_pattern="rev{n}")
    src = board(tmp_path, "rev1/stageB/x.kicad_pcb")
    with pytest.raises(SystemExit):
        call(tmp_path, "new", "W2", "--from", src, "--reason", "r")
    call(tmp_path, "new", "rev2", "--from", src, "--reason", "r")
    assert (tmp_path / "rev2/start.kicad_pcb").exists() and reg(tmp_path)["active"] == "rev2"
    (tmp_path / "rev7").mkdir()
    (tmp_path / "W5").mkdir()
    capsys.readouterr()
    call(tmp_path, "status")
    out = capsys.readouterr().out
    assert "rev7 exists but is not in the registry" in out and "W5" not in out


def test_env_registry_path_relative_to_root(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_VERSIONS", "meta/reg.json")
    init(tmp_path)
    assert (tmp_path / "meta/reg.json").exists() and (tmp_path / "meta/RESUME-W1.md").exists()


def test_real_registry_format_reads_with_boards_missing(tmp_path, capsys):
    if not os.path.exists(REAL):
        pytest.skip("real utv-comms-bridge registry not present")
    (tmp_path / "docs/resume").mkdir(parents=True)
    shutil.copy(REAL, tmp_path / "docs/resume/boards.json")
    before = open(REAL, "rb").read()
    capsys.readouterr()
    call(tmp_path, "status")
    out = capsys.readouterr().out
    assert "ACTIVE BOARD VERSION: %s" % json.load(open(REAL))["active"] in out   # live project state: never hardcode
    assert "WARNING: CURRENT board missing on disk" in out
    assert "WARNING: no resume file for the active version" in out
    call(tmp_path, "start-context", "--hook-json")
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert open(REAL, "rb").read() == before


def test_commit_does_not_sweep_in_other_staged_files(tmp_path):
    import subprocess
    from fluxplace import versions as V
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    res = tmp_path / "docs" / "resume"
    res.mkdir(parents=True)
    (res / "boards.json").write_text("{}")
    (tmp_path / "other.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "other.txt"], check=True)
    V.ROOT, V.REG, V.RES = str(tmp_path), str(res / "boards.json"), str(res)
    V.commit("registry only", {})
    shown = subprocess.run(["git", "-C", str(tmp_path), "show", "--name-only", "--format="], capture_output=True, text=True).stdout
    assert "docs/resume/boards.json" in shown and "other.txt" not in shown
    staged = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], capture_output=True, text=True).stdout
    assert "other.txt" in staged


def test_accept_identical_copper_exit4_changes_nothing(tmp_path, fake, monkeypatch):
    root = git_repo(tmp_path)
    init(root)
    b2 = board(root, "W1/stageB/copy.kicad_pcb")
    monkeypatch.setattr(VS, "copper_fingerprint", lambda p: "same-copper")
    before = (reg(root), commits(root), open(root / "docs/resume/RESUME-W1.md").read(),
              os.path.realpath(root / "W1/CURRENT.kicad_pcb"))
    with pytest.raises(SystemExit) as e:
        call(root, "accept", b2, "--note", "duplicate")
    assert e.value.code == 4
    after = (reg(root), commits(root), open(root / "docs/resume/RESUME-W1.md").read(),
             os.path.realpath(root / "W1/CURRENT.kicad_pcb"))
    assert after == before


def test_accept_changed_copper_still_accepts(tmp_path, fake, monkeypatch):
    root = git_repo(tmp_path)
    init(root)
    b2 = board(root, "W1/stageB/moved.kicad_pcb")
    monkeypatch.setattr(VS, "copper_fingerprint", lambda p: "copper-" + os.path.basename(p))
    call(root, "accept", b2, "--note", "moved")
    assert os.path.realpath(root / "W1/CURRENT.kicad_pcb") == os.path.realpath(b2)
    assert len(reg(root)["versions"]["W1"]["history"]) == 2


def test_copper_fingerprint_follows_copper_only(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")

    def make(name, x2):
        b = pcbnew.BOARD()
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(0, 0))
        t.SetEnd(pcbnew.VECTOR2I(x2, 0))
        t.SetWidth(200000)
        t.SetLayer(pcbnew.F_Cu)
        b.Add(t)
        p = str(tmp_path / name)
        b.Save(p)
        return p
    a, same, moved = make("a.kicad_pcb", 1000000), make("same.kicad_pcb", 1000000), make("moved.kicad_pcb", 2000000)
    assert VS.copper_fingerprint(a) == VS.copper_fingerprint(same)
    assert VS.copper_fingerprint(a) != VS.copper_fingerprint(moved)


def test_copper_fingerprint_follows_zone_outline(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")

    def make(name, corner_x):
        b = pcbnew.BOARD()
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(0, 0))
        t.SetEnd(pcbnew.VECTOR2I(1000000, 0))
        t.SetWidth(200000)
        t.SetLayer(pcbnew.F_Cu)
        b.Add(t)
        z = pcbnew.ZONE(b)
        z.SetLayer(pcbnew.F_Cu)
        o = pcbnew.SHAPE_POLY_SET()
        o.NewOutline()
        for x, y in [(0, 5000000), (corner_x, 5000000), (corner_x, 9000000)]:
            o.Append(x, y)
        z.SetOutline(o)
        b.Add(z)
        p = str(tmp_path / name)
        b.Save(p)
        return p
    a = make("zone-a.kicad_pcb", 4000000)
    moved = make("zone-moved.kicad_pcb", 4500000)
    assert VS.copper_fingerprint(a) != VS.copper_fingerprint(moved)
    assert VS.copper_fingerprint(a) == VS.copper_fingerprint(make("zone-same.kicad_pcb", 4000000))


def test_copper_fingerprint_follows_mask_drawing(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    from fluxplace import versions as V

    def board(path, with_mask):
        b = pcbnew.BOARD()
        t = pcbnew.PCB_TRACK(b); t.SetStart(pcbnew.VECTOR2I(0, 0)); t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(5), 0))
        t.SetLayer(pcbnew.F_Cu); b.Add(t)
        if with_mask:
            s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_RECT); s.SetLayer(pcbnew.B_Mask); s.SetFilled(True)
            s.SetStart(pcbnew.VECTOR2I(0, 0)); s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(3), pcbnew.FromMM(3))); b.Add(s)
        b.Save(str(path))
    board(tmp_path / "a.kicad_pcb", False)
    board(tmp_path / "b.kicad_pcb", True)
    board(tmp_path / "c.kicad_pcb", False)
    a, bb, c = (V.copper_fingerprint(str(tmp_path / n)) for n in ("a.kicad_pcb", "b.kicad_pcb", "c.kicad_pcb"))
    assert a == c and a != bb
