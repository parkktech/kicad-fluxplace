//! Shapes and distances in integer nm. Distances are f64 (the only float path); coordinates stay i64.
//! A shape is a core (point, segment or polygon) inflated by r. Gap of two shapes = core distance - r1 - r2.
//! Rotation follows boardfile._rot: positive angle is counter-clockwise on a y-down screen.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Pt {
    pub x: i64,
    pub y: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Core {
    Point(Pt),
    Seg(Pt, Pt),
    Poly(Vec<Pt>),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Shape {
    pub core: Core,
    pub r: i64,
}

fn diff(a: Pt, b: Pt) -> (f64, f64) {
    ((a.x - b.x) as f64, (a.y - b.y) as f64)
}

fn cross(o: Pt, a: Pt, b: Pt) -> i128 {
    let (ax, ay) = ((a.x - o.x) as i128, (a.y - o.y) as i128);
    let (bx, by) = ((b.x - o.x) as i128, (b.y - o.y) as i128);
    ax * by - ay * bx
}

fn on_box(p: Pt, a: Pt, b: Pt) -> bool {
    p.x >= a.x.min(b.x) && p.x <= a.x.max(b.x) && p.y >= a.y.min(b.y) && p.y <= a.y.max(b.y)
}

/// Distance from point p to segment ab (a == b gives the point distance).
pub fn pt_seg(p: Pt, a: Pt, b: Pt) -> f64 {
    let (dx, dy) = diff(b, a);
    let (px, py) = diff(p, a);
    let len2 = dx * dx + dy * dy;
    if len2 == 0.0 {
        return px.hypot(py);
    }
    let t = ((px * dx + py * dy) / len2).clamp(0.0, 1.0);
    (px - t * dx).hypot(py - t * dy)
}

/// True when segments ab and cd share a point (touching counts).
pub fn seg_intersect(a: Pt, b: Pt, c: Pt, d: Pt) -> bool {
    let (d1, d2) = (cross(a, b, c).signum(), cross(a, b, d).signum());
    let (d3, d4) = (cross(c, d, a).signum(), cross(c, d, b).signum());
    if d1 * d2 < 0 && d3 * d4 < 0 {
        return true;
    }
    (d1 == 0 && on_box(c, a, b)) || (d2 == 0 && on_box(d, a, b))
        || (d3 == 0 && on_box(a, c, d)) || (d4 == 0 && on_box(b, c, d))
}

pub fn seg_seg(a: Pt, b: Pt, c: Pt, d: Pt) -> f64 {
    if seg_intersect(a, b, c, d) {
        return 0.0;
    }
    pt_seg(a, c, d).min(pt_seg(b, c, d)).min(pt_seg(c, a, b)).min(pt_seg(d, a, b))
}

/// Inside or on the boundary of a simple polygon (even-odd ray cast; the crossing test is exact in i128).
pub fn pt_in_poly(p: Pt, poly: &[Pt]) -> bool {
    let n = poly.len();
    if n < 3 {
        return false;
    }
    let mut inside = false;
    for i in 0..n {
        let (a, b) = (poly[i], poly[(i + 1) % n]);
        if cross(a, b, p) == 0 && on_box(p, a, b) {
            return true;
        }
        if (a.y > p.y) != (b.y > p.y) {
            // p lies left of the edge's crossing point <=> the ray crosses this edge
            let lhs = (p.x - a.x) as i128 * (b.y - a.y) as i128;
            let rhs = (p.y - a.y) as i128 * (b.x - a.x) as i128;
            if (b.y > a.y) == (lhs < rhs) {
                inside = !inside;
            }
        }
    }
    inside
}

fn edges(poly: &[Pt]) -> impl Iterator<Item = (Pt, Pt)> + '_ {
    let n = poly.len();
    (0..n).map(move |i| (poly[i], poly[(i + 1) % n]))
}

fn seg_poly(a: Pt, b: Pt, poly: &[Pt]) -> f64 {
    if pt_in_poly(a, poly) || pt_in_poly(b, poly) {
        return 0.0;
    }
    edges(poly).map(|(c, d)| seg_seg(a, b, c, d)).fold(f64::INFINITY, f64::min)
}

fn poly_poly(p: &[Pt], q: &[Pt]) -> f64 {
    if p.iter().any(|&v| pt_in_poly(v, q)) || q.iter().any(|&v| pt_in_poly(v, p)) {
        return 0.0;
    }
    let mut best = f64::INFINITY;
    for (a, b) in edges(p) {
        for (c, d) in edges(q) {
            best = best.min(seg_seg(a, b, c, d));
        }
    }
    best
}

