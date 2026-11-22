//! Path checks and the slack raster (K12). `check_path` ports the non-clearance checks of H.probe_prims (continuity,
//! 45/90 angles, zero length, width and via floors) and adds clearance / crossing / hole hits through the K9 engine.
//! Flagging threshold: a margin below -EPS (3e-4 mm) is a violation, as in probe_prims. The raster is the signed
//! distance from each cell centre to the foreign copper of a layer, minus the required gap (no track half width).

use crate::board::{Item, Kind};
use crate::clear::{self, Ctx};
use crate::geom::{self, Core, Pt, Shape};
use serde_json::{json, Value};
use std::collections::HashSet;

pub const EPS_NM: f64 = 300.0;     // 3e-4 mm, as hrgeom.EPS
const CONT_NM: f64 = 100.0;

#[derive(Clone, Debug)]
pub enum Prim {
    Seg { layer: String, a: Pt, b: Pt },
    Via { at: Pt },
}

fn pt_of(v: &Value) -> Option<Pt> {
    let a = v.as_array().filter(|a| a.len() == 2)?;
    Some(Pt { x: (a[0].as_f64()? * 1e6).round() as i64, y: (a[1].as_f64()? * 1e6).round() as i64 })
}

pub fn parse_prims(v: &Value) -> Result<Vec<Prim>, String> {
    let arr = v.as_array().ok_or("prims must be a list")?;
    arr.iter().map(|p| match p.get(0).and_then(Value::as_str) {
        Some("seg") => Ok(Prim::Seg {
            layer: p.get(1).and_then(Value::as_str).ok_or("seg layer")?.to_string(),
            a: pt_of(&p[2]).ok_or("seg start")?,
            b: pt_of(&p[3]).ok_or("seg end")?,
        }),
        Some("via") => Ok(Prim::Via { at: pt_of(&p[1]).ok_or("via at")? }),
        _ => Err("prim must be seg or via".into()),
    }).collect()
}

fn dist(a: Pt, b: Pt) -> f64 {
    ((a.x - b.x) as f64).hypot((a.y - b.y) as f64)
}

fn mm(v: f64) -> f64 {
    v / 1e6
}

