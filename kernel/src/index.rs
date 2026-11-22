//! Grid index (K6): per layer a hash grid of 1 mm cells; items are listed in every cell their bbox touches.
//! Layer key: bits 0..63 are copper layers, bit 64 is "no copper" (edge and other items with layers == 0).

use std::collections::HashMap;

pub const CELL: i64 = 1_000_000;
pub const NOCOPPER: u32 = 64;

#[derive(Debug, Default)]
pub struct Index {
    cells: HashMap<(u32, i64, i64), Vec<u32>>,
}

/// Index keys of an item's layer mask (bit 64 for items on no copper layer).
pub fn key_bits(layers: u64) -> u128 {
    if layers == 0 { 1u128 << NOCOPPER } else { layers as u128 }
}

fn each_layer(mask: u128) -> impl Iterator<Item = u32> {
    (0..=NOCOPPER).filter(move |k| mask & (1u128 << k) != 0)
}

fn span(bbox: [i64; 4]) -> (i64, i64, i64, i64) {
    (bbox[0].div_euclid(CELL), bbox[1].div_euclid(CELL), bbox[2].div_euclid(CELL), bbox[3].div_euclid(CELL))
}

impl Index {
    pub fn insert(&mut self, id: u32, bbox: [i64; 4], layers: u64) {
        let (x0, y0, x1, y1) = span(bbox);
        for k in each_layer(key_bits(layers)) {
            for cx in x0..=x1 {
                for cy in y0..=y1 {
                    self.cells.entry((k, cx, cy)).or_default().push(id);
                }
            }
        }
    }

    pub fn remove(&mut self, id: u32, bbox: [i64; 4], layers: u64) {
        let (x0, y0, x1, y1) = span(bbox);
        for k in each_layer(key_bits(layers)) {
            for cx in x0..=x1 {
                for cy in y0..=y1 {
                    if let Some(v) = self.cells.get_mut(&(k, cx, cy)) {
                        v.retain(|&i| i != id);
                    }
                }
            }
        }
    }

    /// Ids (sorted, unique) of items whose cells meet bbox on any layer in `mask`. The caller checks exact boxes.
    pub fn query(&self, bbox: [i64; 4], mask: u128) -> Vec<u32> {
        let (x0, y0, x1, y1) = span(bbox);
        let mut out = Vec::new();
        for k in each_layer(mask) {
            for cx in x0..=x1 {
                for cy in y0..=y1 {
                    if let Some(v) = self.cells.get(&(k, cx, cy)) {
                        out.extend_from_slice(v);
                    }
                }
            }
        }
        out.sort_unstable();
        out.dedup();
        out
    }

    pub fn len(&self) -> usize {
        self.cells.values().map(Vec::len).sum()
    }
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

    fn overlaps(a: [i64; 4], b: [i64; 4]) -> bool {
        a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1]
    }

    #[test]
    fn query_equals_brute_force() {
        let mut rng = Rng(0x2545_f491_4f6c_dd1d);
        let mut items = Vec::new();
        let mut idx = Index::default();
        for id in 0..5000u32 {
            let x = rng.range(0, 100_000_000);
            let y = rng.range(0, 80_000_000);
            let b = [x, y, x + rng.range(0, 5_000_000), y + rng.range(0, 5_000_000)];
            let layers = 1u64 << rng.range(0, 4);
            idx.insert(id, b, layers);
            items.push((id, b, layers));
        }
        for _ in 0..200 {
            let x = rng.range(0, 100_000_000);
            let y = rng.range(0, 80_000_000);
            let q = [x, y, x + rng.range(0, 8_000_000), y + rng.range(0, 8_000_000)];
            let mask = 1u128 << rng.range(0, 4);
            let got: Vec<u32> = idx.query(q, mask).into_iter()
                .filter(|&id| {
                    let (_, b, l) = items[id as usize];
                    overlaps(b, q) && (key_bits(l) & mask) != 0
                }).collect();
            let want: Vec<u32> = items.iter()
                .filter(|(_, b, l)| overlaps(*b, q) && (key_bits(*l) & mask) != 0)
                .map(|(id, _, _)| *id).collect();
            assert_eq!(got, want);
        }
    }

    #[test]
    fn remove_then_query_and_long_item() {
        let mut idx = Index::default();
        let long = [0, 0, 199_999_999, 500_000];           // 200 cells along x
        idx.insert(7, long, 0b1);
        assert_eq!(idx.len(), 200);
        assert_eq!(idx.query([150_000_000, 0, 150_000_000, 0], 0b1), vec![7]);
        idx.remove(7, long, 0b1);
        assert!(idx.query([150_000_000, 0, 150_000_000, 0], 0b1).is_empty());
        assert_eq!(idx.len(), 0);
        idx.insert(9, [0, 0, 10, 10], 0);                     // no copper: bit 64 only
        assert_eq!(idx.query([0, 0, 1, 1], 1u128 << NOCOPPER), vec![9]);
        assert!(idx.query([0, 0, 1, 1], 0b1).is_empty());
    }
}
