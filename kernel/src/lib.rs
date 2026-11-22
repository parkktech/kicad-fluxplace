//! C ABI and JSON dispatch. Every entry point runs under catch_unwind: no panic crosses the boundary.
//! Reply shape: {"ok": true, ...} or {"ok": false, "error": "..."}.

pub mod board;
pub mod geom;
pub mod load;
pub mod load_fp;
pub mod load_zone;
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
    Ok(Handle { path: path.to_string(), opts, board, load_ms: t0.elapsed().as_secs_f64() * 1e3 })
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
pub fn dispatch(h: &Handle, req: &str) -> Value {
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
            json!({"ok": true, "path": h.path, "file_version": b.file_version, "layers": b.copper,
                   "nets": b.nets.len() - 1, "items": live, "load_ms": h.load_ms,
                   "zones": b.zones.len(), "fill_stale": b.fill_stale,
                   "unsupported": b.unsupported.iter().map(|u| json!({"what": u.what, "where": u.at, "effect": u.effect})).collect::<Vec<_>>()})
        }
        "items" => items_op(h, &v),
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
            Ok(req) => dispatch(unsafe { &*h }, req),
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
        let h = Handle { path: "x".into(), opts: json!({}), board: board::Board::new(vec![]), load_ms: 0.0 };
        assert_eq!(dispatch(&h, r#"{"op":"ping"}"#)["pong"], true);
        assert_eq!(dispatch(&h, r#"{"op":"nope"}"#)["ok"], false);
        assert_eq!(dispatch(&h, "not json")["ok"], false);
    }
}
