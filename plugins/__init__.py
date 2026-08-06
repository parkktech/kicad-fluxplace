"""fluxplace KiCad Action Plugin — registers 'Signal-flow placement' in the PCB editor
(Tools -> External Plugins). Reads the open board, re-places by communication topology,
shrink-wraps the outline, and refreshes. The heavy lifting lives in the pcbnew-free core."""
import os
import sys

# make the core importable whether installed via PCM (plugins/ + fluxplace/ siblings)
# or run from a cloned repo (repo-root on the plugin path)
_here = os.path.dirname(os.path.abspath(__file__))
for cand in (_here, os.path.dirname(_here)):
    if cand not in sys.path:
        sys.path.insert(0, cand)

import pcbnew

try:
    from fluxplace import graph as G, topology as T, placement as P, kicad_io as IO
    _IMPORT_ERR = None
except Exception as e:            # surface import problems in the dialog, don't crash KiCad
    _IMPORT_ERR = str(e)


class FluxPlaceAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "fluxplace — signal-flow placement"
        self.category = "Placement"
        self.description = ("Route-aware placement: quadratic global solve (hub central), "
                            "constructive route-as-you-place builder, global-router gate.")
        self.show_toolbar_button = True
        icon = os.path.join(_here, "icon.png")
        if os.path.exists(icon):
            self.icon_file_name = icon

    def Run(self):
        import wx
        if _IMPORT_ERR:
            wx.MessageBox("fluxplace core failed to import:\n\n" + _IMPORT_ERR,
                          "fluxplace", wx.ICON_ERROR)
            return
        board = pcbnew.GetBoard()
        parts, nets = IO.read_board(board)
        cg = G.build(parts, nets)
        topo = T.analyze(cg)
        center = IO.board_center(board)
        from fluxplace import route as R
        pos, rot, rep = P.place_routed(parts, cg, topo, center=center)
        before = P.hpwl(parts, cg, {r: (parts[r]["x"], parts[r]["y"]) for r in parts})
        after = P.hpwl(parts, cg, pos)
        IO.apply_orientations(board, rot)                 # rotate first
        nmoved = IO.apply_positions(board, pos, parts)    # then center
        xs0 = []; ys0 = []; xs1 = []; ys1 = []
        for r, (x, y) in pos.items():
            w, h = P.eff_size(parts, r, rot.get(r, 0.0), 0.0)
            xs0.append(x - w / 2); ys0.append(y - h / 2)
            xs1.append(x + w / 2); ys1.append(y + h / 2)
        dims = IO.shrinkwrap_outline(board, min(xs0), min(ys0), max(xs1), max(ys1))
        pcbnew.Refresh()
        pct = 100 * (before - after) / before if before else 0
        routable = ("ROUTABLE — global router closed every net within capacity"
                    if rep["overflow"] == 0 else
                    f"WARNING: {rep['overflow']} congestion overflow(s) — review hotspots")
        wx.MessageBox(
            f"Placed {nmoved} components (route-aware: quad + builder + global route).\n\n"
            f"Weighted wirelength: {before:.0f} → {after:.0f} mm ({pct:+.0f}%)\n"
            f"Board: {dims[0]:.0f} × {dims[1]:.0f} mm\n"
            f"Hub: {topo.hub}   forks: {', '.join(topo.forks) or '—'}\n"
            f"{routable}\n\n"
            "Review, then route. Undo (Ctrl-Z) reverts everything.",
            "fluxplace", wx.ICON_INFORMATION)


FluxPlaceAction().register()
