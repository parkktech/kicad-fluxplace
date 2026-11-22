//! Footprints (K4): placement transform, pads (circle, rect, roundrect, oval, custom, trapezoid as a conservative
//! superset), drills and slots, NPTH, footprint graphics on Edge.Cuts and copper, footprint keepouts.
//! Pad position = footprint at + rotate(pad at, footprint angle). The pad angle in the file is absolute.

use crate::board::{Board, Item, Kind, Unsupported};
use crate::geom::{self, Core, Pt, Shape};
use crate::load::{finish_graphic, gr_shapes, is_yes, stroke_width, with_r, Ctx};
use crate::sexpr::{Kind as SK, Node};

/// (at x y [angle]): the position, first two numbers.
fn xy_at(cx: &Ctx, n: &Node) -> Result<Pt, String> {
    let at = n.first("at").ok_or_else(|| cx.err(n, "missing (at)"))?;
    match at.nums().map_err(|e| cx.err(at, &e))?.as_slice() {
        [x, y, ..] => Ok(Pt { x: *x, y: *y }),
        _ => Err(cx.err(at, "(at) needs x and y")),
    }
}

/// Angle in degrees stored as a plain number in the file (not nm).
fn deg_at(n: &Node, idx: usize) -> f64 {
    n.str_at(idx).and_then(|s| s.parse().ok()).unwrap_or(0.0)
}

#[derive(Clone, Copy)]
struct Xf {
    at: Pt,
    ang: f64,
}

impl Xf {
    fn pt(&self, p: Pt) -> Pt {
        let r = geom::rotate(p, self.ang);
        Pt { x: r.x + self.at.x, y: r.y + self.at.y }
    }
}

fn map_core(s: &Shape, f: &dyn Fn(Pt) -> Pt) -> Shape {
    let core = match &s.core {
        Core::Point(p) => Core::Point(f(*p)),
        Core::Seg(a, b) => Core::Seg(f(*a), f(*b)),
        Core::Poly(v) => Core::Poly(v.iter().map(|p| f(*p)).collect()),
    };
    Shape { core, r: s.r }
}

/// Pad-local shape (around the pad centre) placed at `base`, rotated by the pad's absolute angle.
fn place(s: &Shape, ang: f64, base: Pt) -> Shape {
    map_core(s, &|p| {
        let r = geom::rotate(p, ang);
        Pt { x: r.x + base.x, y: r.y + base.y }
    })
}

fn xy_list(cx: &Ctx, n: &Node) -> Result<Vec<Pt>, String> {
    n.kids("xy").map(|k| match k.nums().map_err(|e| cx.err(k, &e))?.as_slice() {
        [x, y] => Ok(Pt { x: *x, y: *y }),
        _ => Err(cx.err(k, "xy needs two numbers")),
    }).collect()
}

/// Copper mask of layer names: "*.Cu" = every copper layer, "F&B.Cu" = the two outer layers, else the exact name.
fn mask_of(b: &Board, names: &[String]) -> u64 {
    let all = b.all_copper_mask();
    names.iter().fold(0u64, |m, n| {
        m | if n == "*.Cu" {
            all
        } else if n == "F&B.Cu" {
            [b.layer_bit("F.Cu"), b.layer_bit("B.Cu")].into_iter().flatten().fold(0, |a, k| a | (1u64 << k))
        } else {
            b.layer_bit(n).map_or(0, |k| 1u64 << k)
        }
    })
}

fn layer_names(n: &Node) -> Vec<String> {
    n.first("layers").map(|c| c.atoms().iter().filter_map(|a| match &a.kind {
        SK::Str(s) | SK::Sym(s) => Some(s.clone()),
        _ => None,
    }).collect()).unwrap_or_default()
}

