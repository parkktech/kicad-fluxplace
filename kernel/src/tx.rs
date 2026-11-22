//! Transactions (K10): nested begin / commit / rollback over board edits, add, remove, and emit to KiCad 10 text.
//! Rollback undoes removals in reverse order and truncates the items added since the matching begin (adds are
//! always appended, so the item table is restored exactly). Holes of a via or pad share its uuid.

use crate::board::{Board, Item, Kind};
use crate::geom::{Core, Pt, Shape};
use crate::load::ARC_ERR_NM;
use crate::geom::arc_polyline;
use serde_json::Value;

#[derive(Debug, Default)]
pub struct Tx {
    /// ids removed while a transaction is open (undo order)
    pub log: Vec<u32>,
    /// (items length, log length) at each open begin
    pub marks: Vec<(usize, usize)>,
    pub seq: u64,
}

pub fn depth(b: &Board) -> usize {
    b.tx.marks.len()
}

pub fn begin(b: &mut Board) {
    b.tx.marks.push((b.items.len(), b.tx.log.len()));
}

pub fn commit(b: &mut Board) -> Result<usize, String> {
    if b.tx.marks.pop().is_none() {
        return Err("commit without begin".into());
    }
    if b.tx.marks.is_empty() {
        b.tx.log.clear();
    }
    Ok(b.tx.marks.len())
}

pub fn rollback(b: &mut Board) -> Result<usize, String> {
    let (items_len, log_len) = b.tx.marks.pop().ok_or("rollback without begin")?;
    let undo: Vec<u32> = b.tx.log.drain(log_len..).collect();
    for id in undo.into_iter().rev() {
        let it = &mut b.items[id as usize];
        it.alive = true;
        let (bbox, layers) = (it.bbox, it.layers);
        b.index.insert(id, bbox, layers);
    }
    for id in items_len..b.items.len() {
        let it = &b.items[id];
        if it.alive {
            b.index.remove(id as u32, it.bbox, it.layers);
        }
    }
    b.items.truncate(items_len);
    Ok(b.tx.marks.len())
}

/// Marks one item dead (kept in the table for its id). Returns false when it was already dead.
pub fn remove_id(b: &mut Board, id: u32) -> bool {
    let Some(it) = b.items.get_mut(id as usize) else { return false };
    if !it.alive {
        return false;
    }
    it.alive = false;
    let (bbox, layers) = (it.bbox, it.layers);
    b.index.remove(id, bbox, layers);
    if !b.tx.marks.is_empty() {
        b.tx.log.push(id);
    }
    true
}

/// Random-looking v4 uuid from a per-board sequence (deterministic per board state, unique within a board).
pub fn new_uuid(b: &mut Board) -> String {
    b.tx.seq += 1;
    let mut x = b.tx.seq.wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ (b.items.len() as u64).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x ^= x >> 31;
    let lo = x.wrapping_mul(0x94D0_49BB_1331_11EB) ^ (x >> 29);
    format!("{:08x}-{:04x}-4{:03x}-8{:03x}-{:012x}", (x >> 32) as u32, (x >> 16) as u16, (x & 0xfff) as u16,
            (lo >> 52) as u16 & 0xfff, lo & 0xffff_ffff_ffff)
}

fn arc_shapes(s: Pt, m: Pt, e: Pt, w: i64) -> Vec<Shape> {
    let (pts, bound) = arc_polyline(s, m, e, ARC_ERR_NM);
    let r = w / 2 + bound.ceil() as i64;
    pts.windows(2).map(|p| Shape { core: Core::Seg(p[0], p[1]), r }).collect()
}

