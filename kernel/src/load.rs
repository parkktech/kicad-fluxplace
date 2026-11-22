//! Board loader (K3 scope): layers, nets (KiCad 7 `(net N "NAME")` and KiCad 10 `(net "NAME")`), segments,
//! arcs, vias (+ one Hole per copper layer), Edge.Cuts graphics and copper graphics. Footprints (K4) and zones (K5) come later.

use crate::board::{Board, Item, Kind, Unsupported};
use crate::geom::{arc_polyline, Core, Pt, Shape};
use crate::sexpr::{self, Kind as SK, Node};
use std::collections::HashMap;

/// Arc chord error budget (nm); the arc's r is raised by the same amount (conservative).
const ARC_ERR_NM: f64 = 1000.0;

fn line_of(src: &str, at: usize) -> usize {
    1 + src.as_bytes()[..at.min(src.len())].iter().filter(|&&b| b == b'\n').count()
}

pub(crate) struct Ctx<'a> {
    pub(crate) src: &'a str,
    pub(crate) nets_by_num: HashMap<String, String>,
}

impl Ctx<'_> {
    pub(crate) fn at(&self, n: &Node) -> String {
        format!("line {}", line_of(self.src, n.span.0))
    }

    pub(crate) fn err(&self, n: &Node, msg: &str) -> String {
        format!("{}: {msg}", self.at(n))
    }

    pub(crate) fn pt(&self, n: &Node, tag: &str) -> Result<Pt, String> {
        let c = n.first(tag).ok_or_else(|| self.err(n, &format!("missing ({tag} x y)")))?;
        let v = c.nums().map_err(|e| self.err(c, &e))?;
        match v.as_slice() {
            [x, y] => Ok(Pt { x: *x, y: *y }),
            _ => Err(self.err(c, &format!("({tag}) needs two numbers"))),
        }
    }

    pub(crate) fn num_field(&self, n: &Node, tag: &str) -> Result<Option<i64>, String> {
        match n.first(tag) {
            None => Ok(None),
            Some(c) => c.nums().map_err(|e| self.err(c, &e))?.first().copied().map(Some)
                .ok_or_else(|| self.err(c, &format!("({tag}) needs a number"))),
        }
    }

    pub(crate) fn str_field<'b>(&self, n: &'b Node, tag: &str) -> Option<&'b str> {
        n.first(tag).and_then(|c| c.str_at(1))
    }

    /// Net of an item: (net "NAME") | (net N "NAME") | (net N) through the top-level declarations.
    pub(crate) fn net(&self, b: &mut Board, n: &Node) -> Result<u32, String> {
        let Some(c) = n.first("net") else { return Ok(0) };
        let atoms = c.atoms();
        let name = match atoms.as_slice() {
            [] => return Ok(0),
            [one] => match &one.kind {
                SK::Str(s) => s.clone(),
                SK::Num(k) => self.nets_by_num.get(k).cloned().ok_or_else(|| self.err(c, &format!("unknown net {k}")))?,
                _ => return Err(self.err(c, "bad net")),
            },
            [_, two, ..] => match &two.kind {
                SK::Str(s) => s.clone(),
                _ => return Err(self.err(c, "bad net")),
            },
        };
        Ok(b.intern_net(&name))
    }

    /// (layer name, bit) for a copper layer; None for a non-copper layer.
    pub(crate) fn layer(&self, b: &Board, n: &Node) -> Result<Option<(String, u64)>, String> {
        let name = self.str_field(n, "layer").ok_or_else(|| self.err(n, "missing (layer)"))?;
        Ok(b.layer_bit(name).map(|i| (name.to_string(), 1u64 << i)))
    }

    pub(crate) fn uuid(&self, b: &Board, n: &Node) -> String {
        self.str_field(n, "uuid").map(str::to_string)
            .unwrap_or_else(|| format!("00000000-0000-0000-0000-{:012x}", b.items.len() + 1))
    }
}

