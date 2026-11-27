"""The one via/track-forbidden region model (ticket B7).

Regions: rule-area keep-outs (read from the board by routediag / escapeplan / fanout_helper) and solder-mask openings of the OTHER side's
copper (a foreign net's copper under an opening is exposed -> solder_mask_bridge). A `[thermal.*] mask_open` window is taken off the working
board before the first copper stage (thermal.strip_covered; recut makes it again after the copper: the settled D71cd order), so its polygon
is not on the board any more. Its region still is, and is built here from the block: `windows(cons)` -> board-agnostic dicts the stages
merge into what they already read (`augment(B, windows(cons))` for the routediag board model B, `Obstacles`/fanout_helper through `windows`).
Same rule as routediag.mask_openings: only the owner net's copper may lie in the window, every other via/track is forbidden there.
placegaps reads it as the `via-forbidden` gap kind: a part whose pads, in the window, need a via of a foreign net."""
from . import geom as GM


def windows(cons):
    """-> [{name, side "F"|"B", pts, owner}] of the [thermal.*] blocks with mask_open (empty when none)."""
    from . import thermal as TH
    out = []
    for sp in TH.blocks(cons, pre=False):
        if sp["mask"]:
            out.append({"name": f"mask window {sp['name']}", "side": sp["side"], "pts": [tuple(p) for p in sp["pts"]], "owner": sp["net"]})
    return out


def _layer(copper, side):
    return copper[0] if side == "F" else copper[-1]


def _bb(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _same(a, b):
    """two regions on the same layer whose boxes agree to 0.5 mm are the same window (the board still carries the polygon)."""
    return a["layers"] == b["layers"] and all(abs(p - q) < 0.5 for p, q in zip(_bb(a["pts"]), _bb(b["pts"])))


def augment(B, wins):
    """Add the stripped windows (`windows(cons)`) to B["mask_open"] (routediag format) unless the board still carries them. -> B."""
    for w in wins or ():
        if not B.get("copper"):
            break
        r = {"name": w["name"], "layers": {_layer(B["copper"], w["side"])}, "pts": w["pts"], "owner": w["owner"], "mask": w["side"] + ".Mask"}
        if not any(_same(r, m) for m in B.get("mask_open", [])):
            B.setdefault("mask_open", []).append(r)
    return B


def blocked(B, x, y, net, layer=None, via=True, r=0.0):
    """Name of the mask window a via (via=True) or track point on `layer` of `net` would stand in, else None (owner net exempt)."""
    for z in B.get("mask_open", []):
        if (net and net == z["owner"]) or len(z["pts"]) < 3 or not (via or layer in z["layers"]):
            continue
        if GM.poly_near(z["pts"], x, y, r + GM.MASK_MARGIN):
            return z["name"]
    return None


def via_pins(B, ref):
    """Pads of part `ref` that need a via inside a forbidden mask window: a foreign-net pad of a part with >= 3 pads (a two-pad part
    escapes by a track on its own side; a via is only forced on parts that need a fan-out) centred in the window. -> [pad]."""
    p = B["parts"].get(ref)
    if not p or p["n_pads"] < 3:
        return []
    return [q for q in p["pads"] if q["net"] and not q.get("np") and blocked(B, q["x"], q["y"], q["net"], r=0.0)]