pub(crate) fn load_footprint(cx: &Ctx, b: &mut Board, n: &Node) -> Result<(), String> {
    let xf = Xf { at: xy_at(cx, n)?, ang: deg_at(n.first("at").ok_or_else(|| cx.err(n, "missing (at)"))?, 3) };
    let reference = n.kids("property").find(|p| p.str_at(1) == Some("Reference"))
        .and_then(|p| p.str_at(2)).unwrap_or("").to_string();
    let locked = is_yes(n, "locked");
    let fp_clr = cx.num_field(n, "clearance")?;
    for c in n.children().iter().skip(1) {
        match c.tag() {
            Some("pad") => load_pad(cx, b, c, &xf, &reference, locked, fp_clr)?,
            Some(t) if t.starts_with("fp_") && t != "fp_text" => {
                if t == "fp_curve" {
                    b.unsupported.push(Unsupported { what: "fp_curve".into(), at: cx.at(c),
                        effect: "not modelled on a footprint; dropped".into() });
                    continue;
                }
                let Some(shapes) = gr_shapes(cx, c)? else { continue };
                let w = stroke_width(cx, c)?;
                let layer = cx.str_field(c, "layer").unwrap_or("").to_string();
                let placed: Vec<Shape> = with_r(shapes, w / 2).iter().map(|s| map_core(s, &|p| xf.pt(p))).collect();
                finish_graphic(cx, b, c, &layer, placed)?;
            }
            Some("zone") => load_keepout(cx, b, c, &xf)?,
            _ => {}
        }
    }
    Ok(())
}

fn load_keepout(cx: &Ctx, b: &mut Board, z: &Node, xf: &Xf) -> Result<(), String> {
    let Some(k) = z.first("keepout") else { return Ok(()) };
    let poly = z.first("polygon").ok_or_else(|| cx.err(z, "keepout without polygon"))?;
    let pts: Vec<Pt> = xy_list(cx, poly.first("pts").unwrap_or(poly))?.into_iter().map(|p| xf.pt(p)).collect();
    let mut names = layer_names(z);
    if names.is_empty() {
        names.extend(cx.str_field(z, "layer").map(str::to_string));
    }
    let flags: serde_json::Map<String, serde_json::Value> = ["tracks", "vias", "pads", "copperpour", "footprints"]
        .iter().map(|f| (f.to_string(), serde_json::Value::String(
            k.first(f).and_then(|c| c.str_at(1)).unwrap_or("allowed").to_string()))).collect();
    let uuid = cx.uuid(b, z);
    let mut it = Item::new(Kind::Keepout, uuid, 0, mask_of(b, &names), vec![Shape { core: Core::Poly(pts), r: 0 }]);
    it.extra = serde_json::json!({"keepout": flags});
    b.add_item(it);
    Ok(())
}

/// Raw decimal value of a child such as (roundrect_rratio 0.25); None when absent.
fn ratio(cx: &Ctx, n: &Node, tag: &str) -> Result<Option<f64>, String> {
    match n.first(tag) {
        None => Ok(None),
        Some(c) => c.str_at(1).and_then(|s| s.parse().ok()).map(Some)
            .ok_or_else(|| cx.err(c, &format!("({tag}) needs a number"))),
    }
}

/// Two nm values of a child such as (rect_delta x y); [0, 0] when absent.
fn deltas(cx: &Ctx, n: &Node, tag: &str) -> Result<[i64; 2], String> {
    let Some(c) = n.first(tag) else { return Ok([0, 0]) };
    match c.nums().map_err(|e| cx.err(c, &e))?.as_slice() {
        [x, y] => Ok([*x, *y]),
        _ => Err(cx.err(c, &format!("({tag}) needs two numbers"))),
    }
}