/// Non-clearance and clearance violations of a path for `net`. Returns the probe-style dict.
pub fn check_path(cx: &Ctx, prims: &[Prim], net: u32, width: i64, via: Option<(i64, i64)>, skip: &HashSet<u32>,
                  any_angle: bool) -> Value {
    let b = cx.board;
    let f = &cx.rules.floors;
    let rule = cx.rules.net_rule(b.net_name(net));
    let (vd, vdr) = via.unwrap_or((rule.via_dia, rule.via_drill));
    let mut viol: Vec<Value> = Vec::new();
    if width < f.track {
        viol.push(json!({"kind": "width_below_floor", "need": mm(f.track as f64), "have": mm(width as f64)}));
    }
    if prims.iter().any(|p| matches!(p, Prim::Via { .. })) && (vd < f.via_dia || vdr < f.via_drill) {
        viol.push(json!({"kind": "via_below_floor"}));
    }
    // continuity (probe_prims)
    let mut prev_end: Option<Pt> = None;
    let mut prev_layer = String::new();
    for (i, p) in prims.iter().enumerate() {
        match p {
            Prim::Seg { layer, a, b: z } => {
                if let Some(pe) = prev_end {
                    let prev_is_via = i > 0 && matches!(prims[i - 1], Prim::Via { .. });
                    if prev_layer == *layer && dist(pe, *a) > CONT_NM && !prev_is_via {
                        viol.push(json!({"kind": "discontinuity", "i": i}));
                    }
                }
                prev_end = Some(*z);
                prev_layer = layer.clone();
            }
            Prim::Via { at } => {
                if let Some(pe) = prev_end {
                    if dist(pe, *at) > CONT_NM {
                        viol.push(json!({"kind": "discontinuity", "i": i}));
                    }
                }
                prev_end = Some(*at);
                if let Some(Prim::Seg { a, .. }) = prims.get(i + 1) {
                    if dist(*at, *a) > CONT_NM {
                        viol.push(json!({"kind": "discontinuity", "i": i}));
                    }
                }
            }
        }
    }
    // zero length and angle
    for (i, p) in prims.iter().enumerate() {
        let Prim::Seg { a, b: z, layer } = p else { continue };
        let len = dist(*a, *z);
        if len < 1.0 {
            viol.push(json!({"kind": "zero_length", "i": i}));
        } else if !any_angle && len >= 100_000.0 {
            let ang = ((z.y - a.y) as f64).atan2((z.x - a.x) as f64).to_degrees().rem_euclid(45.0);
            if ang.min(45.0 - ang) > 0.01 {
                viol.push(json!({"kind": "angle_not_45_90", "i": i}));
            }
        }
        // clearance of this segment against the board (copper of other nets, edges, keepouts, holes)
        let Some(bit) = b.layer_bit(layer) else { continue };
        let t = Item::new(Kind::Track, String::new(), net, 1u64 << bit,
                          vec![Shape { core: Core::Seg(*a, *z), r: width / 2 }]);
        let m = width / 2 + 1_500_000;
        let bb = [a.x.min(z.x) - m, a.y.min(z.y) - m, a.x.max(z.x) + m, a.y.max(z.y) + m];
        probe(cx, &t, i, skip, bb, (1u128 << bit) | (1u128 << crate::index::NOCOPPER), &mut viol);
    }
    // a segment end must sit on the centre of an own-net via it touches (probe_prims: 0.5 um tolerance)
    for (i, p) in prims.iter().enumerate() {
        let Prim::Seg { layer, a, b: z } = p else { continue };
        let Some(bit) = b.layer_bit(layer) else { continue };
        for end in [*a, *z] {
            let bb = [end.x - 500_000, end.y - 500_000, end.x + 500_000, end.y + 500_000];
            for j in b.index.query(bb, 1u128 << bit) {
                let o = &b.items[j as usize];
                if o.kind == Kind::Via && o.net == net && o.alive {
                    let c = o.shapes[0].core.clone();
                    let Core::Point(c) = c else { continue };
                    let d = dist(end, c);
                    if d > 500.0 && d < o.shapes[0].r as f64 {
                        viol.push(json!({"kind": "track_not_centered_on_via", "i": i, "via_at": [mm(c.x as f64), mm(c.y as f64)], "off_mm": mm(d)}));
                    }
                }
            }
            for (k, q) in prims.iter().enumerate() {
                if let Prim::Via { at } = q {
                    let d = dist(end, *at);
                    if d > 500.0 && d < vd as f64 / 2.0 {
                        viol.push(json!({"kind": "track_not_centered_on_via", "i": i, "via_prim": k, "off_mm": mm(d)}));
                    }
                }
            }
        }
    }
    // via clearance: every copper layer, and the first-layer hole
    for (i, p) in prims.iter().enumerate() {
        let Prim::Via { at } = p else { continue };
        for k in 0..b.copper.len() {
            let t = Item::new(Kind::Via, String::new(), net, 1u64 << k, vec![Shape { core: Core::Point(*at), r: vd / 2 }]);
            let m = vd / 2 + 1_500_000;
            let bb = [at.x - m, at.y - m, at.x + m, at.y + m];
            probe(cx, &t, i, skip, bb, (1u128 << k) | (1u128 << crate::index::NOCOPPER), &mut viol);
        }
    }
    let ok = viol.is_empty();
    json!({"ok": ok, "violations": viol})
}

/// Pushes a violation for each foreign obstacle whose margin is below -EPS.
fn probe(cx: &Ctx, t: &Item, i: usize, skip: &HashSet<u32>, bb: [i64; 4], mask: u128, viol: &mut Vec<Value>) {
    let b = cx.board;
    let layer = b.layer_names(t.layers).first().map(|s| s.to_string()).unwrap_or_default();
    for j in b.index.query(bb, mask) {
        let o = &b.items[j as usize];
        if !o.alive || skip.contains(&o.id) || (o.net != 0 && o.net == t.net) {
            continue;
        }
        let gap = geom::gap_sets(&t.shapes, &o.shapes);
        // geom distances are nm already
        let (g_nm, n_nm) = (gap, if o.kind == Kind::Keepout { 0.0 } else { clear::need(cx, t, o, &layer).nm as f64 });
        let margin_nm = g_nm - n_nm;
        if margin_nm < -EPS_NM {
            let kind = if gap <= 0.0 && matches!(o.kind, Kind::Track | Kind::Arc | Kind::Via | Kind::Pad) {
                "crossing"
            } else {
                match o.kind {
                    Kind::Edge => "edge_hit",
                    Kind::Keepout => "ko_hit",
                    _ => "clearance",
                }
            };
            viol.push(json!({"kind": kind, "i": i, "with": o.id, "item_kind": o.kind.name(),
                             "gap": mm(g_nm), "need": mm(n_nm), "margin": mm(margin_nm)}));
        }
    }
}

