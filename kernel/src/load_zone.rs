//! Board zones (K5): copper zones with their stored fills per layer, keepout zones (one Keepout item per polygon
//! with the keepout flags), and the fill_stale flag. The file's fills are used as stored; fill_stale tells the
//! caller that a foreign track, via or pad overlaps a stored fill, so a refill is needed (KModel, K15).

use crate::board::{Board, Item, Kind, Zone};
use crate::geom::{self, Core, Pt, Shape};
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
            let no = |f: &str| flags.get(f).and_then(|v| v.as_str()) == Some("not_allowed");
            it.extra = serde_json::json!({"keepout": flags, "name": cx.str_field(n, "name").unwrap_or(""),
                "no_tracks": no("tracks"), "no_vias": no("vias"), "no_pads": no("pads")});
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

const BAND: i64 = 1_000_000;

/// Edges of one polygon bucketed into horizontal bands of BAND nm: a ray or a segment query touches only its rows.
pub(crate) struct PolyBands {
    y0: i64,
    rows: Vec<Vec<(Pt, Pt)>>,
}

impl PolyBands {
    pub(crate) fn new(poly: &[Pt]) -> PolyBands {
        let n = poly.len();
        if n < 3 {
            return PolyBands { y0: 0, rows: Vec::new() };
        }
        let y0 = poly.iter().map(|p| p.y).min().unwrap_or(0);
        let y1 = poly.iter().map(|p| p.y).max().unwrap_or(0);
        let mut rows = vec![Vec::new(); ((y1 - y0) / BAND + 1) as usize];
        for i in 0..n {
            let (a, b) = (poly[i], poly[(i + 1) % n]);
            let (lo, hi) = (a.y.min(b.y), a.y.max(b.y));
            for r in ((lo - y0) / BAND)..=((hi - y0) / BAND) {
                rows[r as usize].push((a, b));
            }
        }
        PolyBands { y0, rows }
    }

    fn row_range(&self, lo: i64, hi: i64) -> std::ops::RangeInclusive<usize> {
        let last = self.rows.len().saturating_sub(1) as i64;
        let r0 = ((lo - self.y0).div_euclid(BAND)).clamp(0, last);
        let r1 = ((hi - self.y0).div_euclid(BAND)).clamp(0, last);
        r0 as usize..=r1 as usize
    }

    /// Inside or on the boundary (even-odd ray cast along +x over the edges of p's row only).
    pub(crate) fn contains(&self, p: Pt) -> bool {
        if self.rows.is_empty() || p.y < self.y0 || (p.y - self.y0) / BAND >= self.rows.len() as i64 {
            return false;
        }
        let row = &self.rows[((p.y - self.y0) / BAND) as usize];
        let mut inside = false;
        for &(a, b) in row {
            if geom::pt_seg(p, a, b) == 0.0 {
                return true;
            }
            if (a.y > p.y) != (b.y > p.y) {
                let lhs = (p.x - a.x) as i128 * (b.y - a.y) as i128;
                let rhs = (p.y - a.y) as i128 * (b.x - a.x) as i128;
                if (b.y > a.y) == (lhs < rhs) {
                    inside = !inside;
                }
            }
        }
        inside
    }

    /// True when the segment ab (a disc of radius r around it) touches this polygon's copper.
    pub(crate) fn near(&self, a: Pt, b: Pt, r: i64) -> bool {
        if self.contains(a) {
            return true;
        }
        let (xl, xh) = (a.x.min(b.x) - r, a.x.max(b.x) + r);
        let (yl, yh) = (a.y.min(b.y) - r, a.y.max(b.y) + r);
        if self.rows.is_empty() {
            return false;
        }
        for row in self.row_range(yl, yh) {
            for &(e0, e1) in &self.rows[row] {
                let (ex0, ex1) = (e0.x.min(e1.x), e0.x.max(e1.x));
                if ex1 < xl || ex0 > xh {
                    continue;
                }
                if geom::seg_seg(a, b, e0, e1) <= r as f64 {
                    return true;
                }
            }
        }
        false
    }
}

/// True when a foreign-net track, arc, via or pad overlaps a stored fill on its layer (the fill is stale).
/// Copper that encloses a whole fill island is caught by a fill vertex inside the copper.
pub(crate) fn fill_stale(b: &Board) -> bool {
    for z in &b.zones {
        for (bit, poly) in &z.fills {
            if poly.len() < 3 {
                continue;
            }
            let bands = PolyBands::new(poly);
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
                for s in &it.shapes {
                    let hit = match &s.core {
                        Core::Point(p) => bands.near(*p, *p, s.r),
                        Core::Seg(a, c) => bands.near(*a, *c, s.r),
                        Core::Poly(v) => {
                            (0..v.len()).any(|i| bands.near(v[i], v[(i + 1) % v.len()], s.r))
                                || geom::pt_in_poly(poly[0], v)
                        }
                    };
                    if hit {
                        return true;
                    }
                }
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
        fn range(&mut self, lo: i64, hi: i64) -> i64 {
            lo + (self.next() % (hi - lo) as u64) as i64
        }
    }

    /// Star polygon with 50 random radii (nm) around (0, 0); always simple (angles increase).
    fn star(rng: &mut Rng) -> Vec<Pt> {
        (0..50).map(|i| {
            let a = i as f64 * std::f64::consts::TAU / 50.0;
            let r = rng.range(1_000_000, 20_000_000) as f64;
            Pt { x: (r * a.cos()).round() as i64, y: (r * a.sin()).round() as i64 }
        }).collect()
    }

    #[test]
    fn bands_contains_matches_point_in_polygon() {
        let mut rng = Rng(0x2545_f491_4f6c_dd1d);
        for _ in 0..20 {
            let poly = star(&mut rng);
            let bands = PolyBands::new(&poly);
            for _ in 0..2000 {
                let p = Pt { x: rng.range(-25_000_000, 25_000_000), y: rng.range(-25_000_000, 25_000_000) };
                assert_eq!(bands.contains(p), geom::pt_in_poly(p, &poly), "point {p:?}");
            }
        }
    }

    #[test]
    fn bands_near_matches_core_distance() {
        let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
        for _ in 0..20 {
            let poly = star(&mut rng);
            let bands = PolyBands::new(&poly);
            for _ in 0..25 {
                let a = Pt { x: rng.range(-25_000_000, 25_000_000), y: rng.range(-25_000_000, 25_000_000) };
                let b = Pt { x: a.x + rng.range(-3_000_000, 3_000_000), y: a.y + rng.range(-3_000_000, 3_000_000) };
                let r = rng.range(0, 200_000);
                let want = geom::core_dist(&Core::Poly(poly.clone()), &Core::Seg(a, b)) - r as f64 <= 0.0;
                assert_eq!(bands.near(a, b, r), want, "seg {a:?} {b:?} r {r}");
            }
        }
    }
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