/// Stroke width of a graphic: (width w) or (stroke (width w) ...); 0 when absent.
pub(crate) fn stroke_width(cx: &Ctx, n: &Node) -> Result<i64, String> {
    if let Some(w) = cx.num_field(n, "width")? {
        return Ok(w);
    }
    match n.first("stroke") {
        Some(st) => Ok(cx.num_field(st, "width")?.unwrap_or(0)),
        None => Ok(0),
    }
}

pub(crate) fn is_yes(n: &Node, tag: &str) -> bool {
    n.first(tag).and_then(|c| c.str_at(1)) == Some("yes")
}

fn stack_key(name: &str, id: u64) -> (u8, u64) {
    match name {
        "F.Cu" => (0, 0),
        "B.Cu" => (2, 0),
        _ => (1, id),
    }
}

fn copper_stack(root: &Node) -> Vec<String> {
    let mut v: Vec<(u64, String)> = Vec::new();
    if let Some(layers) = root.first("layers") {
        for l in layers.children().iter().skip(1) {
            let (Some(id), Some(name)) = (l.str_at(0), l.str_at(1)) else { continue };
            if name.ends_with(".Cu") {
                v.push((id.parse().unwrap_or(0), name.to_string()));
            }
        }
    }
    v.sort_by_key(|(id, name)| stack_key(name, *id));
    v.into_iter().map(|(_, n)| n).collect()
}

fn circle_shapes(c: Pt, r: i64) -> Vec<Shape> {
    let p = |x: i64, y: i64| Pt { x, y };
    let quarter = [p(c.x + r, c.y), p(c.x, c.y + r), p(c.x - r, c.y), p(c.x, c.y - r), p(c.x + r, c.y)];
    let mut out = Vec::new();
    for w in quarter.windows(3).step_by(2) {
        let (pts, _) = arc_polyline(w[0], w[1], w[2], ARC_ERR_NM);
        out.extend(pts.windows(2).map(|s| Shape { core: Core::Seg(s[0], s[1]), r: 0 }));
    }
    out
}

pub(crate) fn chain(pts: &[Pt], closed: bool) -> Vec<Shape> {
    let mut v: Vec<Shape> = pts.windows(2).map(|w| Shape { core: Core::Seg(w[0], w[1]), r: 0 }).collect();
    if closed && pts.len() > 2 {
        v.push(Shape { core: Core::Seg(pts[pts.len() - 1], pts[0]), r: 0 });
    }
    v
}

pub(crate) fn with_r(shapes: Vec<Shape>, r: i64) -> Vec<Shape> {
    shapes.into_iter().map(|s| Shape { r, ..s }).collect()
}

/// Shapes of one gr_* primitive (r = 0; the caller sets the stroke half width).
pub(crate) fn gr_shapes(cx: &Ctx, n: &Node) -> Result<Option<Vec<Shape>>, String> {
    let tag = n.tag().unwrap_or("").replacen("fp_", "gr_", 1);
    let tag = tag.as_str();
    Ok(Some(match tag {
        "gr_line" => chain(&[cx.pt(n, "start")?, cx.pt(n, "end")?], false),
        "gr_rect" => {
            let (a, b) = (cx.pt(n, "start")?, cx.pt(n, "end")?);
            chain(&[a, Pt { x: b.x, y: a.y }, b, Pt { x: a.x, y: b.y }], true)
        }
        "gr_poly" => {
            let c = n.first("pts").ok_or_else(|| cx.err(n, "gr_poly without pts"))?;
            let pts: Vec<Pt> = c.kids("xy").map(|k| match k.nums().map_err(|e| cx.err(k, &e))?.as_slice() {
                [x, y] => Ok(Pt { x: *x, y: *y }),
                _ => Err(cx.err(k, "xy needs two numbers")),
            }).collect::<Result<_, String>>()?;
            chain(&pts, true)
        }
        "gr_arc" => {
            let (pts, _) = arc_polyline(cx.pt(n, "start")?, cx.pt(n, "mid")?, cx.pt(n, "end")?, ARC_ERR_NM);
            chain(&pts, false)
        }
        "gr_circle" => {
            let c = cx.pt(n, "center")?;
            let e = cx.pt(n, "end")?;
            circle_shapes(c, ((e.x - c.x) as f64).hypot((e.y - c.y) as f64).round() as i64)
        }
        _ => return Ok(None),
    }))
}

