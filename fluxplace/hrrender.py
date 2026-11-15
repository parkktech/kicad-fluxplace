"""Render a hand-routing window (output of handroute.window) to PNG or SVG: one panel per copper layer, 1 mm coordinate grid.
Only drawing; no geometry decisions."""
import colorsys
import hashlib
import math


def _col(net):
    if not net:
        return (150, 150, 150)
    h = int(hashlib.md5(net.encode()).hexdigest()[:6], 16)
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, 0.55 + (h >> 9) % 40 / 100.0, 0.95)
    return (int(r * 255), int(g * 255), int(b * 255))


class _Png:
    def __init__(self, w, h):
        from PIL import Image, ImageDraw, ImageFont
        self.im = Image.new("RGB", (w, h), (18, 18, 22))
        self.d = ImageDraw.Draw(self.im, "RGBA")
        try:
            self.f = ImageFont.load_default(size=11)
            self.fb = ImageFont.load_default(size=15)
        except TypeError:
            self.f = self.fb = ImageFont.load_default()

    def line(self, a, b, w, c, alpha=255):
        self.d.line([a, b], fill=c + (alpha,), width=max(1, int(round(w))))
        r = max(1, int(round(w))) / 2
        for p in (a, b):
            self.d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=c + (alpha,))

    def poly(self, pts, fill=None, outline=None, alpha=255):
        self.d.polygon(pts, fill=(fill + (alpha,)) if fill else None, outline=(outline + (255,)) if outline else None)

    def circle(self, c, r, fill=None, outline=None, alpha=255):
        self.d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=(fill + (alpha,)) if fill else None,
                       outline=(outline + (255,)) if outline else None)

    def text(self, p, s, c=(235, 235, 235), big=False):
        self.d.text(p, s, fill=c, font=self.fb if big else self.f)

    def save(self, path):
        self.im.save(path)


class _Svg:
    def __init__(self, w, h):
        self.w, self.h, self.o = w, h, [f'<rect width="{w}" height="{h}" fill="#121216"/>']

    @staticmethod
    def _c(c):
        return "#%02x%02x%02x" % tuple(c)

    def line(self, a, b, w, c, alpha=255):
        self.o.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" stroke="{self._c(c)}" '
                      f'stroke-width="{max(1, w):.1f}" stroke-linecap="round" opacity="{alpha / 255:.2f}"/>')

    def poly(self, pts, fill=None, outline=None, alpha=255):
        p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        self.o.append(f'<polygon points="{p}" fill="{self._c(fill) if fill else "none"}" fill-opacity="{alpha / 255:.2f}" '
                      f'stroke="{self._c(outline) if outline else "none"}" stroke-width="1"/>')

    def circle(self, c, r, fill=None, outline=None, alpha=255):
        self.o.append(f'<circle cx="{c[0]:.1f}" cy="{c[1]:.1f}" r="{r:.1f}" fill="{self._c(fill) if fill else "none"}" '
                      f'fill-opacity="{alpha / 255:.2f}" stroke="{self._c(outline) if outline else "none"}"/>')

    def text(self, p, s, c=(235, 235, 235), big=False):
        s = s.replace("&", "&amp;").replace("<", "&lt;")
        self.o.append(f'<text x="{p[0]:.1f}" y="{p[1] + 11:.1f}" fill="{self._c(c)}" font-family="monospace" '
                      f'font-size="{15 if big else 10}">{s}</text>')

    def save(self, path):
        open(path, "w").write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}">' + "".join(self.o) + "</svg>")