/// Distance between two cores; 0 when they touch or overlap.
pub fn core_dist(a: &Core, b: &Core) -> f64 {
    match (a, b) {
        (Core::Poly(p), Core::Poly(q)) => poly_poly(p, q),
        (Core::Poly(p), other) | (other, Core::Poly(p)) => {
            let (s, e) = seg_ends(other);
            seg_poly(s, e, p)
        }
        (x, y) => {
            let ((s, e), (t, f)) = (seg_ends(x), seg_ends(y));
            seg_seg(s, e, t, f)
        }
    }
}

fn seg_ends(c: &Core) -> (Pt, Pt) {
    match c {
        Core::Point(p) => (*p, *p),
        Core::Seg(p, q) => (*p, *q),
        Core::Poly(v) => (v[0], v[0]),
    }
}

/// Signed gap of two shapes: core distance minus both inflation radii (negative when the copper overlaps).
pub fn gap(a: &Shape, b: &Shape) -> f64 {
    core_dist(&a.core, &b.core) - a.r as f64 - b.r as f64
}

/// Smallest gap over two sets of shapes (an item with several shapes, e.g. a custom pad).
pub fn gap_sets(a: &[Shape], b: &[Shape]) -> f64 {
    a.iter().flat_map(|x| b.iter().map(move |y| gap(x, y))).fold(f64::INFINITY, f64::min)
}

/// Polyline approximating the arc start -> mid -> end (through mid). Returns (points, error bound in nm).
/// Points are on the arc to within the bound: a chord's sagitta is at most `max_err_nm`, plus 1 nm of rounding.
/// Start and end are the exact input points. Collinear input gives the straight polyline start, mid, end.
pub fn arc_polyline(s: Pt, m: Pt, e: Pt, max_err_nm: f64) -> (Vec<Pt>, f64) {
    let (ax, ay, bx, by, cx, cy) = (s.x as f64, s.y as f64, m.x as f64, m.y as f64, e.x as f64, e.y as f64);
    let d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by));
    if d.abs() < 1e-9 {
        return (vec![s, m, e], 1.0);
    }
    let a2 = ax * ax + ay * ay;
    let b2 = bx * bx + by * by;
    let c2 = cx * cx + cy * cy;
    let ox = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d;
    let oy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d;
    let r = (ax - ox).hypot(ay - oy);
    let ang = |p: Pt| (p.y as f64 - oy).atan2(p.x as f64 - ox);
    let tau = std::f64::consts::TAU;
    let norm = |v: f64| v.rem_euclid(tau);
    let (ts, tm, te) = (ang(s), ang(m), ang(e));
    let ccw = norm(te - ts);
    let sweep = if norm(tm - ts) < ccw { ccw } else { ccw - tau };
    let step = if max_err_nm >= r { std::f64::consts::FRAC_PI_2 } else { 2.0 * (1.0 - max_err_nm / r).acos() };
    let n = ((sweep.abs() / step).ceil() as usize).max(1);
    let mut pts = vec![s];
    for k in 1..n {
        let t = ts + sweep * k as f64 / n as f64;
        pts.push(Pt { x: (ox + r * t.cos()).round() as i64, y: (oy + r * t.sin()).round() as i64 });
    }
    pts.push(e);
    (pts, max_err_nm + 1.0)
}

/// Rotate about the origin: exact for multiples of 90 degrees, rounded to nm otherwise.
pub fn rotate(p: Pt, deg: f64) -> Pt {
    let q = deg.rem_euclid(360.0);
    if q.fract() == 0.0 && (q as i64) % 90 == 0 {
        return match (q as i64) / 90 {
            0 => p,
            1 => Pt { x: p.y, y: -p.x },
            2 => Pt { x: -p.x, y: -p.y },
            _ => Pt { x: -p.y, y: p.x },
        };
    }
    let a = deg.to_radians();
    let (c, s) = (a.cos(), a.sin());
    let (x, y) = (p.x as f64, p.y as f64);
    Pt { x: (x * c + y * s).round() as i64, y: (-x * s + y * c).round() as i64 }
}

/// Axis-aligned roundrect centred at c: the inner rectangle (shrunk by rr) with inflation rr. Exact.
pub fn roundrect(c: Pt, w: i64, h: i64, rr: i64) -> Result<Shape, String> {
    if rr < 0 || 2 * rr > w || 2 * rr > h {
        return Err(format!("roundrect radius {rr} does not fit {w} x {h}"));
    }
    let (hx, hy) = (w / 2 - rr, h / 2 - rr);
    let v = vec![
        Pt { x: c.x - hx, y: c.y - hy },
        Pt { x: c.x + hx, y: c.y - hy },
        Pt { x: c.x + hx, y: c.y + hy },
        Pt { x: c.x - hx, y: c.y + hy },
    ];
    Ok(Shape { core: Core::Poly(v), r: rr })
}

