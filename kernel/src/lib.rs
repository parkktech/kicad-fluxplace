//! C ABI and JSON dispatch. Every entry point runs under catch_unwind: no panic crosses the boundary.
//! Reply shape: {"ok": true, ...} or {"ok": false, "error": "..."}.

pub mod board;
pub mod dru;
pub mod geom;
pub mod index;
pub mod load;
pub mod load_fp;
pub mod load_zone;
pub mod rules;
pub mod sexpr;

use serde_json::{json, Value};
use std::cell::RefCell;
use std::ffi::{c_char, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};

thread_local! {
    static LAST_ERROR: RefCell<CString> = RefCell::new(CString::new("").unwrap());
}

fn set_error(msg: &str) {
    let clean = msg.replace('\0', " ");
    LAST_ERROR.with(|e| *e.borrow_mut() = CString::new(clean).unwrap());
}

/// Opaque handle: the loaded board (K3) plus the open options.
pub struct Handle {
    path: String,
    #[allow(dead_code)]
    opts: Value,
    board: board::Board,
    rules: rules::Rules,
    dru: dru::Dru,
    load_ms: f64,
}

fn cstr_arg<'a>(p: *const c_char, what: &str) -> Result<&'a str, String> {
    if p.is_null() {
        return Err(format!("{what}: null pointer"));
    }
    unsafe { CStr::from_ptr(p) }.to_str().map_err(|_| format!("{what}: not UTF-8"))
}

fn open_inner(path: &str, opts_json: &str) -> Result<Handle, String> {
    let meta = std::fs::metadata(path).map_err(|e| format!("cannot open {path}: {e}"))?;
    if !meta.is_file() {
        return Err(format!("not a file: {path}"));
    }
    let opts: Value = if opts_json.trim().is_empty() {
        json!({})
    } else {
        serde_json::from_str(opts_json).map_err(|e| format!("opts: {e}"))?
    };
    let t0 = std::time::Instant::now();
    let src = std::fs::read_to_string(path).map_err(|e| format!("cannot read {path}: {e}"))?;
    let board = load::load(&src).map_err(|e| format!("{path}: {e}"))?;
    // project: explicit "project" option (null = none), else the .kicad_pro beside the board
    let project = match opts.get("project") {
        Some(Value::Null) => None,
        Some(Value::String(p)) => Some(p.clone()),
        _ => {
            let pro = std::path::Path::new(path).with_extension("kicad_pro");
            pro.is_file().then(|| pro.to_string_lossy().into_owned())
        }
    };
    let rules = rules::load(project.as_deref())?;
    // custom rules: the "dru" option (null = none), else the .kicad_dru beside the board
    let dru_path = match opts.get("dru") {
        Some(Value::Null) => None,
        Some(Value::String(p)) => Some(std::path::PathBuf::from(p)),
        _ => Some(std::path::Path::new(path).with_extension("kicad_dru")),
    };
    let dru = match dru_path.filter(|p| p.is_file()) {
        None => dru::Dru { coverage: "none".into(), ..Default::default() },
        Some(p) => {
            let text = std::fs::read_to_string(&p).map_err(|e| format!("cannot read {}: {e}", p.display()))?;
            dru::parse(&text).map_err(|e| format!("{}: {e}", p.display()))?
        }
    };
    Ok(Handle { path: path.to_string(), opts, board, rules, dru, load_ms: t0.elapsed().as_secs_f64() * 1e3 })
}

fn floors_json(f: &rules::Floors, tag: &str) -> Value {
    let mm = |x: i64| x as f64 / 1e6;
    json!({"ok": true, "tag": tag, "clr": mm(f.clr), "track": mm(f.track), "via_dia": mm(f.via_dia),
           "via_drill": mm(f.via_drill), "hole_clr": mm(f.hole_clr), "h2h": mm(f.h2h), "edge_clr": mm(f.edge_clr),
           "annular": mm(f.annular)})
}

fn rules_json(r: &rules::Rules) -> Value {
    json!({"source": r.source, "floors": floors_json(&r.floors, ""),
           "classes": r.classes.iter().map(|c| c.name.clone()).collect::<Vec<_>>(),
           "patterns": r.patterns.len(), "unsupported": r.unsupported})
}