/// Adds one item from a JSON description (same shapes as the loader). Returns the new ids and uuids.
pub fn add_json(b: &mut Board, spec: &Value) -> Result<(Vec<u32>, Vec<String>), String> {
    let nm = |v: &Value| -> Result<i64, String> {
        v.as_f64().map(|x| (x * 1e6).round() as i64).ok_or_else(|| format!("expected a number, got {v}"))
    };
    let pt = |v: &Value| -> Result<Pt, String> {
        let a = v.as_array().filter(|a| a.len() == 2).ok_or("point needs [x, y]")?;
        Ok(Pt { x: nm(&a[0])?, y: nm(&a[1])? })
    };
    let kind = spec.get("kind").and_then(Value::as_str).ok_or("item needs kind")?;
    let net_name = spec.get("net").and_then(Value::as_str).unwrap_or("");
    let net = b.intern_net(net_name);
    let uuid = match spec.get("uuid").and_then(Value::as_str) {
        Some(u) => u.to_string(),
        None => new_uuid(b),
    };
    let layer_mask = |name: &str| b.layer_bit(name).map(|k| 1u64 << k).ok_or_else(|| format!("{name} is not a copper layer"));
    match kind {
        "seg" | "arc" => {
            let layer = spec.get("layer").and_then(Value::as_str).ok_or("track needs layer")?;
            let mask = layer_mask(layer)?;
            let w = spec.get("width").map(nm).transpose()?.ok_or("track needs width")?;
            let (shapes, k, extra) = if kind == "seg" {
                let (a, z) = (pt(&spec["a"])?, pt(&spec["b"])?);
                (vec![Shape { core: Core::Seg(a, z), r: w / 2 }], Kind::Track, serde_json::json!({"width": w as f64 / 1e6}))
            } else {
                let (s, m, e) = (pt(&spec["a"])?, pt(&spec["mid"])?, pt(&spec["b"])?);
                let sh = arc_shapes(s, m, e, w);
                (sh, Kind::Arc, serde_json::json!({"width": w as f64 / 1e6, "arc": true,
                    "mid": [m.x as f64 / 1e6, m.y as f64 / 1e6]}))
            };
            let mut it = Item::new(k, uuid.clone(), net, mask, shapes);
            it.locked = spec.get("locked").and_then(Value::as_bool).unwrap_or(false);
            it.extra = extra;
            Ok((vec![b.add_item(it)], vec![uuid]))
        }
        "via" => {
            let at = pt(&spec["at"])?;
            let dia = nm(&spec["dia"])?;
            let drill = nm(&spec["drill"])?;
            let mask = match spec.get("layers").and_then(Value::as_array) {
                None => b.all_copper_mask(),
                Some(l) if l.len() == 2 => b.span_mask(l[0].as_str().unwrap_or(""), l[1].as_str().unwrap_or(""))
                    .ok_or("via layers must be copper")?,
                Some(_) => return Err("via layers needs two copper layers".into()),
            };
            let mut via = Item::new(Kind::Via, uuid.clone(), net, mask, vec![Shape { core: Core::Point(at), r: dia / 2 }]);
            via.extra = serde_json::json!({"via": true, "dia": dia as f64 / 1e6, "drill": drill as f64 / 1e6});
            let mut ids = vec![b.add_item(via)];
            let mut hole = Item::new(Kind::Hole, uuid.clone(), net, b.all_copper_mask(),
                                     vec![Shape { core: Core::Point(at), r: drill / 2 }]);
            hole.extra = serde_json::json!({"via": true});
            ids.push(b.add_item(hole));
            Ok((ids, vec![uuid]))
        }
        other => Err(format!("unknown kind {other}")),
    }
}

/// Fixed-point mm text with up to 6 decimals (exact for nm input).
pub fn mm_text(nm: i64) -> String {
    let neg = nm < 0;
    let a = nm.unsigned_abs();
    let (i, f) = (a / 1_000_000, a % 1_000_000);
    let frac = format!("{f:06}");
    let frac = frac.trim_end_matches('0');
    let body = if frac.is_empty() { format!("{i}") } else { format!("{i}.{frac}") };
    if neg && body != "0" { format!("-{body}") } else { body }
}

