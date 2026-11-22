//! Board model: copper stack, nets (name <-> id, id 0 = no net), item table. Coordinates are nm (i64).

use crate::geom::{Core, Pt, Shape};
use serde_json::{json, Value};
use std::collections::HashMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Pad,
    Track,
    Arc,
    Via,
    Hole,
    Fill,
    Keepout,
    Edge,
    Graphic,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Pad => "pad",
            Kind::Track => "track",
            Kind::Arc => "arc",
            Kind::Via => "via",
            Kind::Hole => "hole",
            Kind::Fill => "fill",
            Kind::Keepout => "keepout",
            Kind::Edge => "edge",
            Kind::Graphic => "graphic",
        }
    }

    pub fn parse(s: &str) -> Option<Kind> {
        [Kind::Pad, Kind::Track, Kind::Arc, Kind::Via, Kind::Hole, Kind::Fill, Kind::Keepout, Kind::Edge,
         Kind::Graphic].into_iter().find(|k| k.name() == s)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Item {
    pub id: u32,
    pub uuid: String,
    pub kind: Kind,
    pub net: u32,
    pub layers: u64,
    pub shapes: Vec<Shape>,
    pub bbox: [i64; 4],
    pub clr_local: Option<i64>,
    pub class: u16,
    pub ref_: String,
    pub locked: bool,
    pub alive: bool,
    pub extra: Value,
}

impl Item {
    pub fn new(kind: Kind, uuid: String, net: u32, layers: u64, shapes: Vec<Shape>) -> Item {
        Item { id: 0, uuid, kind, net, layers, bbox: bbox_of(&shapes), shapes, clr_local: None, class: 0,
               ref_: String::new(), locked: false, alive: true, extra: json!({}) }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Unsupported {
    pub what: String,
    pub at: String,
    pub effect: String,
}

/// A copper zone (pour). Keepout zones become Keepout items instead.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Zone {
    pub uuid: String,
    pub net: u32,
    pub layers: u64,
    pub name: String,
    pub priority: i64,
    pub outline: Vec<Pt>,
    pub clearance: Option<i64>,
    pub min_thickness: Option<i64>,
    pub connect: String,
    pub thermal_gap: Option<i64>,
    pub thermal_width: Option<i64>,
    pub island_mode: Option<i64>,
    pub hatch: Option<String>,
    /// (layer bit, outline) per stored filled_polygon, as the file has them.
    pub fills: Vec<(u32, Vec<Pt>)>,
}

#[derive(Debug, Default)]
pub struct Board {
    pub zones: Vec<Zone>,
    pub fill_stale: bool,
    pub file_version: String,
    /// Copper layer names in stack order: bit i of Item.layers is copper[i].
    pub copper: Vec<String>,
    /// Net names by id. Id 0 is the empty name (no net).
    pub nets: Vec<String>,
    pub net_ids: HashMap<String, u32>,
    pub items: Vec<Item>,
    pub index: crate::index::Index,
    pub unsupported: Vec<Unsupported>,
}

pub fn bbox_of(shapes: &[Shape]) -> [i64; 4] {
    let mut b = [i64::MAX, i64::MAX, i64::MIN, i64::MIN];
    let mut grow = |p: Pt, r: i64| {
        b[0] = b[0].min(p.x - r);
        b[1] = b[1].min(p.y - r);
        b[2] = b[2].max(p.x + r);
        b[3] = b[3].max(p.y + r);
    };
    for s in shapes {
        match &s.core {
            Core::Point(p) => grow(*p, s.r),
            Core::Seg(p, q) => {
                grow(*p, s.r);
                grow(*q, s.r);
            }
            Core::Poly(v) => v.iter().for_each(|p| grow(*p, s.r)),
        }
    }
    if shapes.is_empty() { [0, 0, 0, 0] } else { b }
}

impl Board {
    pub fn new(copper: Vec<String>) -> Board {
        let mut b = Board { copper, ..Default::default() };
        b.nets.push(String::new());
        b.net_ids.insert(String::new(), 0);
        b
    }

    pub fn intern_net(&mut self, name: &str) -> u32 {
        if let Some(&id) = self.net_ids.get(name) {
            return id;
        }
        let id = self.nets.len() as u32;
        self.nets.push(name.to_string());
        self.net_ids.insert(name.to_string(), id);
        id
    }

    pub fn net_name(&self, id: u32) -> &str {
        self.nets.get(id as usize).map(String::as_str).unwrap_or("")
    }

    pub fn layer_bit(&self, name: &str) -> Option<u32> {
        self.copper.iter().position(|c| c == name).map(|i| i as u32)
    }

    /// Mask of the copper layers from `a` to `b` (inclusive, either order).
    pub fn span_mask(&self, a: &str, b: &str) -> Option<u64> {
        let (i, j) = (self.layer_bit(a)?, self.layer_bit(b)?);
        let (lo, hi) = (i.min(j), i.max(j));
        Some((lo..=hi).fold(0u64, |m, k| m | (1u64 << k)))
    }

    pub fn all_copper_mask(&self) -> u64 {
        (0..self.copper.len()).fold(0u64, |m, k| m | (1u64 << k))
    }

    pub fn add_item(&mut self, mut it: Item) -> u32 {
        it.id = self.items.len() as u32;
        self.index.insert(it.id, it.bbox, it.layers);
        self.items.push(it);
        self.items.len() as u32 - 1
    }

    pub fn layer_names(&self, mask: u64) -> Vec<&str> {
        (0..self.copper.len()).filter(|&k| mask & (1u64 << k) != 0).map(|k| self.copper[k].as_str()).collect()
    }
}

fn mm(v: i64) -> f64 {
    v as f64 / 1e6
}

fn pt_json(p: Pt) -> Value {
    json!([mm(p.x), mm(p.y)])
}

pub fn shape_json(s: &Shape) -> Value {
    let core = match &s.core {
        Core::Point(p) => json!({"t": "point", "p": pt_json(*p)}),
        Core::Seg(a, b) => json!({"t": "seg", "a": pt_json(*a), "b": pt_json(*b)}),
        Core::Poly(v) => json!({"t": "poly", "pts": v.iter().map(|p| pt_json(*p)).collect::<Vec<_>>()}),
    };
    json!({"core": core, "r": mm(s.r)})
}

/// Item dict of the `items` reply (mm at the API).
pub fn item_json(b: &Board, it: &Item) -> Value {
    json!({
        "id": it.id, "uuid": it.uuid, "kind": it.kind.name(), "net": it.net, "net_name": b.net_name(it.net),
        "layers": b.layer_names(it.layers), "shapes": it.shapes.iter().map(shape_json).collect::<Vec<_>>(),
        "bbox": it.bbox.iter().map(|&v| mm(v)).collect::<Vec<_>>(),
        "clr": it.clr_local.map(mm), "clr_hole": Value::Null, "ref": it.ref_, "locked": it.locked,
        "extra": it.extra,
    })
}
