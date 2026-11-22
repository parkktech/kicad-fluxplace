//! C ABI and JSON dispatch. Every entry point runs under catch_unwind: no panic crosses the boundary.
//! Reply shape: {"ok": true, ...} or {"ok": false, "error": "..."}.

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

/// Opaque handle. K3 onward holds the loaded board here.
pub struct Handle {
    path: String,
    #[allow(dead_code)]
    opts: Value,
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
    Ok(Handle { path: path.to_string(), opts })
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
        "info" => json!({"ok": true, "path": h.path, "todo": "C1 K3 (board load)"}),
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
        let h = Handle { path: "x".into(), opts: json!({}) };
        assert_eq!(dispatch(&h, r#"{"op":"ping"}"#)["pong"], true);
        assert_eq!(dispatch(&h, r#"{"op":"nope"}"#)["ok"], false);
        assert_eq!(dispatch(&h, "not json")["ok"], false);
    }
}