/// Signed distance from p to a shape set (negative inside polygons), minimum over shapes.
fn sd(p: Pt, shapes: &[Shape]) -> f64 {
    shapes.iter().map(|s| {
        let d = match &s.core {
            Core::Point(q) => dist(p, *q),
            Core::Seg(a, z) => geom::pt_seg(p, *a, *z),
            Core::Poly(v) => {
                let e = (0..v.len()).map(|i| geom::pt_seg(p, v[i], v[(i + 1) % v.len()])).fold(f64::INFINITY, f64::min);
                if geom::pt_in_poly(p, v) { -e } else { e }
            }
        };
        d - s.r as f64
    }).fold(f64::INFINITY, f64::min)
}

/// Slack raster on the legacy grid (hrgeom.grid): samples at x0 + i*pitch, y0 + j*pitch, both ends included, row-major
/// (y outer). Value = min over foreign obstacles of (signed distance - required gap); -9 outside the board outline
/// (even-odd over the Edge.Cuts segments); 9 where nothing is near.
pub fn slack_field(cx: &Ctx, bbox: [i64; 4], pitch: i64, layer: u32, net: u32, kind: Kind) -> (usize, usize, Vec<f32>) {
    let b = cx.board;
    let nx = ((bbox[2] - bbox[0]) / pitch + 1).max(1) as usize;
    let ny = ((bbox[3] - bbox[1]) / pitch + 1).max(1) as usize;
    let layer_name = b.copper.get(layer as usize).cloned().unwrap_or_default();
    let probe_item = Item::new(kind, String::new(), net, 1u64 << layer,
                               vec![Shape { core: Core::Point(Pt { x: 0, y: 0 }), r: 0 }]);
    let edges: Vec<(Pt, Pt)> = b.items.iter().filter(|i| i.alive && i.kind == Kind::Edge)
        .filter_map(|i| match i.shapes.first()?.core { Core::Seg(a, z) => Some((a, z)), _ => None }).collect();
    let mut out = vec![9.0f32; nx * ny];
    let obs: Vec<usize> = b.index.query(bbox, 1u128 << layer)
        .into_iter().map(|j| j as usize)
        .filter(|&j| b.items[j].alive && b.items[j].kind != Kind::Edge && !(b.items[j].net != 0 && b.items[j].net == net))
        .collect();
    let needs: Vec<f64> = obs.iter().map(|&j| {
        let o = &b.items[j];
        if o.kind == Kind::Keepout { 0.0 } else { clear::need(cx, &probe_item, o, &layer_name).nm as f64 }
    }).collect();
    for jy in 0..ny {
        let y = (bbox[1] + jy as i64 * pitch) as f64;
        for jx in 0..nx {
            let x = (bbox[0] + jx as i64 * pitch) as f64;
            let mut inside = false;
            for (a, z) in &edges {
                let (ay, by) = (a.y as f64, z.y as f64);
                if ay != by && ((ay > y) != (by > y)) && x < (z.x - a.x) as f64 * (y - ay) / (by - ay) + a.x as f64 {
                    inside = !inside;
                }
            }
            if !inside {
                out[jy * nx + jx] = -9.0;
                continue;
            }
            let p = Pt { x: x as i64, y: y as i64 };
            let mut best = f64::INFINITY;
            for (k, &j) in obs.iter().enumerate() {
                best = best.min(sd(p, &b.items[j].shapes) - needs[k]);
            }
            out[jy * nx + jx] = if best.is_finite() { (best / 1e6) as f32 } else { 9.0 };
        }
    }
    (nx, ny, out)
}
