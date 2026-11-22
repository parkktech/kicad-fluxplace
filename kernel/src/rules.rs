//! Project rules (K7): floors, net classes, class patterns and assignments from the .kicad_pro, effective class
//! of a net, and the fallback constants used without a project. Units nm (mm at the API).
//! Patterns: `*` and `?` globs. A pattern with regex syntax is not evaluated (no regex engine in this crate) and is
//! listed in `unsupported`.

use serde_json::Value;
use std::collections::HashMap;

pub const MM: f64 = 1e6;

fn nm(v: f64) -> i64 {
    (v * MM).round() as i64
}

#[derive(Clone, Debug, PartialEq)]
pub struct Floors {
    pub clr: i64,
    pub track: i64,
    pub via_dia: i64,
    pub via_drill: i64,
    pub hole_clr: i64,
    pub h2h: i64,
    pub edge_clr: i64,
    pub annular: i64,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct NetClass {
    pub name: String,
    pub priority: i64,
    pub clearance: i64,
    pub track: i64,
    pub via_dia: i64,
    pub via_drill: i64,
    pub dp_width: i64,
    pub dp_gap: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct NetRule {
    pub class: String,
    pub width: i64,
    pub clearance: i64,
    pub via_dia: i64,
    pub via_drill: i64,
    pub dp_width: i64,
    pub dp_gap: i64,
}

#[derive(Clone, Debug)]
pub struct Rules {
    pub source: String,
    /// Project floors (or fallback without a project); `set_floors` raises `floors` only.
    pub project: Floors,
    pub floors: Floors,
    pub classes: Vec<NetClass>,
    pub patterns: Vec<(String, String)>,
    pub assignments: HashMap<String, Vec<String>>,
    pub unsupported: Vec<String>,
}

/// Fallback constants (ROUTER-C1 section 6): used without a project and for keys a project leaves out.
pub fn fallback_floors() -> Floors {
    Floors { clr: 0, track: 0, via_dia: nm(0.5), via_drill: nm(0.3), hole_clr: nm(0.25), h2h: nm(0.25),
             edge_clr: nm(0.5), annular: nm(0.1) }
}

fn fallback_default() -> NetClass {
    NetClass { name: "Default".into(), priority: i64::MAX, clearance: nm(0.2), track: nm(0.2),
               via_dia: nm(0.6), via_drill: nm(0.3), dp_width: 0, dp_gap: 0 }
}

fn num(v: &Value, key: &str) -> Option<f64> {
    v.get(key).and_then(Value::as_f64)
}

/// Shell-style glob: `*` any run, `?` one character.
pub fn glob(pat: &str, s: &str) -> bool {
    let (p, t) = (pat.as_bytes(), s.as_bytes());
    let (mut pi, mut ti, mut star, mut mark) = (0usize, 0usize, None, 0usize);
    while ti < t.len() {
        if pi < p.len() && (p[pi] == b'?' || p[pi] == t[ti]) {
            pi += 1;
            ti += 1;
        } else if pi < p.len() && p[pi] == b'*' {
            star = Some(pi);
            mark = ti;
            pi += 1;
        } else if let Some(sp) = star {
            pi = sp + 1;
            mark += 1;
            ti = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == b'*' {
        pi += 1;
    }
    pi == p.len()
}

fn is_glob(pat: &str) -> bool {
    !pat.chars().any(|c| matches!(c, '^' | '$' | '(' | ')' | '[' | ']' | '{' | '}' | '+' | '|' | '\\'))
}

impl Rules {
    /// Rules of a board without a project file.
    pub fn fallback() -> Rules {
        let f = fallback_floors();
        Rules { source: "fallback".into(), project: f.clone(), floors: f, classes: vec![fallback_default()],
                patterns: Vec::new(), assignments: HashMap::new(), unsupported: Vec::new() }
    }

    /// Rules from a parsed .kicad_pro.
    pub fn from_project(pro: &Value) -> Rules {
        let fb = fallback_floors();
        let r = pro.pointer("/board/design_settings/rules").cloned().unwrap_or(Value::Null);
        let f = |k: &str, d: i64| num(&r, k).map(nm).unwrap_or(d);
        let project = Floors {
            clr: f("min_clearance", fb.clr),
            track: f("min_track_width", fb.track),
            via_dia: f("min_via_diameter", fb.via_dia),
            via_drill: f("min_through_hole_diameter", fb.via_drill),
            annular: f("min_via_annular_width", fb.annular),
            hole_clr: f("min_hole_clearance", fb.hole_clr),
            h2h: f("min_hole_to_hole", fb.h2h),
            edge_clr: f("min_copper_edge_clearance", fb.edge_clr),
        };
        let ns = pro.get("net_settings").cloned().unwrap_or(Value::Null);
        let mut classes = Vec::new();
        for c in ns.get("classes").and_then(Value::as_array).cloned().unwrap_or_default() {
            let g = |k: &str| num(&c, k).map(nm).unwrap_or(0);
            classes.push(NetClass {
                name: c.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
                priority: c.get("priority").and_then(Value::as_i64).unwrap_or(i64::MAX - 1),
                clearance: g("clearance"), track: g("track_width"), via_dia: g("via_diameter"),
                via_drill: g("via_drill"), dp_width: g("diff_pair_width"), dp_gap: g("diff_pair_gap"),
            });
        }
        if !classes.iter().any(|c| c.name == "Default") {
            classes.push(fallback_default());
        }
        let mut patterns = Vec::new();
        let mut unsupported = Vec::new();
        for p in ns.get("netclass_patterns").and_then(Value::as_array).cloned().unwrap_or_default() {
            let cls = p.get("netclass").and_then(Value::as_str).unwrap_or("").to_string();
            let pat = p.get("pattern").and_then(Value::as_str).unwrap_or("").to_string();
            if is_glob(&pat) {
                patterns.push((cls, pat));
            } else {
                unsupported.push(format!("netclass pattern {pat:?} (regex) not evaluated"));
            }
        }
        let mut assignments = HashMap::new();
        if let Some(obj) = ns.get("netclass_assignments").and_then(Value::as_object) {
            for (net, v) in obj {
                let list: Vec<String> = v.as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string))
                    .collect()).unwrap_or_default();
                assignments.insert(net.clone(), list);
            }
        }
        Rules { source: "project".into(), floors: project.clone(), project, classes, patterns, assignments, unsupported }
    }

    /// set_floors: the given values raised to at least the project floors.
    pub fn set_floors(&mut self, clr: i64, track: i64, via_dia: i64, via_drill: i64) {
        self.floors = Floors {
            clr: self.project.clr.max(clr), track: self.project.track.max(track),
            via_dia: self.project.via_dia.max(via_dia), via_drill: self.project.via_drill.max(via_drill),
            ..self.floors.clone()
        };
    }

    /// Effective class of a net: matching classes by priority (smaller first, file order on ties), Default last;
    /// each parameter from the first class that sets it (> 0); unset parameters fall back to the fallback constants.
    /// The result is never below the floors.
    pub fn net_rule(&self, net: &str) -> NetRule {
        let mut matched: Vec<usize> = Vec::new();
        for (cls, pat) in &self.patterns {
            if glob(pat, net) {
                matched.extend(self.classes.iter().position(|c| &c.name == cls));
            }
        }
        if let Some(list) = self.assignments.get(net) {
            for name in list {
                matched.extend(self.classes.iter().position(|c| &c.name == name));
            }
        }
        matched.sort_unstable();
        matched.dedup();
        let mut order: Vec<usize> = matched.into_iter().filter(|&i| self.classes[i].name != "Default").collect();
        order.sort_by_key(|&i| (self.classes[i].priority, i));
        // Default is consulted (and listed) only when a parameter is still unset by the matched classes
        let getters: [fn(&NetClass) -> i64; 6] = [|c| c.clearance, |c| c.track, |c| c.via_dia, |c| c.via_drill,
                                                  |c| c.dp_width, |c| c.dp_gap];
        let covered = getters.iter().all(|g| order.iter().any(|&i| g(&self.classes[i]) > 0));
        if !covered {
            if let Some(d) = self.classes.iter().position(|c| c.name == "Default") {
                order.push(d);
            }
        }
        let pick = |get: fn(&NetClass) -> i64| order.iter().map(|&i| get(&self.classes[i])).find(|&v| v > 0).unwrap_or(0);
        let fb = fallback_default();
        let or_fb = |v: i64, d: i64| if v > 0 { v } else { d };
        NetRule {
            // pcbnew's effective class name: the ordered class names, comma-joined ("HS,Default")
            class: if order.is_empty() { "Default".into() } else {
                order.iter().map(|&i| self.classes[i].name.as_str()).collect::<Vec<_>>().join(",")
            },
            width: or_fb(pick(|c| c.track), fb.track).max(self.floors.track),
            clearance: or_fb(pick(|c| c.clearance), fb.clearance).max(self.floors.clr),
            via_dia: or_fb(pick(|c| c.via_dia), fb.via_dia).max(self.floors.via_dia),
            via_drill: or_fb(pick(|c| c.via_drill), fb.via_drill).max(self.floors.via_drill),
            dp_width: pick(|c| c.dp_width),
            dp_gap: pick(|c| c.dp_gap),
        }
    }
}

/// Rules for a board: the project at `path` (the .kicad_pro beside the board, or `project` from the options).
pub fn load(project: Option<&str>) -> Result<Rules, String> {
    match project {
        None => Ok(Rules::fallback()),
        Some(p) => {
            let text = std::fs::read_to_string(p).map_err(|e| format!("cannot read project {p}: {e}"))?;
            let v: Value = serde_json::from_str(&text).map_err(|e| format!("project {p}: {e}"))?;
            Ok(Rules::from_project(&v))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn globs() {
        assert!(glob("SIG*", "SIG_1") && glob("S?G", "SIG") && glob("*", ""));
        assert!(!glob("SIG?", "SIG") && !glob("A*B", "AC"));
        assert!(glob("*_P*", "USB_P1_x"));
    }

    #[test]
    fn effective_class_priority_and_default_last() {
        let pro: Value = serde_json::json!({
            "board": {"design_settings": {"rules": {"min_clearance": 0.1, "min_track_width": 0.15}}},
            "net_settings": {
                "classes": [
                    {"name": "Default", "clearance": 0.2, "track_width": 0.2},
                    {"name": "HS", "priority": 1, "clearance": 0.15, "track_width": 0.3, "via_diameter": 0.7, "via_drill": 0.35},
                    {"name": "PWR", "priority": 1, "track_width": 0.5}
                ],
                "netclass_patterns": [{"netclass": "HS", "pattern": "SIG*"}, {"netclass": "PWR", "pattern": "SIG*"}]
            }});
        let r = Rules::from_project(&pro);
        let n = r.net_rule("SIG_1");
        assert_eq!(n.class, "HS,PWR,Default");           // both classes match SIG*; priority, then file order; Default last
        assert_eq!(n.width, 300_000);
        assert_eq!(n.clearance, 150_000);
        assert_eq!(n.via_dia, 700_000);
        let d = r.net_rule("GND");
        assert_eq!((d.class.as_str(), d.width, d.clearance), ("Default", 200_000, 200_000));
    }

    #[test]
    fn floors_clamp_and_set_floors_raises_only() {
        let mut r = Rules::fallback();
        r.set_floors(150_000, 0, 0, 0);
        assert_eq!(r.floors.clr, 150_000);
        assert_eq!(r.net_rule("X").clearance, 200_000);  // class 0.2 is above the floor
        assert_eq!(r.project.clr, 0);
    }
}