pub(crate) fn copper_item_for_gr(cx: &Ctx, b: &mut Board, n: &Node, shapes: Vec<Shape>, mask: u64) {
    let uuid = cx.uuid(b, n);
    b.add_item(Item::new(Kind::Graphic, uuid, 0, mask, shapes));
}

fn load_segment(cx: &Ctx, b: &mut Board, n: &Node) -> Result<(), String> {
    let Some((_, mask)) = cx.layer(b, n)? else {
        b.unsupported.push(Unsupported { what: "segment".into(), at: cx.at(n), effect: "layer is not copper; dropped".into() });
        return Ok(());
    };
    let w = cx.num_field(n, "width")?.ok_or_else(|| cx.err(n, "segment without width"))?;
    let (a, z) = (cx.pt(n, "start")?, cx.pt(n, "end")?);
    let net = cx.net(b, n)?;
    let mut it = Item::new(Kind::Track, cx.uuid(b, n), net, mask, vec![Shape { core: Core::Seg(a, z), r: w / 2 }]);
    it.locked = is_yes(n, "locked");
    b.add_item(it);
    Ok(())
}

fn load_arc(cx: &Ctx, b: &mut Board, n: &Node) -> Result<(), String> {
    let Some((_, mask)) = cx.layer(b, n)? else {
        b.unsupported.push(Unsupported { what: "arc".into(), at: cx.at(n), effect: "layer is not copper; dropped".into() });
        return Ok(());
    };
    let w = cx.num_field(n, "width")?.ok_or_else(|| cx.err(n, "arc without width"))?;
    let (pts, bound) = arc_polyline(cx.pt(n, "start")?, cx.pt(n, "mid")?, cx.pt(n, "end")?, ARC_ERR_NM);
    let r = w / 2 + bound.ceil() as i64;
    let net = cx.net(b, n)?;
    let shapes = with_r(chain(&pts, false), r);
    let mut it = Item::new(Kind::Arc, cx.uuid(b, n), net, mask, shapes);
    it.locked = is_yes(n, "locked");
    it.extra = serde_json::json!({"width": w as f64 / 1e6, "arc": true});
    b.add_item(it);
    Ok(())
}

fn load_via(cx: &Ctx, b: &mut Board, n: &Node) -> Result<(), String> {
    let at = cx.pt(n, "at")?;
    let size = cx.num_field(n, "size")?.ok_or_else(|| cx.err(n, "via without size"))?;
    let drill = cx.num_field(n, "drill")?.unwrap_or(size / 2);
    let layers = n.first("layers").map(|c| c.atoms().iter().filter_map(|a| match &a.kind {
        SK::Str(s) => Some(s.clone()),
        _ => None,
    }).collect::<Vec<_>>()).unwrap_or_default();
    let mask = match layers.as_slice() {
        [] => b.all_copper_mask(),
        [a, z] => b.span_mask(a, z).ok_or_else(|| cx.err(n, "via layers are not copper"))?,
        _ => return Err(cx.err(n, "via (layers) needs two layers")),
    };
    let net = cx.net(b, n)?;
    let uuid = cx.uuid(b, n);
    let mut via = Item::new(Kind::Via, uuid.clone(), net, mask, vec![Shape { core: Core::Point(at), r: size / 2 }]);
    via.locked = is_yes(n, "locked");
    via.extra = serde_json::json!({"via": true, "dia": size as f64 / 1e6, "drill": drill as f64 / 1e6});
    b.add_item(via);
    for k in 0..b.copper.len() {
        if mask & (1u64 << k) != 0 {
            let hole = Item::new(Kind::Hole, uuid.clone(), net, 1u64 << k, vec![Shape { core: Core::Point(at), r: drill / 2 }]);
            b.add_item(hole);
        }
    }
    Ok(())
}