def render(W, path, overlay=None, px_mm=None, sites=True, free=False):
    """W = handroute.window() result (with the raw geometry kept under W['_geom']). overlay = parsed path prims."""
    x0, y0, x1, y1 = W["window"]
    layers = W["layers"]
    G = W["_geom"]
    ww, hh = x1 - x0, y1 - y0
    if px_mm is None:
        px_mm = max(20, min(160, int(640 / max(ww, 1e-6))))
    pw, ph = int(ww * px_mm), int(hh * px_mm)
    cols = 1 if len(layers) == 1 else 2
    rows = (len(layers) + cols - 1) // cols
    pad = 28
    cw, ch = pw + 2 * pad, ph + 2 * pad + 18
    svg = path.endswith(".svg")
    C = (_Svg if svg else _Png)(cw * cols, ch * rows)
    for k, lay in enumerate(layers):
        ox, oy = (k % cols) * cw + pad, (k // cols) * ch + pad + 18
        T = lambda p: (ox + (p[0] - x0) * px_mm, oy + (p[1] - y0) * px_mm)       # noqa: E731
        C.text((ox, oy - 40), f"{lay}   window {x0:.2f},{y0:.2f} .. {x1:.2f},{y1:.2f}  grid 1 mm", big=True)
        C.poly([T((x0, y0)), T((x1, y0)), T((x1, y1)), T((x0, y1))], fill=(26, 26, 32), outline=(70, 70, 80))
        gx = math.ceil(x0)
        while gx <= x1:
            C.line(T((gx, y0)), T((gx, y1)), 1, (60, 60, 70), 120)
            C.text((T((gx, y0))[0] + 2, oy - 14), f"{gx:g}", (150, 150, 170))
            gx += 1
        gy = math.ceil(y0)
        while gy <= y1:
            C.line(T((x0, gy)), T((x1, gy)), 1, (60, 60, 70), 120)
            C.text((ox - 27, T((x0, gy))[1] - 5), f"{gy:g}", (150, 150, 170))
            gy += 1
        for z in G["zones"]:
            if z["layer"] == lay and z["outline"]:
                C.poly([T(p) for p in z["outline"]], fill=_col(z["net"]), alpha=18)
        for ko in G["keepouts"]:
            if lay in ko["layers"]:
                C.poly([T(p) for p in ko["pts"]], fill=(255, 60, 60), outline=(255, 60, 60), alpha=40)
        for e in G["edge"]:
            C.line(T(e[0]), T(e[1]), 2, (255, 255, 255))
        if free and lay in G.get("free_mask", {}):
            for (a, b) in G["free_mask"][lay]:
                C.line(T(a), T(b), px_mm * 0.05, (60, 200, 90), 60)
        for p in G["pads"]:
            if lay in p["layers"]:
                C.poly([T(q) for q in p["poly"]], fill=_col(p["net"]), outline=(255, 255, 255), alpha=170)
                C.text((T(p["pos"])[0] - 10, T(p["pos"])[1] - 6), p["ref"].split(".")[-1], (10, 10, 10))
        for t in G["tracks"]:
            if t["layer"] == lay:
                C.line(T(t["a"]), T(t["b"]), t["w"] * px_mm, _col(t["net"]), 235)
                if t["locked"]:
                    m = T(((t["a"][0] + t["b"][0]) / 2, (t["a"][1] + t["b"][1]) / 2))
                    C.circle(m, 2, fill=(255, 255, 255))
        for v in G["vias"]:
            c = T(v["pos"])
            C.circle(c, v["dia"] / 2 * px_mm, fill=_col(v["net"]), outline=(255, 255, 255), alpha=220)
            C.circle(c, v["drill"] / 2 * px_mm, fill=(18, 18, 22))
        if sites and G.get("via_sites"):
            for s in G["via_sites"]:
                C.circle(T(s), 1.5, fill=(80, 255, 120))
        for pr in overlay or []:
            if pr[0] == "seg" and pr[1] == lay:
                C.line(T(pr[2]), T(pr[3]), max(2, (W.get("track", {}).get("width", 0.15)) * px_mm), (255, 255, 0), 150)
            if pr[0] == "via":
                C.circle(T(pr[1]), max(3, W.get("via", {}).get("dia", 0.45) / 2 * px_mm), outline=(255, 255, 0), alpha=0)
    C.save(path)
    return path
