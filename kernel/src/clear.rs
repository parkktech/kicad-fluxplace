//! Clearance engine (K9): the required gap `need` (ROUTER-C1 section 6) and the check of an item set against the index.
//! Units nm. Keepouts use the flags of the keepout item. Connectivity (opens) is K11; pours are K15.
//! Neighbourhood query margin: 5 mm + the item's largest radius (clearances above 5 mm are not modelled).

use crate::board::{Board, Item, Kind};
use crate::dru::{self, Facts, Rule, Tri};
use crate::geom;
use crate::rules::Rules;
use serde_json::{json, Value};

pub const MARGIN: i64 = 5_000_000;

pub struct Ctx<'a> {
    pub board: &'a Board,
    pub rules: &'a Rules,
    pub dru: &'a dru::Dru,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Need {
    pub nm: i64,
    pub rule: String,
}

fn copper(k: Kind) -> bool {
    matches!(k, Kind::Track | Kind::Arc | Kind::Via | Kind::Pad | Kind::Graphic)
}

fn facts(cx: &Ctx, it: &Item) -> Facts {
    let net = cx.board.net_name(it.net).to_string();
    let class = cx.rules.net_rule(&net).class;
    let netclasses = class.split(',').map(str::to_string).collect();
    let kind = match it.kind {
        Kind::Track | Kind::Arc => "Track",
        Kind::Via => "Via",
        Kind::Pad => "Pad",
        Kind::Hole => if it.extra.get("via").is_some() { "Via" } else { "Pad" },
        Kind::Keepout => "Zone",
        Kind::Edge | Kind::Graphic | Kind::Fill => "Graphic",
    }.to_string();
    let npth = it.extra.get("npth").and_then(Value::as_bool).unwrap_or(false);
    let ptype = it.extra.get("type").and_then(Value::as_str).unwrap_or("");
    let pad_type = match (it.kind, ptype) {
        (Kind::Pad, "smd") | (Kind::Pad, "connect") => "SMD".to_string(),
        (Kind::Pad, "thru_hole") => "PTH".to_string(),
        (Kind::Pad, "np_thru_hole") => "NPTH".to_string(),
        (Kind::Hole, _) => if npth { "NPTH" } else { "PTH" }.to_string(),
        _ => String::new(),
    };
    let via_type = if it.kind == Kind::Via {
        if it.layers == cx.board.all_copper_mask() { "THROUGH" } else { "BLIND" }.to_string()
    } else {
        String::new()
    };
    let layer = cx.board.layer_names(it.layers).first().map(|s| s.to_string()).unwrap_or_default();
    Facts {
        kind, net_name: net, net_class: class, netclasses, layer,
        plated: matches!(it.kind, Kind::Hole) || (it.kind == Kind::Pad && pad_type == "PTH"),
        pad_type, via_type, diff_pair: None, coupled: false,
    }
}

fn layer_matches(rule: &Rule, layer: &str) -> bool {
    match rule.layer.as_deref() {
        None => true,
        Some("outer") => layer.is_empty() || layer == "F.Cu" || layer == "B.Cu",
        Some("inner") => layer.is_empty() || (layer != "F.Cu" && layer != "B.Cu"),
        Some(l) => layer.is_empty() || l == layer,
    }
}

/// Applies the DRU rules of constraint `kind` in file order to `base`: a rule that matches (true) replaces the
/// value, a rule that may match only raises it. Returns (value, name of the last rule that set it).
fn dru_apply(cx: &Ctx, kind: &str, base: i64, base_name: &str, a: &Item, c: &Item, layer: &str) -> (i64, String) {
    let (fa, fc) = (facts(cx, a), facts(cx, c));
    let mut v = base;
    let mut name = base_name.to_string();
    for rule in &cx.dru.rules {
        if !layer_matches(rule, layer) {
            continue;
        }
        let Some(min) = dru::min_of(rule, kind) else { continue };
        let t = match &rule.cond {
            None => Tri::True,
            Some(e) => dru::eval(e, &fa, &fc).or(dru::eval(e, &fc, &fa)),
        };
        match t {
            Tri::True => {
                v = min;
                name = rule.name.clone();
            }
            Tri::May => {
                if min > v {
                    v = min;
                    name = format!("{} (may)", rule.name);
                }
            }
            Tri::False => {}
        }
    }
    (v, name)
}

/// Value of an item's own clearance: local pad clearance, else the net class (net 0: the floor).
fn item_value(cx: &Ctx, it: &Item) -> i64 {
    it.clr_local.unwrap_or_else(|| if it.net == 0 { cx.rules.floors.clr } else {
        cx.rules.net_rule(cx.board.net_name(it.net)).clearance
    })
}