pub(crate) fn load_graphic(cx: &Ctx, b: &mut Board, n: &Node) -> Result<(), String> {
    let layer = cx.str_field(n, "layer").unwrap_or("");
    let width = stroke_width(cx, n)?;
    let tag = n.tag().unwrap_or("").replacen("fp_", "gr_", 1);
    let tag = tag.as_str();
    if tag == "gr_curve" {
        let pts: Vec<Pt> = n.kids("pts").flat_map(|p| p.kids("xy")).filter_map(|k| {
            let v = k.nums().ok()?;
            match v.as_slice() {
                [x, y] => Some(Pt { x: *x, y: *y }),
                _ => None,
            }
        }).collect();
        let (x0, y0) = (pts.iter().map(|p| p.x).min(), pts.iter().map(|p| p.y).min());
        let (x1, y1) = (pts.iter().map(|p| p.x).max(), pts.iter().map(|p| p.y).max());
        if let (Some(x0), Some(y0), Some(x1), Some(y1)) = (x0, y0, x1, y1) {
            let box_pts = [Pt { x: x0, y: y0 }, Pt { x: x1, y: y0 }, Pt { x: x1, y: y1 }, Pt { x: x0, y: y1 }];
            b.unsupported.push(Unsupported { what: "gr_curve".into(), at: cx.at(n),
                effect: "not flattened; conservative bounding polygon of the control points".into() });
            let shapes = with_r(chain(&box_pts, true), width / 2);
            return finish_graphic(cx, b, n, layer, shapes);
        }
        return Ok(());
    }
    if tag == "gr_text" {
        return Ok(());
    }
    let Some(shapes) = gr_shapes(cx, n)? else { return Ok(()) };
    finish_graphic(cx, b, n, layer, with_r(shapes, width / 2))
}

pub(crate) fn finish_graphic(cx: &Ctx, b: &mut Board, n: &Node, layer: &str, shapes: Vec<Shape>) -> Result<(), String> {
    if layer == "Edge.Cuts" {
        let uuid = cx.uuid(b, n);
        b.add_item(Item::new(Kind::Edge, uuid, 0, 0, shapes));
    } else if let Some(bit) = b.layer_bit(layer) {
        copper_item_for_gr(cx, b, n, shapes, 1u64 << bit);
    }
    Ok(())
}

/// Parse a KiCad board file text into the K3 model.
pub fn load(src: &str) -> Result<Board, String> {
    let root = sexpr::parse(src)?;
    if root.tag() != Some("kicad_pcb") {
        return Err("not a kicad_pcb file".into());
    }
    let mut b = Board::new(copper_stack(&root));
    b.file_version = root.first("version").and_then(|v| v.str_at(1)).unwrap_or("").to_string();
    let mut nets_by_num = HashMap::new();
    for d in root.kids("net") {
        if let [num, name] = d.atoms().as_slice() {
            if let (SK::Num(k), SK::Str(v)) = (&num.kind, &name.kind) {
                nets_by_num.insert(k.clone(), v.clone());
            }
        }
    }
    let cx = Ctx { src, nets_by_num };
    for n in root.children().iter().skip(1) {
        match n.tag() {
            Some("segment") => load_segment(&cx, &mut b, n)?,
            Some("arc") => load_arc(&cx, &mut b, n)?,
            Some("via") => load_via(&cx, &mut b, n)?,
            Some("footprint") => crate::load_fp::load_footprint(&cx, &mut b, n)?,
            Some(t) if t.starts_with("gr_") => load_graphic(&cx, &mut b, n)?,
            _ => {}
        }
    }
    Ok(b)
}


#[cfg(test)]
mod tests {
    use super::*;