fn local_shapes(cx: &Ctx, b: &mut Board, p: &Node, shape: &str, sx: i64, sy: i64) -> Result<Vec<Shape>, String> {
    let (hx, hy) = (sx / 2, sy / 2);
    let corners = |hx: i64, hy: i64| vec![Pt { x: -hx, y: -hy }, Pt { x: hx, y: -hy }, Pt { x: hx, y: hy }, Pt { x: -hx, y: hy }];
    let rect = || vec![Shape { core: Core::Poly(corners(hx, hy)), r: 0 }];
    let chamfer_nm = ratio(cx, p, "chamfer_ratio")?.map(|cr| (cr * sx.min(sy) as f64).round() as i64);
    let unsup = |b: &mut Board, what: &str, effect: &str| {
        b.unsupported.push(Unsupported { what: what.into(), at: cx.at(p), effect: effect.into() });
    };
    Ok(match shape {
        "circle" => vec![Shape { core: Core::Point(Pt { x: 0, y: 0 }), r: sx / 2 }],
        "rect" => match chamfer_nm {
            None => rect(),
            Some(k) => {
                // all four corners chamfered; a corner list that names fewer is not modelled: conservative rectangle
                let partial = p.first("chamfer").map_or(false, |c| c.atoms().len() < 4);
                if partial {
                    unsup(b, "partial pad chamfer", "modelled as the full rectangle");
                    rect()
                } else {
                    vec![Shape { core: Core::Poly(geom::chamfered_rect(Pt { x: 0, y: 0 }, sx, sy, k, k)
                        .map_err(|e| cx.err(p, &e))?), r: 0 }]
                }
            }
        },
        "roundrect" => {
            let rr = (sx.min(sy) as f64 * ratio(cx, p, "roundrect_rratio")?.unwrap_or(0.25)).round() as i64;
            if chamfer_nm.is_some() {
                unsup(b, "roundrect with chamfer", "modelled as the full rectangle");
                rect()
            } else {
                vec![geom::roundrect(Pt { x: 0, y: 0 }, sx, sy, rr).map_err(|e| cx.err(p, &e))?]
            }
        }
        "oval" => {
            let d = sx - sy;
            let (a, z) = if d >= 0 {
                (Pt { x: -d / 2, y: 0 }, Pt { x: d / 2, y: 0 })
            } else {
                (Pt { x: 0, y: d / 2 }, Pt { x: 0, y: -d / 2 })
            };
            vec![Shape { core: Core::Seg(a, z), r: sx.min(sy) / 2 }]
        }
        "trapezoid" => {
            // conservative superset: a rectangle that holds the trapezoid for these deltas (pcbnew check in K5)
            let [dx, dy] = deltas(cx, p, "rect_delta")?;
            let e = (dx.abs() + dy.abs()) / 2;
            unsup(b, "pad trapezoid", "modelled as a conservative rectangle");
            vec![Shape { core: Core::Poly(corners(hx + e, hy + e)), r: 0 }]
        }
        _ => {
            unsup(b, &format!("pad shape {shape}"), "modelled as its bounding rectangle");
            rect()
        }
    })
}

fn custom_shapes(cx: &Ctx, b: &mut Board, p: &Node, sx: i64, sy: i64) -> Result<Vec<Shape>, String> {
    let mut out = Vec::new();
    if let Some(prims) = p.first("primitives") {
        for c in prims.children().iter().skip(1) {
            match c.tag() {
                Some("gr_poly") | Some("gr_rect") if c.first("fill").and_then(|f| f.str_at(1)) != Some("no") => {
                    let pts = if c.tag() == Some("gr_rect") {
                        let (a, z) = (cx.pt(c, "start")?, cx.pt(c, "end")?);
                        vec![a, Pt { x: z.x, y: a.y }, z, Pt { x: a.x, y: z.y }]
                    } else {
                        xy_list(cx, c.first("pts").unwrap_or(c))?
                    };
                    out.push(Shape { core: Core::Poly(pts), r: 0 });
                }
                Some(t) if t.starts_with("gr_") => {
                    let w = stroke_width(cx, c)?;
                    if let Some(s) = gr_shapes(cx, c)? {
                        out.extend(with_r(s, w / 2));
                    }
                }
                _ => {}
            }
        }
    }
    if let Some(opts) = p.first("options") {
        if let Some(anchor) = opts.first("anchor").and_then(|a| a.str_at(1)) {
            let mut a = local_shapes(cx, b, p, if anchor == "circle" { "circle" } else { "rect" }, sx, sy)?;
            out.append(&mut a);
        }
    }
    if out.is_empty() {
        b.unsupported.push(Unsupported { what: "custom pad without primitives".into(), at: cx.at(p),
            effect: "modelled as its bounding rectangle".into() });
        out = local_shapes(cx, b, p, "rect", sx, sy)?;
    }
    Ok(out)
}