fn first_layer(cx: &Ctx, it: &Item) -> String {
    cx.board.layer_names(it.layers).first().map(|s| s.to_string()).unwrap_or_default()
}

/// Required gap between a and c on `layer` ("" for holes and edges). Holes: hole clearance / hole to hole;
/// edge: copper to Edge.Cuts; copper pairs: the larger item value, then DRU, then the floor.
pub fn need(cx: &Ctx, a: &Item, c: &Item, layer: &str) -> Need {
    let f = &cx.rules.floors;
    let (base, base_name, kind, floor) = match (a.kind, c.kind) {
        (Kind::Hole, Kind::Hole) => (f.h2h, "floor", "hole_to_hole", f.h2h),
        (Kind::Hole, _) | (_, Kind::Hole) => (f.hole_clr, "floor", "hole_clearance", f.hole_clr),
        (Kind::Edge, _) | (_, Kind::Edge) => (f.edge_clr, "floor", "edge_clearance", f.edge_clr),
        _ => (item_value(cx, a).max(item_value(cx, c)), "net class", "clearance", f.clr),
    };
    let (v, name) = dru_apply(cx, kind, base, base_name, a, c, layer);
    Need { nm: v.max(floor), rule: name }
}

fn gap(a: &Item, c: &Item) -> f64 {
    geom::gap_sets(&a.shapes, &c.shapes)
}

#[derive(Clone, Debug)]
pub struct Violation {
    pub kind: &'static str,
    pub a: u32,
    pub b: Option<u32>,
    pub layer: String,
    pub need: i64,
    pub have: f64,
    pub at: [i64; 2],
    pub rule: String,
}

fn mid(a: &Item, c: &Item) -> [i64; 2] {
    [(a.bbox[0] + a.bbox[2] + c.bbox[0] + c.bbox[2]) / 4, (a.bbox[1] + a.bbox[3] + c.bbox[1] + c.bbox[3]) / 4]
}

fn push(out: &mut Vec<Violation>, kind: &'static str, a: &Item, c: Option<&Item>, layer: &str, need: Need, have: f64) {
    out.push(Violation {
        kind, a: a.id, b: c.map(|x| x.id), layer: layer.to_string(), need: need.nm, have,
        at: match c { Some(x) => mid(a, x), None => [(a.bbox[0] + a.bbox[2]) / 2, (a.bbox[1] + a.bbox[3]) / 2] },
        rule: need.rule,
    });
}

/// Width, via size, drill and annular ring of one item (mm floors, DRU where it sets a value).
fn self_checks(cx: &Ctx, it: &Item, out: &mut Vec<Violation>) {
    let f = &cx.rules.floors;
    let layer = first_layer(cx, it);
    let mm = |k: &str| it.extra.get(k).and_then(Value::as_f64).map(|v| (v * 1e6).round() as i64);
    match it.kind {
        Kind::Track | Kind::Arc => {
            if let Some(w) = mm("width") {
                let (floor, name) = dru_apply(cx, "track_width", f.track, "floor", it, it, &layer);
                if w < floor {
                    push(out, "width", it, None, &layer, Need { nm: floor, rule: name }, w as f64);
                }
            }
        }
        Kind::Via => {
            let (dia, drill) = (mm("dia"), mm("drill"));
            if let Some(d) = dia {
                let (floor, name) = dru_apply(cx, "via_diameter", f.via_dia, "floor", it, it, &layer);
                if d < floor {
                    push(out, "via size", it, None, &layer, Need { nm: floor, rule: name }, d as f64);
                }
                if let Some(dr) = drill {
                    let (dfloor, dname) = dru_apply(cx, "hole_size", f.via_drill, "floor", it, it, &layer);
                    if dr < dfloor {
                        push(out, "drill", it, None, &layer, Need { nm: dfloor, rule: dname }, dr as f64);
                    }
                    let (afloor, aname) = dru_apply(cx, "annular_width", f.annular, "floor", it, it, &layer);
                    let ring = (d - dr) / 2;
                    if ring < afloor {
                        push(out, "annular", it, None, &layer, Need { nm: afloor, rule: aname }, ring as f64);
                    }
                }
            }
        }
        _ => {}
    }
}