/// items: optional filters bbox [x0,y0,x1,y1] (mm), layer (name), net (name), kinds (names), ids (u32 list).
fn items_op(h: &Handle, v: &Value) -> Value {
    let b = &h.board;
    let bbox = v.get("bbox").and_then(Value::as_array).and_then(|a| {
        let f: Vec<f64> = a.iter().filter_map(Value::as_f64).collect();
        (f.len() == 4).then(|| f.iter().map(|x| (x * 1e6).round() as i64).collect::<Vec<_>>())
    });
    let layer = v.get("layer").and_then(Value::as_str).and_then(|l| b.layer_bit(l));
    let net = v.get("net").and_then(Value::as_str).and_then(|n| b.net_ids.get(n).copied());
    let kinds: Option<Vec<board::Kind>> = v.get("kinds").and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).filter_map(board::Kind::parse).collect());
    let ids: Option<Vec<u64>> = v.get("ids").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_u64).collect());
    if v.get("net").is_some() && net.is_none() {
        return json!({"ok": true, "items": []});
    }
    let out: Vec<Value> = b.items.iter().filter(|it| it.alive).filter(|it| {
        bbox.as_ref().map_or(true, |bx| it.bbox[0] <= bx[2] && it.bbox[2] >= bx[0] && it.bbox[1] <= bx[3] && it.bbox[3] >= bx[1])
            && layer.map_or(true, |l| it.layers & (1u64 << l) != 0)
            && net.map_or(true, |n| it.net == n)
            && kinds.as_ref().map_or(true, |k| k.contains(&it.kind))
            && ids.as_ref().map_or(true, |s| s.contains(&(it.id as u64)))
    }).map(|it| board::item_json(b, it)).collect();
    json!({"ok": true, "items": out})
}

/// Dispatch one JSON request. K0 ops: ping, info (stub), panic (test hook: proves catch_unwind works).
pub fn dispatch(h: &mut Handle, req: &str) -> Value {
    let v: Value = match serde_json::from_str(req) {
        Ok(v) => v,
        Err(e) => return json!({"ok": false, "error": format!("request: {e}")}),
    };
    let op = match v.get("op").and_then(Value::as_str) {
        Some(op) => op,
        None => return json!({"ok": false, "error": "request: missing op"}),
    };
    match op {
        "ping" => json!({"ok": true, "pong": true}),
        "info" => {
            let b = &h.board;
            let live = b.items.iter().filter(|i| i.alive).count();
            let mut uuid_count: std::collections::HashMap<&str, u32> = std::collections::HashMap::new();
            for it in b.items.iter().filter(|i| i.alive && matches!(i.kind, board::Kind::Pad | board::Kind::Track | board::Kind::Arc | board::Kind::Via)) {
                *uuid_count.entry(it.uuid.as_str()).or_insert(0) += 1;
            }
            let dup_uuids = uuid_count.values().filter(|&&n| n > 1).count();
            json!({"ok": true, "path": h.path, "file_version": b.file_version, "layers": b.copper, "dup_uuids": dup_uuids,
                   "nets": b.nets.len() - 1, "items": live, "load_ms": h.load_ms,
                   "zones": b.zones.len(), "fill_stale": b.fill_stale,
                   "rules": rules_json(&h.rules),
                   "dru": {"rules": h.dru.rules.len(), "coverage": h.dru.coverage, "unsupported": h.dru.unsupported},
                   "unsupported": b.unsupported.iter().map(|u| json!({"what": u.what, "where": u.at, "effect": u.effect})).collect::<Vec<_>>()})
        }
        "items" => items_op(h, &v),
        "net_rule" => {
            let net = v.get("net").and_then(Value::as_str).unwrap_or("");
            let r = h.rules.net_rule(net);
            let mm = |x: i64| x as f64 / 1e6;
            json!({"ok": true, "class": r.class, "width": mm(r.width), "clearance": mm(r.clearance),
                   "via_dia": mm(r.via_dia), "via_drill": mm(r.via_drill), "dp_width": mm(r.dp_width), "dp_gap": mm(r.dp_gap)})
        }
        "set_floors" => {
            let g = |k: &str| v.get(k).and_then(Value::as_f64).map(|x| (x * 1e6).round() as i64).unwrap_or(0);
            h.rules.set_floors(g("clr"), g("track"), g("via_dia"), g("via_drill"));
            floors_json(&h.rules.floors, "ok")
        }
        "zones" => {
            let b = &h.board;
            let zones: Vec<Value> = b.zones.iter().map(|z| {
                let fills: Vec<Value> = (0..b.copper.len() as u32).filter_map(|bit| {
                    let polys: Vec<&Vec<geom::Pt>> = z.fills.iter().filter(|(l, _)| *l == bit).map(|(_, p)| p).collect();
                    (!polys.is_empty()).then(|| json!({"layer": b.copper[bit as usize],
                        "polygons": polys.len(),
                        "area_mm2": polys.iter().map(|p| load_zone::area_mm2(p)).sum::<f64>()}))
                }).collect();
                json!({"uuid": z.uuid, "name": z.name, "net": b.net_name(z.net), "layers": b.layer_names(z.layers),
                       "priority": z.priority, "fills": fills, "hatch": z.hatch,
                       "outline_pts": z.outline.len()})
            }).collect();
            json!({"ok": true, "zones": zones})
        }
        "panic" => panic!("forced panic (test hook)"),
        other => json!({"ok": false, "error": format!("unknown op: {other}")}),
    }
}

