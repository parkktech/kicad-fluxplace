"""Post-route clean-up, run before the close loop (and runnable alone): off-centre track ends.

`track_not_centered_on_via` (an error KiCad's DRC reports for every track end that touches a via off its centre; KRT leaves hundreds):
snapiter.run moves the ends to the via centre where the DRC stays clean (new-violation guard per iteration; refused joints are
reported).  Dangling vias / stubs are NOT pruned here: on the W15 KRT preview the 130 `via_dangling` are locked fan-out vias, and removing
them (alone or with their stubs) raised the unconnected count 65 -> 78 and moved the warning to the stubs (`track_dangling` 57 -> 148);
a safe removal needs a per-candidate connectivity check (open item)."""
import os
import shutil


def _unc(board, cli):
    from . import tune as TU
    return TU.drc_counts(board, cli)[1]


def clean(board, out, kicad_cli="kicad-cli"):
    """-> {snap: {moved, excluded, iters, centred_left, refused_geom}, unconnected: [before, after], kept: bool}; `out` is always written
    (the input when the snap is not kept: unconnected must not rise)."""
    from . import snapiter as SI
    rep = {"snap": None, "unconnected": None, "kept": True}
    for ext in (".kicad_pro", ".kicad_dru"):
        s = os.path.splitext(board)[0] + ext
        if os.path.exists(s) and os.path.abspath(s) != os.path.abspath(os.path.splitext(out)[0] + ext):
            shutil.copy(s, os.path.splitext(out)[0] + ext)
    before = _unc(board, kicad_cli)
    shutil.copy(board, out)
    try:
        rep["snap"] = SI.run(board, out, log=lambda m: None)
    except Exception as e:                           # noqa: BLE001 - a clean-up failure is reported, never fatal
        rep["snap"] = {"error": str(e)[:160]}
    after = _unc(out, kicad_cli)
    if after > before:
        shutil.copy(board, out)
        rep["kept"], after = False, before
    rep["unconnected"] = [before, after]
    return rep