/// KiCad 10 text of one copper item (segment, arc or via); other kinds are an error.
pub fn emit(b: &Board, id: u32) -> Result<String, String> {
    let it = b.items.get(id as usize).filter(|i| i.alive).ok_or_else(|| format!("no live item {id}"))?;
    let m = mm_text;
    let p = |x: Pt| format!("{} {}", m(x.x), m(x.y));
    let net = format!(r#"(net "{}")"#, b.net_name(it.net));
    let lock = if it.locked { " (locked yes)" } else { "" };
    let width = it.extra.get("width").and_then(Value::as_f64).map(|w| (w * 1e6).round() as i64);
    match it.kind {
        Kind::Track => {
            let Core::Seg(a, z) = it.shapes[0].core else { return Err("track is not a segment".into()) };
            let w = width.ok_or("track has no width")?;
            let layer = b.layer_names(it.layers).first().copied().unwrap_or("");
            Ok(format!(r#"(segment (start {}) (end {}) (width {}) (layer "{layer}") {net}{lock} (uuid "{}"))"#,
                       p(a), p(z), m(w), it.uuid))
        }
        Kind::Arc => {
            let first = it.shapes.first().ok_or("empty arc")?;
            let last = it.shapes.last().ok_or("empty arc")?;
            let (Core::Seg(s, _), Core::Seg(_, e)) = (&first.core, &last.core) else { return Err("arc shape".into()) };
            let mid = it.extra.get("mid").and_then(Value::as_array).ok_or("arc has no mid")?;
            let mid = Pt { x: (mid[0].as_f64().unwrap_or(0.0) * 1e6).round() as i64,
                           y: (mid[1].as_f64().unwrap_or(0.0) * 1e6).round() as i64 };
            let w = width.ok_or("arc has no width")?;
            let layer = b.layer_names(it.layers).first().copied().unwrap_or("");
            Ok(format!(r#"(arc (start {}) (mid {}) (end {}) (width {}) (layer "{layer}") {net}{lock} (uuid "{}"))"#,
                       p(*s), p(mid), p(*e), m(w), it.uuid))
        }
        Kind::Via => {
            let Core::Point(at) = it.shapes[0].core else { return Err("via is not a point".into()) };
            let dia = it.extra.get("dia").and_then(Value::as_f64).map(|d| (d * 1e6).round() as i64).ok_or("via dia")?;
            let drill = it.extra.get("drill").and_then(Value::as_f64).map(|d| (d * 1e6).round() as i64).ok_or("via drill")?;
            let names = b.layer_names(it.layers);
            let (a, z) = (names.first().copied().unwrap_or(""), names.last().copied().unwrap_or(""));
            Ok(format!(r#"(via (at {}) (size {}) (drill {}) (layers "{a}" "{z}") {net}{lock} (uuid "{}"))"#,
                       p(at), m(dia), m(drill), it.uuid))
        }
        other => Err(format!("emit of {} is not modelled", other.name())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::board::item_json;
    use serde_json::json;

    fn board() -> Board {
        Board::new(vec!["F.Cu".into(), "In1.Cu".into(), "B.Cu".into()])
    }

    /// The alive items as JSON text: the "items dump" of the K10 test.
    fn dump(b: &Board) -> String {
        let v: Vec<Value> = b.items.iter().filter(|i| i.alive).map(|i| item_json(b, i)).collect();
        serde_json::to_string(&v).unwrap()
    }

    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
        fn pick(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }

    fn random_edit(b: &mut Board, rng: &mut Rng) {
        match rng.pick(3) {
            0 => {
                let x = rng.pick(50) as f64;
                let spec = json!({"kind": "seg", "net": "N", "layer": "F.Cu", "width": 0.2,
                                  "a": [x, 1.0], "b": [x + 3.0, 1.0]});
                add_json(b, &spec).unwrap();
            }
            1 => {
                let live: Vec<u32> = b.items.iter().filter(|i| i.alive).map(|i| i.id).collect();
                if !live.is_empty() {
                    remove_id(b, live[rng.pick(live.len() as u64) as usize]);
                }
            }
            _ => {
                let spec = json!({"kind": "via", "net": "M", "at": [5.0, 5.0], "dia": 0.6, "drill": 0.3});
                add_json(b, &spec).unwrap();
            }
        }
    }

    #[test]
    fn rollback_restores_the_items_dump_exactly() {
        let mut b = board();
        let mut rng = Rng(0xdead_beef_1234_5678);
        let mut snaps: Vec<(String, usize)> = Vec::new();
        let mut rollbacks = 0;
        for _ in 0..200 {
            begin(&mut b);
            snaps.push((dump(&b), b.index.len()));
            random_edit(&mut b, &mut rng);
            let mut steps = 0;
            while depth(&b) > 0 && steps < 200 {
                steps += 1;
                match rng.pick(5) {
                    0 if depth(&b) < 3 => {
                        begin(&mut b);
                        snaps.push((dump(&b), b.index.len()));
                        random_edit(&mut b, &mut rng);
                    }
                    1 | 2 => {
                        rollback(&mut b).unwrap();
                        let (want, idx) = snaps.pop().unwrap();
                        assert_eq!(dump(&b), want, "rollback must restore the dump exactly");
                        assert_eq!(b.index.len(), idx, "and the index");
                        rollbacks += 1;
                    }
                    3 => {
                        commit(&mut b).unwrap();
                        snaps.pop();
                    }
                    _ => random_edit(&mut b, &mut rng),
                }
            }
            if depth(&b) > 0 {
                while depth(&b) > 0 {
                    rollback(&mut b).unwrap();
                    let (want, _) = snaps.pop().unwrap();
                    assert_eq!(dump(&b), want);
                    rollbacks += 1;
                }
            }
            assert!(snaps.is_empty());
        }
        assert!(rollbacks > 200, "exercised {rollbacks} rollbacks");
    }

    #[test]
    fn emit_parses_back_to_the_same_item() {
        let mut b = board();
        let (ids, _) = add_json(&mut b, &json!({"kind": "via", "net": "N", "at": [12.5, 7.25], "dia": 0.6,
                                                "drill": 0.3, "layers": ["F.Cu", "B.Cu"], "uuid": "aaaa"})).unwrap();
        let (sid, _) = add_json(&mut b, &json!({"kind": "seg", "net": "N", "layer": "F.Cu", "width": 0.2,
                                                "a": [1.0, 2.0], "b": [3.5, 2.0], "uuid": "bbbb"})).unwrap();
        let text = format!("(kicad_pcb (version 20260206) (layers (0 \"F.Cu\" signal) (4 \"In1.Cu\" signal) \
(31 \"B.Cu\" signal) (44 \"Edge.Cuts\" user))\n{}\n{})\n", emit(&b, sid[0]).unwrap(), emit(&b, ids[0]).unwrap());
        let back = crate::load::load(&text).unwrap();
        let seg = back.items.iter().find(|i| i.kind == Kind::Track).unwrap();
        let orig = &b.items[sid[0] as usize];
        assert_eq!(seg.shapes, orig.shapes);
        assert_eq!(seg.layers, orig.layers);
        let via = back.items.iter().find(|i| i.kind == Kind::Via).unwrap();
        assert_eq!(via.shapes, b.items[ids[0] as usize].shapes);
        assert_eq!(via.layers, b.items[ids[0] as usize].layers);
    }

    #[test]
    fn mm_text_is_exact() {
        assert_eq!(mm_text(1_475_000), "1.475");
        assert_eq!(mm_text(-937_500), "-0.9375");
        assert_eq!(mm_text(2_000_000), "2");
        assert_eq!(mm_text(1), "0.000001");
    }
}