    const K10: &str = "(kicad_pcb (version 20260206)\n(layers (0 \"F.Cu\" signal) (4 \"In1.Cu\" signal) (31 \"B.Cu\" signal) (44 \"Edge.Cuts\" user))\n\
(segment (start 1.0 2.0) (end 3.5 2.0) (width 0.2) (layer \"F.Cu\") (net \"N\") (uuid \"a\"))\n\
(via (at 3 2) (size 0.6) (drill 0.3) (layers \"F.Cu\" \"B.Cu\") (net \"N\") (uuid \"b\"))\n\
(arc (start 0 0) (mid 1 1) (end 2 0) (width 0.1) (layer \"B.Cu\") (net \"\") (uuid \"d\"))\n\
(gr_line (start 0 0) (end 10 0) (layer \"Edge.Cuts\") (width 0.05) (uuid \"c\"))\n\
(gr_rect (start 0 0) (end 2 1) (layer \"F.Silkscreen\") (width 0.1))\n)\n";

    const K7: &str = "(kicad_pcb (version 20221018)\n(layers (0 \"F.Cu\" signal) (4 \"In1.Cu\" signal) (31 \"B.Cu\" signal) (44 \"Edge.Cuts\" user))\n\
(net 0 \"\") (net 5 \"N\")\n\
(segment (start 1.0 2.0) (end 3.5 2.0) (width 0.2) (layer \"F.Cu\") (net 5) (uuid \"a\"))\n\
(via (at 3 2) (size 0.6) (drill 0.3) (layers \"F.Cu\" \"B.Cu\") (net 5) (uuid \"b\"))\n\
(arc (start 0 0) (mid 1 1) (end 2 0) (width 0.1) (layer \"B.Cu\") (net 0) (uuid \"d\"))\n\
(gr_line (start 0 0) (end 10 0) (layer \"Edge.Cuts\") (width 0.05) (uuid \"c\"))\n\
(gr_rect (start 0 0) (end 2 1) (layer \"F.Silkscreen\") (width 0.1))\n)\n";

    #[test]
    fn kicad7_and_kicad10_net_forms_load_to_the_same_items() {
        let a = load(K10).unwrap();
        let b = load(K7).unwrap();
        assert_eq!(a.items, b.items);
        assert_eq!(a.nets, b.nets);
        assert_eq!(a.copper, vec!["F.Cu", "In1.Cu", "B.Cu"]);
    }

    #[test]
    fn segment_and_via_in_nm_with_span() {
        let b = load(K10).unwrap();
        let t = b.items.iter().find(|i| i.kind == Kind::Track).unwrap();
        assert_eq!(t.shapes[0].core, Core::Seg(Pt { x: 1_000_000, y: 2_000_000 }, Pt { x: 3_500_000, y: 2_000_000 }));
        assert_eq!(t.shapes[0].r, 100_000);
        assert_eq!(t.net, b.net_ids["N"]);
        assert_eq!(t.layers, 1);
        let via = b.items.iter().find(|i| i.kind == Kind::Via).unwrap();
        assert_eq!(via.layers, 0b111, "F.Cu to B.Cu spans every copper layer");
        assert_eq!(via.shapes[0].r, 300_000);
        assert_eq!(b.items.iter().filter(|i| i.kind == Kind::Hole).count(), 3, "one hole per copper layer");
    }

    #[test]
    fn edge_graphics_and_unsupported_are_listed() {
        let b = load(K10).unwrap();
        let e = b.items.iter().find(|i| i.kind == Kind::Edge).unwrap();
        assert_eq!(e.layers, 0);
        assert_eq!(e.shapes.len(), 1);
        assert!(b.items.iter().all(|i| i.kind != Kind::Graphic), "silk is not copper");
        assert!(load("(kicad_pcb (layers (0 \"F.Cu\" signal)) (segment (start x 1) (end 2 2) (width 0.2) (layer \"F.Cu\")))").is_err());
    }
}
