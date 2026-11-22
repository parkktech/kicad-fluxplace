//! Connectivity (K11): per-net components of pads, tracks, arcs and vias (touch within 1 um on a common layer) and
//! zone fills (attached when an item anchor lies inside a stored fill). opens(net) = components that hold at least
//! one pad, minus one. Rebuilt from the live items on each query (nets are small); holes do not connect.
//! K13 calibrates whether pad-less clusters count.

use crate::board::{Board, Item, Kind};
use crate::geom::{self, Core, Pt};
use std::collections::HashMap;

pub const TOUCH_NM: f64 = 1000.0;

fn find(par: &mut [usize], mut i: usize) -> usize {
    while par[i] != i {
        par[i] = par[par[i]];
        i = par[i];
    }
    i
}

fn union(par: &mut [usize], i: usize, j: usize) {
    let (a, b) = (find(par, i), find(par, j));
    par[a] = b;
}

fn copper_kind(k: Kind) -> bool {
    matches!(k, Kind::Pad | Kind::Track | Kind::Arc | Kind::Via)
}

/// Probe points of an item for zone-fill membership (legacy _anchors): segment ends, or centre plus vertices.
fn anchors(it: &Item) -> Vec<Pt> {
    let mut v = Vec::new();
    for s in &it.shapes {
        match &s.core {
            Core::Point(p) => v.push(*p),
            Core::Seg(a, b) => {
                v.push(*a);
                v.push(*b);
            }
            Core::Poly(p) => {
                v.extend(p.iter().step_by((p.len() / 8).max(1)));
            }
        }
    }
    v
}

pub struct Comp {
    pub items: Vec<u32>,
    pub pads: usize,
    /// (zone index, fill index) of the fills attached to this component
    pub fills: Vec<(usize, usize)>,
}

/// Connected components of one net (items and attached fills), largest first.
pub fn components(b: &Board, net: u32) -> Vec<Comp> {
    let items: Vec<&Item> = b.items.iter().filter(|i| i.alive && i.net == net && copper_kind(i.kind)).collect();
    let n = items.len();
    let mut par: Vec<usize> = (0..n).collect();
    let mut pos = vec![usize::MAX; b.items.len()];
    for (k, it) in items.iter().enumerate() {
        pos[it.id as usize] = k;
    }
    for (k, a) in items.iter().enumerate() {
        let m = TOUCH_NM as i64;
        let bb = [a.bbox[0] - m, a.bbox[1] - m, a.bbox[2] + m, a.bbox[3] + m];
        // only items on a shared copper layer can touch (checked below): query this item's layers alone
        for j in b.index.query(bb, crate::index::key_bits(a.layers)) {
            let kk = match pos.get(j as usize) {
                Some(&p) if p != usize::MAX => p,
                _ => continue,
            };
            if kk <= k {
                continue;
            }
            let c = items[kk];
            if a.layers & c.layers == 0 {
                continue;
            }
            if geom::gap_sets(&a.shapes, &c.shapes) <= TOUCH_NM {
                union(&mut par, k, kk);
            }
        }
    }
    // zone fills: one node each; attached to every item on its layer with an anchor inside
    let anchors_of: Vec<Vec<Pt>> = items.iter().map(|it| anchors(it)).collect();
    let mut fill_nodes: Vec<(usize, usize, usize)> = Vec::new();
    let mut nodes = n;
    for (zi, z) in b.zones.iter().enumerate() {
        if z.net != net {
            continue;
        }
        for (fi, (bit, poly)) in z.fills.iter().enumerate() {
            if poly.len() < 3 {
                continue;
            }
            let node = nodes;
            nodes += 1;
            par.push(node);
            fill_nodes.push((zi, fi, node));
            let (x0, y0) = (poly.iter().map(|p| p.x).min().unwrap_or(0), poly.iter().map(|p| p.y).min().unwrap_or(0));
            let (x1, y1) = (poly.iter().map(|p| p.x).max().unwrap_or(0), poly.iter().map(|p| p.y).max().unwrap_or(0));
            let bands = crate::load_zone::PolyBands::new(poly);
            for (k, it) in items.iter().enumerate() {
                if it.layers & (1u64 << bit) == 0 || it.bbox[0] > x1 || it.bbox[2] < x0 || it.bbox[1] > y1 || it.bbox[3] < y0 {
                    continue;
                }
                if anchors_of[k].iter().any(|&p| bands.contains(p)) {
                    union(&mut par, k, node);
                }
            }
        }
    }
    let mut groups: HashMap<usize, Comp> = HashMap::new();
    for k in 0..n {
        let r = find(&mut par, k);
        let g = groups.entry(r).or_insert(Comp { items: Vec::new(), pads: 0, fills: Vec::new() });
        g.items.push(items[k].id);
        if items[k].kind == Kind::Pad {
            g.pads += 1;
        }
    }
    for (zi, fi, node) in fill_nodes {
        let r = find(&mut par, node);
        if let Some(g) = groups.get_mut(&r) {
            g.fills.push((zi, fi));
        }
    }
    let mut out: Vec<Comp> = groups.into_values().collect();
    out.sort_by(|a, b| b.items.len().cmp(&a.items.len()).then(a.items.first().cmp(&b.items.first())));
    out
}

/// Opens of a net: every component of the net minus one, pad-less stubs included (kicad-cli counts each as an
/// unconnected item; K14 false accepts on a floating new track). 0 for a net with at most one component.
pub fn opens(b: &Board, net: u32) -> usize {
    components(b, net).len().saturating_sub(1)
}

/// Track and arc endpoints that touch no other copper of the net and no zone fill.
pub fn dangling(b: &Board, net: u32) -> Vec<u32> {
    let mut out = Vec::new();
    for it in b.items.iter().filter(|i| i.alive && i.net == net && matches!(i.kind, Kind::Track | Kind::Arc)) {
        for s in &it.shapes {
            let Core::Seg(a, z) = s.core else { continue };
            for end in [a, z] {
                let touch = b.index.query([end.x - 1, end.y - 1, end.x + 1, end.y + 1], crate::index::key_bits(it.layers))
                    .into_iter().any(|j| {
                        let c = &b.items[j as usize];
                        c.id != it.id && c.alive && c.net == net && copper_kind(c.kind)
                            && it.layers & c.layers != 0 && geom::gap_sets(&[crate::geom::Shape { core: Core::Point(end), r: 0 }], &c.shapes) <= TOUCH_NM
                    });
                if !touch && !out.contains(&it.id) {
                    out.push(it.id);
                }
            }
        }
    }
    out
}