fn to_c(s: String) -> *mut c_char {
    CString::new(s.replace('\0', " ")).unwrap().into_raw()
}

#[no_mangle]
pub extern "C" fn fpk_open(path: *const c_char, opts_json: *const c_char) -> *mut Handle {
    let r = catch_unwind(AssertUnwindSafe(|| {
        let p = cstr_arg(path, "path")?;
        let o = if opts_json.is_null() { "" } else { cstr_arg(opts_json, "opts")? };
        open_inner(p, o)
    }));
    match r {
        Ok(Ok(h)) => Box::into_raw(Box::new(h)),
        Ok(Err(e)) => { set_error(&e); std::ptr::null_mut() }
        Err(_) => { set_error("panic in fpk_open"); std::ptr::null_mut() }
    }
}

#[no_mangle]
pub extern "C" fn fpk_call(h: *mut Handle, req_json: *const c_char) -> *mut c_char {
    let r = catch_unwind(AssertUnwindSafe(|| {
        if h.is_null() {
            return json!({"ok": false, "error": "null handle"});
        }
        match cstr_arg(req_json, "request") {
            Ok(req) => dispatch(unsafe { &mut *h }, req),
            Err(e) => json!({"ok": false, "error": e}),
        }
    }));
    let reply = r.unwrap_or_else(|_| json!({"ok": false, "error": "panic in fpk_call"}));
    to_c(reply.to_string())
}

#[no_mangle]
pub extern "C" fn fpk_raster(_h: *mut Handle, _req_json: *const c_char, _buf: *mut f32, _len: usize) -> i64 {
    let r = catch_unwind(AssertUnwindSafe(|| {
        set_error("fpk_raster: not implemented before K12");
        -1i64
    }));
    r.unwrap_or(-1)
}

#[no_mangle]
pub extern "C" fn fpk_close(h: *mut Handle) {
    if !h.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| unsafe { drop(Box::from_raw(h)) }));
    }
}

#[no_mangle]
pub extern "C" fn fpk_free_str(p: *mut c_char) {
    if !p.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| unsafe { drop(CString::from_raw(p)) }));
    }
}

#[no_mangle]
pub extern "C" fn fpk_last_error() -> *const c_char {
    LAST_ERROR.with(|e| e.borrow().as_ptr())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ping_and_unknown_op() {
        let mut h = Handle { path: "x".into(), opts: json!({}), board: board::Board::new(vec![]), rules: rules::Rules::fallback(), dru: dru::Dru::default(), load_ms: 0.0 };
        assert_eq!(dispatch(&mut h, r#"{"op":"ping"}"#)["pong"], true);
        assert_eq!(dispatch(&mut h, r#"{"op":"nope"}"#)["ok"], false);
        assert_eq!(dispatch(&mut h, "not json")["ok"], false);
    }
}