/// Axis-aligned rectangle with corners cut by (cx, cy): an 8-vertex polygon. Exact.
pub fn chamfered_rect(c: Pt, w: i64, h: i64, cx: i64, cy: i64) -> Result<Vec<Pt>, String> {
    let (hx, hy) = (w / 2, h / 2);
    if cx < 0 || cy < 0 || cx > w || cy > h {
        return Err(format!("chamfer {cx} x {cy} does not fit {w} x {h}"));
    }
    let p = |dx: i64, dy: i64| Pt { x: c.x + dx, y: c.y + dy };
    Ok(vec![
        p(-hx + cx, -hy), p(hx - cx, -hy), p(hx, -hy + cy), p(hx, hy - cy),
        p(hx - cx, hy), p(-hx + cx, hy), p(-hx, hy - cy), p(-hx, -hy + cy),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(x: i64, y: i64) -> Pt {
        Pt { x, y }
    }

    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
        fn coord(&mut self) -> i64 {
            (self.next() % 1_000_001) as i64
        }
    }

    /// Oracle: distance from segment ab to segment cd by sampling ab (2000 points, step 0.5 um). The sampled
    /// minimum is within half a step of the true one, so the tolerance is 2 um.
    fn sampled_seg_seg(a: Pt, b: Pt, c: Pt, d: Pt) -> f64 {
        (0..=2000).map(|i| {
            let t = i as f64 / 2000.0;
            let q = p((a.x as f64 + t * (b.x - a.x) as f64).round() as i64,
                      (a.y as f64 + t * (b.y - a.y) as f64).round() as i64);
            pt_seg(q, c, d)
        }).fold(f64::INFINITY, f64::min)
    }

    fn in_tri(q: Pt, t: &[Pt; 3]) -> bool {
        let s: Vec<i128> = (0..3).map(|i| cross(t[i], t[(i + 1) % 3], q)).collect();
        (s.iter().all(|&v| v >= 0)) || (s.iter().all(|&v| v <= 0))
    }

    #[test]
    fn hand_values() {
        assert_eq!(pt_seg(p(5, 5), p(0, 0), p(10, 0)), 5.0);
        assert_eq!(pt_seg(p(13, 4), p(0, 0), p(10, 0)), 5.0);
        assert_eq!(seg_seg(p(0, 0), p(10, 10), p(0, 10), p(10, 0)), 0.0);
        assert_eq!(seg_seg(p(0, 0), p(10, 0), p(0, 3), p(10, 3)), 3.0);
        let sq = vec![p(0, 0), p(10, 0), p(10, 10), p(0, 10)];
        assert_eq!(seg_poly(p(2, 2), p(3, 3), &sq), 0.0);
        assert_eq!(poly_poly(&sq, &[p(12, 0), p(20, 0), p(20, 10)]), 2.0);
        assert_eq!(poly_poly(&sq, &[p(5, 5), p(20, 5), p(20, 20)]), 0.0);
    }

    #[test]
    fn point_in_polygon_inside_outside_boundary() {
        let sq = vec![p(0, 0), p(10, 0), p(10, 10), p(0, 10)];
        assert!(pt_in_poly(p(5, 5), &sq) && pt_in_poly(p(0, 5), &sq) && pt_in_poly(p(10, 10), &sq));
        assert!(!pt_in_poly(p(11, 5), &sq) && !pt_in_poly(p(5, -1), &sq));
    }

    #[test]
    fn random_segment_pairs_match_sampled_oracle() {
        let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
        for _ in 0..300 {
            let (a, b, c, d) = (p(rng.coord(), rng.coord()), p(rng.coord(), rng.coord()),
                                p(rng.coord(), rng.coord()), p(rng.coord(), rng.coord()));
            let exact = seg_seg(a, b, c, d);
            let oracle = sampled_seg_seg(a, b, c, d);
            assert!((exact - oracle).abs() < 2000.0, "{exact} vs {oracle}");
        }
    }

    #[test]
    fn random_triangle_pairs_match_sampled_oracle() {
        let mut rng = Rng(0x1234_5678_9abc_def1);
        for _ in 0..200 {
            let t: [Pt; 3] = [p(rng.coord(), rng.coord()), p(rng.coord(), rng.coord()), p(rng.coord(), rng.coord())];
            let u: [Pt; 3] = [p(rng.coord(), rng.coord()), p(rng.coord(), rng.coord()), p(rng.coord(), rng.coord())];
            let exact = poly_poly(&t, &u);
            let mut oracle = f64::INFINITY;
            for (x, y) in [(t, u), (u, t)] {
                for i in 0..3 {
                    for k in 0..=700 {
                        let s = k as f64 / 700.0;
                        let (a, b) = (x[i], x[(i + 1) % 3]);
                        let q = p((a.x as f64 + s * (b.x - a.x) as f64).round() as i64,
                                  (a.y as f64 + s * (b.y - a.y) as f64).round() as i64);
                        let d = if in_tri(q, &y) { 0.0 } else {
                            (0..3).map(|j| pt_seg(q, y[j], y[(j + 1) % 3])).fold(f64::INFINITY, f64::min)
                        };
                        oracle = oracle.min(d);
                    }
                }
            }
            assert!((exact - oracle).abs() < 2000.0, "{exact} vs {oracle}");
        }
    }

    #[test]
    fn arc_polyline_holds_its_error_bound() {
        let r = 10_000_000.0;
        // a half circle through the top: start (R,0), mid (0,R), end (-R,0)
        let (s, m, e) = (p(10_000_000, 0), p(0, 10_000_000), p(-10_000_000, 0));
        let (pts, bound) = arc_polyline(s, m, e, 1000.0);
        assert_eq!(pts[0], s);
        assert_eq!(*pts.last().unwrap(), e);
        assert!(pts.len() > 10);
        for k in 0..=4000 {
            let t = std::f64::consts::PI * k as f64 / 4000.0;
            let q = p((r * t.cos()).round() as i64, (r * t.sin()).round() as i64);
            let d = pts.windows(2).map(|w| pt_seg(q, w[0], w[1])).fold(f64::INFINITY, f64::min);
            assert!(d <= bound, "arc point {k}: {d} > {bound}");
        }
        // collinear input is a straight polyline
        assert_eq!(arc_polyline(p(0, 0), p(5, 0), p(10, 0), 1000.0).0.len(), 3);
    }

    #[test]
    fn roundrect_gap_matches_analytic_within_1nm() {
        // axis-aligned: inner rects [-0.8,0.8] and [2.6,3.4] mm, radii 0.2 and 0.1 mm -> gap 1.5 mm
        let a = roundrect(p(0, 0), 2_000_000, 1_000_000, 200_000).unwrap();
        let b = roundrect(p(3_000_000, 0), 1_000_000, 1_000_000, 100_000).unwrap();
        assert!((gap(&a, &b) - 1_500_000.0).abs() <= 1.0, "{}", gap(&a, &b));
        // diagonal: corner (0.8, 0.3) to corner (2.6, 1.6) mm, minus both radii
        let c = roundrect(p(3_000_000, 2_000_000), 1_000_000, 1_000_000, 100_000).unwrap();
        let analytic = (1_800_000f64.powi(2) + 1_300_000f64.powi(2)).sqrt() - 200_000.0 - 100_000.0;
        assert!((gap(&a, &c) - analytic).abs() <= 1.0, "{} vs {analytic}", gap(&a, &c));
        assert!(roundrect(p(0, 0), 100, 100, 60).is_err());
    }

    #[test]
    fn rotations_are_exact_on_quarter_turns() {
        assert_eq!(rotate(p(1, 0), 90.0), p(0, -1));
        assert_eq!(rotate(p(1, 2), 180.0), p(-1, -2));
        assert_eq!(rotate(p(1, 2), 270.0), p(-2, 1));
        assert_eq!(rotate(p(1, 2), -90.0), rotate(p(1, 2), 270.0));
        let g = rotate(p(1_000_000, 0), 37.0);
        assert!((g.x as f64 - 1e6 * 37f64.to_radians().cos()).abs() <= 1.0);
        assert!((g.y as f64 + 1e6 * 37f64.to_radians().sin()).abs() <= 1.0);
    }

    #[test]
    fn chamfer_and_gap_sets() {
        let v = chamfered_rect(p(0, 0), 2_000_000, 1_000_000, 100_000, 100_000).unwrap();
        assert_eq!(v.len(), 8);
        assert!(chamfered_rect(p(0, 0), 10, 10, 20, 1).is_err());
        let a = [Shape { core: Core::Point(p(0, 0)), r: 1 }, Shape { core: Core::Point(p(100, 0)), r: 1 }];
        let b = [Shape { core: Core::Point(p(50, 0)), r: 1 }];
        assert_eq!(gap_sets(&a, &b), 48.0);
    }
}