/// Copper item against a keepout polygon whose flags forbid its kind.
fn keepout_check(cx: &Ctx, it: &Item, ko: &Item, out: &mut Vec<Violation>) {
    let flag = match it.kind {
        Kind::Track | Kind::Arc => "no_tracks",
        Kind::Via => "no_vias",
        Kind::Pad => "no_pads",
        _ => return,
    };
    if ko.extra.get(flag).and_then(Value::as_bool) != Some(true) || it.layers & ko.layers == 0 {
        return;
    }
    let layer = first_layer(cx, it);
    let g = gap(it, ko);
    if g < 0.0 {
        push(out, "keepout", it, Some(ko), &layer, Need { nm: 0, rule: "keepout".into() }, g);
    }
}

fn pair_check(cx: &Ctx, a: &Item, c: &Item, out: &mut Vec<Violation>) {
    match (a.kind, c.kind) {
        (Kind::Keepout, Kind::Keepout) | (Kind::Edge, Kind::Edge) => {}
        (Kind::Keepout, _) if copper(c.kind) => keepout_check(cx, c, a, out),
        (_, Kind::Keepout) if copper(a.kind) => keepout_check(cx, a, c, out),
        (Kind::Edge, k) | (k, Kind::Edge) if k != Kind::Keepout => {
            let (e, o) = if a.kind == Kind::Edge { (a, c) } else { (c, a) };
            let n = need(cx, e, o, "");
            let g = gap(e, o);
            if g < n.nm as f64 {
                push(out, "edge", o, Some(e), &first_layer(cx, o), n, g);
            }
        }
        (Kind::Hole, Kind::Hole) => {
            if a.uuid == c.uuid {
                return;
            }
            let n = need(cx, a, c, "");
            let g = gap(a, c);
            if g < n.nm as f64 {
                push(out, "hole to hole", a, Some(c), "", n, g);
            }
        }
        (Kind::Hole, _) | (_, Kind::Hole) => {
            let (h, o) = if a.kind == Kind::Hole { (a, c) } else { (c, a) };
            if !copper(o.kind) || h.uuid == o.uuid || (o.net != 0 && o.net == h.net) {
                return;
            }
            let layer = first_layer(cx, o);
            let n = need(cx, h, o, &layer);
            let g = gap(h, o);
            if g < n.nm as f64 {
                push(out, "hole clearance", h, Some(o), &layer, n, g);
            }
        }
        _ if copper(a.kind) && copper(c.kind) => {
            let common = a.layers & c.layers;
            if common == 0 || (a.net != 0 && a.net == c.net) {
                return;
            }
            let layer = cx.board.layer_names(common).first().map(|s| s.to_string()).unwrap_or_default();
            let n = need(cx, a, c, &layer);
            let g = gap(a, c);
            if g < 0.0 {
                push(out, "short", a, Some(c), &layer, Need { nm: 0, rule: "short".into() }, g);
            } else if g < n.nm as f64 {
                push(out, "clearance", a, Some(c), &layer, n, g);
            }
        }
        _ => {}
    }
}

/// Violations of the items `ids` (default: every live item) against all live items. Each pair is reported once.
pub fn check(cx: &Ctx, ids: Option<&[u32]>) -> Vec<Violation> {
    let b = cx.board;
    let mut targets = vec![false; b.items.len()];
    match ids {
        Some(v) => {
            for &i in v {
                if let Some(t) = targets.get_mut(i as usize) {
                    *t = true;
                }
            }
        }
        None => targets.iter_mut().for_each(|t| *t = true),
    }
    let mut out = Vec::new();
    for (i, it) in b.items.iter().enumerate() {
        if !targets[i] || !it.alive {
            continue;
        }
        self_checks(cx, it, &mut out);
        let r = it.shapes.iter().map(|s| s.r).max().unwrap_or(0);
        let m = MARGIN + r;
        let bb = [it.bbox[0] - m, it.bbox[1] - m, it.bbox[2] + m, it.bbox[3] + m];
        // every layer and the no-copper bit: a pair is then found from either side (the dedupe below relies on it)
        let mask = (1u128 << (crate::index::NOCOPPER + 1)) - 1;
        for j in b.index.query(bb, mask) {
            let j = j as usize;
            if j == i || !b.items[j].alive || (targets[j] && j < i) {
                continue;
            }
            pair_check(cx, it, &b.items[j], &mut out);
        }
    }
    out
}

pub fn violation_json(b: &Board, v: &Violation) -> Value {
    let mm = |x: i64| x as f64 / 1e6;
    json!({"type": v.kind, "a": v.a, "b": v.b, "layer": v.layer, "need": mm(v.need), "have": v.have / 1e6,
           "at": [mm(v.at[0]), mm(v.at[1])], "rule": v.rule,
           "a_net": b.net_name(b.items[v.a as usize].net), "a_uuid": b.items[v.a as usize].uuid})
}