fn load_pad(cx: &Ctx, b: &mut Board, p: &Node, xf: &Xf, reference: &str, fp_locked: bool,
            fp_clr: Option<i64>) -> Result<(), String> {
    let num = p.str_at(1).unwrap_or("").to_string();
    let typ = p.str_at(2).unwrap_or("").to_string();
    let shape = p.str_at(3).unwrap_or("").to_string();
    let at = p.first("at").ok_or_else(|| cx.err(p, "pad without at"))?;
    let ang = deg_at(at, 3);
    let centre = xf.pt(xy_at(cx, p)?);
    let size = p.first("size").map(|s| s.nums()).transpose().map_err(|e| cx.err(p, &e))?.unwrap_or_default();
    let (sx, sy) = match size.as_slice() {
        [x, y] => (*x, *y),
        [d] => (*d, *d),
        _ => (0, 0),
    };
    let mask = mask_of(b, &layer_names(p));
    let net = cx.net(b, p)?;
    let clr = cx.num_field(p, "clearance")?.or(fp_clr);
    let uuid = cx.uuid(b, p);
    let npth = typ == "np_thru_hole";
    let ref_ = format!("{reference}.{num}");

    if p.first("padstack").is_some() {
        b.unsupported.push(Unsupported { what: "padstack".into(), at: cx.at(p),
            effect: "per-layer shapes not modelled; conservative rectangle on every copper layer".into() });
        let all = b.all_copper_mask();
        let shapes = local_shapes(cx, b, p, "rect", sx, sy)?.iter().map(|s| place(s, ang, centre)).collect();
        let mut it = Item::new(Kind::Pad, uuid.clone(), net, all, shapes);
        it.ref_ = ref_;
        it.locked = fp_locked;
        it.clr_local = clr;
        b.add_item(it);
        return Ok(());
    }

    if !npth && mask != 0 {
        let local = match shape.as_str() {
            "custom" => custom_shapes(cx, b, p, sx, sy)?,
            _ => local_shapes(cx, b, p, &shape, sx, sy)?,
        };
        let shapes = local.iter().map(|s| place(s, ang, centre)).collect();
        let mut it = Item::new(Kind::Pad, uuid.clone(), net, mask, shapes);
        it.ref_ = ref_.clone();
        it.locked = fp_locked;
        it.clr_local = clr;
        it.extra = serde_json::json!({"shape": shape, "pad_angle": ang});
        b.add_item(it);
    }

    if let Some(d) = p.first("drill") {
        let oval = d.atoms().first().map(|a| matches!(&a.kind, SK::Sym(s) if s == "oval")).unwrap_or(false);
        let v = d.nums().map_err(|e| cx.err(d, &e))?;
        let (dx, dy) = match (oval, v.as_slice()) {
            (true, [x, y]) => (*x, *y),
            (false, [x]) => (*x, *x),
            _ => return Err(cx.err(d, "drill needs a diameter or (oval x y)")),
        };
        let off = match d.first("offset") {
            Some(o) => {
                let v = o.nums().map_err(|e| cx.err(o, &e))?;
                match v.as_slice() {
                    [x, y] => {
                        let r = geom::rotate(Pt { x: *x, y: *y }, ang);
                        Pt { x: r.x, y: r.y }
                    }
                    _ => Pt { x: 0, y: 0 },
                }
            }
            None => Pt { x: 0, y: 0 },
        };
        let c = Pt { x: centre.x + off.x, y: centre.y + off.y };
        let hole_shape = if dx == dy {
            Shape { core: Core::Point(c), r: dx / 2 }
        } else {
            let len = (dx - dy).abs() / 2;
            let (a, z) = if dx > dy {
                (Pt { x: -len, y: 0 }, Pt { x: len, y: 0 })
            } else {
                (Pt { x: 0, y: -len }, Pt { x: 0, y: len })
            };
            place(&Shape { core: Core::Seg(a, z), r: dx.min(dy) / 2 }, ang, c)
        };
        let layers = if mask != 0 { mask } else { b.all_copper_mask() };
        for k in 0..b.copper.len() {
            if layers & (1u64 << k) != 0 {
                let mut h = Item::new(Kind::Hole, uuid.clone(), net, 1u64 << k, vec![hole_shape.clone()]);
                h.ref_ = ref_.clone();
                h.locked = fp_locked;
                h.extra = serde_json::json!({"npth": npth});
                b.add_item(h);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use crate::board::{Board, Kind};
    use crate::load::load;

    fn board(fp_at: &str, body: &str) -> Board {
        let src = format!("(kicad_pcb (version 20260206) (layers (0 \"F.Cu\" signal) (4 \"In1.Cu\" signal) \
(31 \"B.Cu\" signal) (44 \"Edge.Cuts\" user))\n(footprint \"R\" (layer \"F.Cu\") (at {fp_at}) \
(property \"Reference\" \"R1\") {body})\n)\n");
        load(&src).unwrap()
    }

    fn mm6(v: i64) -> f64 {
        v as f64 / 1e6
    }

    fn pads(b: &Board) -> Vec<&crate::board::Item> {
        b.items.iter().filter(|i| i.kind == Kind::Pad).collect()
    }

    fn bbox_mm(b: &Board, i: usize) -> [f64; 4] {
        b.items[i].bbox.map(mm6)
    }

    fn close(a: [f64; 4], e: [f64; 4]) -> bool {
        a.iter().zip(e.iter()).all(|(x, y)| (x - y).abs() < 2e-6)
    }

    #[test]
    fn rect_pad_at_0_90_37_degrees_hand_values() {
        let pad = "(pad \"1\" smd rect (at 1 0 0) (size 2 1) (layers \"F.Cu\"))";
        let b0 = board("0 0 0", pad);
        assert!(close(bbox_mm(&b0, pads(&b0)[0].id as usize), [0.0, -0.5, 2.0, 0.5]));
        let b90 = board("0 0 90", pad);
        assert!(close(bbox_mm(&b90, pads(&b90)[0].id as usize), [-1.0, -1.5, 1.0, -0.5]));
        let b37 = board("0 0 37", pad);
        let (c, s) = (37f64.to_radians().cos(), 37f64.to_radians().sin());
        let want = [c - 1.0, -s - 0.5, c + 1.0, -s + 0.5];
        assert!(close(bbox_mm(&b37, pads(&b37)[0].id as usize), want), "{:?}", bbox_mm(&b37, 0));
    }

    #[test]
    fn rotated_pad_swaps_its_sides() {
        let b = board("0 0 0", "(pad \"1\" smd rect (at 1 0 90) (size 2 1) (layers \"F.Cu\"))");
        assert!(close(bbox_mm(&b, pads(&b)[0].id as usize), [0.5, -1.0, 1.5, 1.0]));
    }

    #[test]
    fn circle_oval_and_roundrect() {
        let c = board("0 0 0", "(pad \"1\" smd circle (at 1 0) (size 1 1) (layers \"F.Cu\"))");
        assert!(close(bbox_mm(&c, pads(&c)[0].id as usize), [0.5, -0.5, 1.5, 0.5]));
        let o = board("0 0 0", "(pad \"1\" smd oval (at 1 0) (size 2 1) (layers \"F.Cu\"))");
        assert!(close(bbox_mm(&o, pads(&o)[0].id as usize), [0.0, -0.5, 2.0, 0.5]));
        let r = board("0 0 0", "(pad \"1\" smd roundrect (at 1 0) (size 2 1) (layers \"F.Cu\") (roundrect_rratio 0.25))");
        let p = pads(&r)[0];
        assert_eq!(p.shapes[0].r, 250_000);
        assert!(close(bbox_mm(&r, p.id as usize), [0.0, -0.5, 2.0, 0.5]));
    }

    #[test]
    fn slot_drill_is_a_capsule_on_every_span_layer() {
        let b = board("0 0 0", "(pad \"1\" thru_hole oval (at 0 0) (size 1.2 0.8) (drill oval 0.6 0.4) (layers \"*.Cu\"))");
        let holes: Vec<_> = b.items.iter().filter(|i| i.kind == Kind::Hole).collect();
        assert_eq!(holes.len(), 3, "one hole per copper layer");
        assert_eq!(holes[0].shapes[0].r, 200_000);
        assert_eq!(holes[0].shapes[0].core, crate::geom::Core::Seg(
            crate::geom::Pt { x: -100_000, y: 0 }, crate::geom::Pt { x: 100_000, y: 0 }));
    }

    #[test]
    fn layer_wildcards_expand() {
        let all = board("0 0 0", "(pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"*.Cu\"))");
        assert_eq!(pads(&all)[0].layers, 0b111);
        let outer = board("0 0 0", "(pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"F&B.Cu\"))");
        assert_eq!(pads(&outer)[0].layers, 0b101);
        let bot = board("0 0 0", "(pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"B.Cu\"))");
        assert_eq!(pads(&bot)[0].layers, 0b100);
    }

    #[test]
    fn padstack_is_unsupported_with_conservative_shape() {
        let b = board("0 0 0", "(pad \"1\" smd rect (at 1 0) (size 2 1) (layers \"F.Cu\") (padstack (x 1)))");
        assert!(b.unsupported.iter().any(|u| u.what == "padstack"));
        let p = pads(&b)[0];
        assert_eq!(p.layers, 0b111);
        assert!(close(bbox_mm(&b, p.id as usize), [0.0, -0.5, 2.0, 0.5]));
    }

    #[test]
    fn npth_has_holes_but_no_copper_pad() {
        let b = board("0 0 0", "(pad \"H\" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers \"*.Cu\"))");
        assert!(pads(&b).is_empty());
        let holes: Vec<_> = b.items.iter().filter(|i| i.kind == Kind::Hole).collect();
        assert_eq!(holes.len(), 3);
        assert_eq!(holes[0].shapes[0].r, 1_600_000);
    }

    #[test]
    fn custom_pad_polygon_and_footprint_edge_graphics() {
        let body = "(pad \"1\" smd custom (at 0 0) (size 0.1 0.1) (layers \"F.Cu\") \
(primitives (gr_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (width 0) (fill yes))))\n\
(fp_line (start 0 0) (end 1 0) (stroke (width 0.05) (type solid)) (layer \"Edge.Cuts\"))";
        let b = board("10 0 90", body);
        let p = pads(&b)[0];
        assert!(matches!(p.shapes[0].core, crate::geom::Core::Poly(_)));
        let e = b.items.iter().find(|i| i.kind == Kind::Edge).unwrap();
        // fp at (10, 0) rotated 90: the local edge (0,0)-(1,0) becomes (10,0)-(10,-1); half width 0.025
        assert!(close(bbox_mm(&b, e.id as usize), [9.975, -1.025, 10.025, 0.025]), "{:?}", bbox_mm(&b, e.id as usize));
    }
}
