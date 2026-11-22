//! Board zones (K5): copper zones with their stored fills per layer, keepout zones (one Keepout item per polygon
//! with the keepout flags), and the fill_stale flag. The file's fills are used as stored; fill_stale tells the
//! caller that a foreign track, via or pad overlaps a stored fill, so a refill is needed (KModel, K15).

use crate::board::{Board, Item, Kind, Zone};
use crate::geom::{core_dist, Core, Pt, Shape};
use crate::load::Ctx;
use crate::load_fp::{layer_names, mask_of, xy_list};
use crate::sexpr::Node;

fn int_field(n: &Node, tag: &str) -> Option<i64> {
    n.first(tag).and_then(|c| c.str_at(1)).and_then(|s| s.parse().ok())
}

pub(crate) fn load_zone(cx: &Ctx, b: &mut Board, n: &Node) -> Result<(), String> {
    let uuid = cx.uuid(b, n);
    let names = layer_names(n);
    let names = if names.is_empty() { cx.str_field(n, "layer").map(|l| vec![l.to_string()]).unwrap_or_default() } else { names };
    let mask = mask_of(b, &names);
    let net = cx.net(b, n)?;
    if let Some(k) = n.first("keepout") {
        let flags: serde_json::Map<String, serde_json::Value> = ["tracks", "vias", "pads", "copperpour", "footprints"]
            .iter().map(|f| (f.to_string(), serde_json::Value::String(
                k.first(f).and_then(|c| c.str_at(1)).unwrap_or("allowed").to_string()))).collect();
        for poly in n.kids("polygon") {
            let pts = xy_list(cx, poly.first("pts").unwrap_or(poly))?;
            if pts.len() < 3 {
                continue;
            }
            let mut it = Item::new(Kind::Keepout, uuid.clone(), 0, mask, vec![Shape { core: Core::Poly(pts), r: 0 }]);
            it.extra = serde_json::json!({"keepout": flags, "name": cx.str_field(n, "name").unwrap_or("")});
            b.add_item(it);
        }
        return Ok(());
    }
    let outline = match n.first("polygon") {
        Some(p) => xy_list(cx, p.first("pts").unwrap_or(p))?,
        None => Vec::new(),
    };
    let mut fills = Vec::new();
    for f in n.kids("filled_polygon") {
        let layer = cx.str_field(f, "layer").unwrap_or("");
        let Some(bit) = b.layer_bit(layer) else { continue };
        let pts = xy_list(cx, f.first("pts").unwrap_or(f))?;
        fills.push((bit, pts));
    }
    let connect = n.first("connect_pads");
    b.zones.push(Zone {
        uuid,
        net,
        layers: mask,
        name: cx.str_field(n, "name").unwrap_or("").to_string(),
        priority: int_field(n, "priority").unwrap_or(0),
        outline,
        clearance: connect.and_then(|c| cx.num_field(c, "clearance").ok().flatten()),
        min_thickness: cx.num_field(n, "min_thickness")?,
        connect: connect.and_then(|c| c.str_at(1)).unwrap_or("").to_string(),
        thermal_gap: n.first("fill").and_then(|f| cx.num_field(f, "thermal_gap").ok().flatten()),
        thermal_width: n.first("fill").and_then(|f| cx.num_field(f, "thermal_bridge_width").ok().flatten()),
        island_mode: n.first("fill").and_then(|f| int_field(f, "island_removal_mode")),
        hatch: n.first("hatch").and_then(|h| h.str_at(1)).map(str::to_string),
        fills,
    });
    Ok(())
}

/// True when a foreign-net track, arc, via or pad overlaps a stored fill on its layer (the fill is stale).
pub(crate) fn fill_stale(b: &Board) -> bool {
    for z in &b.zones {
        for (bit, poly) in &z.fills {
            if poly.len() < 3 {
                continue;
            }
            let fill = Core::Poly(poly.clone());
            let (x0, y0) = (poly.iter().map(|p| p.x).min().unwrap_or(0), poly.iter().map(|p| p.y).min().unwrap_or(0));
            let (x1, y1) = (poly.iter().map(|p| p.x).max().unwrap_or(0), poly.iter().map(|p| p.y).max().unwrap_or(0));
            for it in &b.items {
                let copper = matches!(it.kind, Kind::Track | Kind::Arc | Kind::Via | Kind::Pad);
                if !it.alive || !copper || it.net == z.net || it.layers & (1u64 << bit) == 0 {
                    continue;
                }
                if it.bbox[0] > x1 || it.bbox[2] < x0 || it.bbox[1] > y1 || it.bbox[3] < y0 {
                    continue;
                }
                if it.shapes.iter().any(|s| core_dist(&fill, &s.core) - s.r as f64 <= 0.0) {
                    return true;
                }
            }
        }
    }
    false
}

/// Shoelace area in mm^2 of a polygon in nm.
pub(crate) fn area_mm2(pts: &[Pt]) -> f64 {
    let n = pts.len();
    let s: i128 = (0..n).map(|i| {
        let (a, c) = (pts[i], pts[(i + 1) % n]);
        a.x as i128 * c.y as i128 - c.x as i128 * a.y as i128
    }).sum();
    (s.abs() as f64) / 2.0 / 1e12
}
