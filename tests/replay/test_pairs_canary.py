"""Real-board canary (slow tier): the pairs stage alone on the stored pour-tie board of the W16 board (route-7 input).
Guards the batch-10 regression class (route-8: complete pairs 8/21 against 13/21, pairs left unsearched by the time budget).
Runtime: route-7 pairs stage 767 s alone; 1800 s was exceeded under load 20+ (w92 first run), so the cap is 3600 s; the run is capped at CANARY_TIMEOUT s."""
import json, os, re, shutil
from fluxplace.pairs import laid_count
import pytest
from . import replaylib as R

pytestmark = pytest.mark.replay
CANARY_TIMEOUT = 3600
MIN_LAID = 13              # D71ft: LAID pairs (complete + laid over target skew) >= 13/21; complete-within-target is reported, not gated
UNSEARCHED = "time budget reached before this pair was searched"


@pytest.fixture(scope="module")
def canary(tmp_path_factory):
    if not R.ROUTER_OK:
        pytest.skip("router (KiCadRoutingTools venv / freerouting jar) not installed")
    import lzma
    proj = str(tmp_path_factory.mktemp("canary")); d = os.path.join(R.DATA, "canary")
    with lzma.open(os.path.join(d, "board.kicad_pcb.xz")) as s, open(os.path.join(proj, "board.kicad_pcb"), "wb") as o:
        shutil.copyfileobj(s, o)
    for f in ("board.kicad_pro", "board.fluxmap.json"):
        shutil.copy(os.path.join(d, f), proj)
    shutil.copytree(os.path.join(R.DATA, "docs"), os.path.join(proj, "docs"))
    cons = os.path.join(proj, "cons"); os.makedirs(cons)
    shutil.copy(os.path.join(d, "constraints-route.toml"), os.path.join(cons, "constraints.toml"))
    shutil.copy(os.path.join(d, "spec.json"), cons)
    res = R.pipeline(proj, "board.kicad_pcb", cons, "pairs", "pairs", CANARY_TIMEOUT, extra=("--route-budget", "7200"))
    res["proj"] = proj
    text = res["out"]
    for root, _, files in os.walk(os.path.join(proj, "run")):
        for f in files:
            if f in ("stage.log", "board.pairs.json"):
                text += open(os.path.join(root, f), errors="replace").read()
    res["text"] = text
    m = re.findall(r"pairs: \w+ - (\d+)/(\d+) complete", text)
    res["complete"], res["total"] = (int(m[-1][0]), int(m[-1][1])) if m else (None, None)
    res["laid"] = None
    for root, _, files in os.walk(os.path.join(proj, "run")):
        if "board.pairs.json" in files:
            res["laid"] = laid_count(json.load(open(os.path.join(root, "board.pairs.json"))))
    print(f"CANARY pairs: {res['complete']}/{res['total']} complete, {res['laid']} laid, rc {res['rc']}, {res['secs']} s")
    return res


def test_laid_pairs_are_not_fewer_than_route_9(canary):
    assert canary["laid"] is not None, canary["log"]
    assert canary["laid"] >= MIN_LAID, (canary["laid"], canary["complete"], canary["total"], canary["log"])


def test_laid_count_includes_over_skew_pairs():
    rep = {"pairs": [{"status": s} for s in ("complete", "mismatch", "mismatch", "open", "not-laid", "unknown")]}
    assert laid_count(rep) == 3


def test_no_pair_is_left_unsearched_by_the_time_budget(canary):
    assert UNSEARCHED not in canary["text"], canary["log"]
