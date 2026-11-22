//! .kicad_dru subset (K8): rules, a three-valued condition evaluator (true, false, may), units, and the coverage
//! report. Conditions on anything not modelled evaluate to `may`: a clearance-like rule whose condition may match
//! only raises the required value (K9); a rule with `may` on a non-copper constraint is listed, never dropped silently.

use crate::sexpr::{self, Node};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tri {
    True,
    False,
    May,
}

impl Tri {
    fn not(self) -> Tri {
        match self {
            Tri::True => Tri::False,
            Tri::False => Tri::True,
            Tri::May => Tri::May,
        }
    }
    fn and(self, o: Tri) -> Tri {
        if self == Tri::False || o == Tri::False {
            Tri::False
        } else if self == Tri::May || o == Tri::May {
            Tri::May
        } else {
            Tri::True
        }
    }
    pub fn or(self, o: Tri) -> Tri {
        if self == Tri::True || o == Tri::True {
            Tri::True
        } else if self == Tri::May || o == Tri::May {
            Tri::May
        } else {
            Tri::False
        }
    }
    fn from(b: bool) -> Tri {
        if b { Tri::True } else { Tri::False }
    }
}

/// What the evaluator knows about one side (A or B) of a pair.
#[derive(Clone, Debug, Default)]
pub struct Facts {
    pub kind: String,
    pub net_name: String,
    pub net_class: String,
    pub netclasses: Vec<String>,
    pub layer: String,
    pub plated: bool,
    pub pad_type: String,
    pub via_type: String,
    pub diff_pair: Option<String>,
    pub coupled: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Expr {
    Or(Vec<Expr>),
    And(Vec<Expr>),
    Not(Box<Expr>),
    Lit(Tri),
    /// side (true = A), property, optional literal, equality (true = ==, false = !=); None = function call
    Prop { a: bool, name: String, arg: Option<String>, eq: bool },
    Call { a: bool, name: String, arg: Option<String> },
}

#[derive(Clone, Debug, PartialEq)]
enum Tok {
    Ident(String),
    Str(String),
    Op(&'static str),
}

fn lex(s: &str) -> Result<Vec<Tok>, String> {
    let b: Vec<char> = s.chars().collect();
    let mut i = 0;
    let mut out = Vec::new();
    while i < b.len() {
        let c = b[i];
        if c.is_whitespace() {
            i += 1;
        } else if c == '\'' {
            let j = b[i + 1..].iter().position(|&x| x == '\'').ok_or("unterminated string")? + i + 1;
            out.push(Tok::Str(b[i + 1..j].iter().collect()));
            i = j + 1;
        } else if c.is_alphanumeric() || c == '_' {
            let j = (i..b.len()).find(|&k| !(b[k].is_alphanumeric() || b[k] == '_')).unwrap_or(b.len());
            out.push(Tok::Ident(b[i..j].iter().collect()));
            i = j;
        } else {
            let two: String = b[i..(i + 2).min(b.len())].iter().collect();
            let op = match two.as_str() {
                "==" => Some("=="),
                "!=" => Some("!="),
                "&&" => Some("&&"),
                "||" => Some("||"),
                _ => None,
            };
            if let Some(o) = op {
                out.push(Tok::Op(o));
                i += 2;
            } else {
                let o = match c {
                    '!' => "!", '(' => "(", ')' => ")", '.' => ".", ',' => ",",
                    _ => return Err(format!("unexpected character {c:?}")),
                };
                out.push(Tok::Op(o));
                i += 1;
            }
        }
    }
    Ok(out)
}

struct P {
    t: Vec<Tok>,
    i: usize,
}

impl P {
    fn peek(&self) -> Option<&Tok> {
        self.t.get(self.i)
    }
    fn eat(&mut self, op: &str) -> bool {
        if self.peek() == Some(&Tok::Op(leak(op))) {
            self.i += 1;
            true
        } else {
            false
        }
    }
    fn or(&mut self) -> Result<Expr, String> {
        let mut v = vec![self.and()?];
        while self.eat("||") {
            v.push(self.and()?);
        }
        Ok(if v.len() == 1 { v.pop().unwrap() } else { Expr::Or(v) })
    }
    fn and(&mut self) -> Result<Expr, String> {
        let mut v = vec![self.unary()?];
        while self.eat("&&") {
            v.push(self.unary()?);
        }
        Ok(if v.len() == 1 { v.pop().unwrap() } else { Expr::And(v) })
    }
    fn unary(&mut self) -> Result<Expr, String> {
        if self.eat("!") {
            return Ok(Expr::Not(Box::new(self.unary()?)));
        }
        self.primary()
    }
    fn primary(&mut self) -> Result<Expr, String> {
        if self.eat("(") {
            let e = self.or()?;
            if !self.eat(")") {
                return Err("missing )".into());
            }
            return Ok(e);
        }
        let side = match self.peek() {
            Some(Tok::Ident(s)) if s == "A" || s == "B" => s == "A",
            Some(Tok::Ident(s)) if s == "true" => { self.i += 1; return Ok(Expr::Lit(Tri::True)); }
            Some(Tok::Ident(s)) if s == "false" => { self.i += 1; return Ok(Expr::Lit(Tri::False)); }
            _ => return Err(format!("expected A, B or a literal at token {}", self.i)),
        };
        self.i += 1;
        if !self.eat(".") {
            return Err("expected .".into());
        }
        let name = match self.peek().cloned() {
            Some(Tok::Ident(n)) => { self.i += 1; n }
            _ => return Err("expected a name after .".into()),
        };
        if self.eat("(") {
            let arg = match self.peek().cloned() {
                Some(Tok::Str(s)) => { self.i += 1; Some(s) }
                _ => None,
            };
            if !self.eat(")") {
                return Err("missing ) after call".into());
            }
            return Ok(Expr::Call { a: side, name, arg });
        }
        let eq = match self.peek() {
            Some(Tok::Op("==")) => true,
            Some(Tok::Op("!=")) => false,
            _ => return Ok(Expr::Call { a: side, name, arg: None }),
        };
        self.i += 1;
        match self.peek().cloned() {
            Some(Tok::Str(s)) => {
                self.i += 1;
                Ok(Expr::Prop { a: side, name, arg: Some(s), eq })
            }
            _ => Err("comparison needs a quoted value".into()),
        }
    }
}

fn leak(s: &str) -> &'static str {
    match s {
        "==" => "==", "!=" => "!=", "&&" => "&&", "||" => "||", "!" => "!", "(" => "(", ")" => ")", "." => ".", _ => "?",
    }
}

pub fn parse_condition(src: &str) -> Result<Expr, String> {
    let mut p = P { t: lex(src)?, i: 0 };
    let e = p.or()?;
    if p.i != p.t.len() {
        return Err("trailing tokens".into());
    }
    Ok(e)
}

fn wild(pat: &str, s: &str) -> bool {
    crate::rules::glob(pat, s)
}

fn prop_value<'a>(f: &'a Facts, name: &str) -> Option<&'a str> {
    match name {
        "NetClass" => Some(&f.net_class),
        "NetName" => Some(&f.net_name),
        "Layer" => Some(&f.layer),
        "Pad_Type" => Some(&f.pad_type),
        "Via_Type" => Some(&f.via_type),
        "Type" => Some(&f.kind),
        _ => None,
    }
}

/// Evaluate an expression for a pair (a, b). Unknown properties and functions are `May`.
pub fn eval(e: &Expr, a: &Facts, b: &Facts) -> Tri {
    match e {
        Expr::Or(v) => v.iter().fold(Tri::False, |acc, x| acc.or(eval(x, a, b))),
        Expr::And(v) => v.iter().fold(Tri::True, |acc, x| acc.and(eval(x, a, b))),
        Expr::Not(x) => eval(x, a, b).not(),
        Expr::Lit(t) => *t,
        Expr::Prop { a: side, name, arg, eq } => {
            let f = if *side { a } else { b };
            let Some(lit) = arg else { return Tri::May };
            // Pad_Type / Via_Type are not known for other item kinds: unknown, not false
            if matches!(name.as_str(), "Pad_Type" | "Via_Type") && prop_value(f, name) == Some("") {
                return Tri::May;
            }
            let hit = match name.as_str() {
                "Type" => lit.split('|').any(|opt| opt == f.kind),
                "NetName" => wild(lit, &f.net_name),
                _ => match prop_value(f, name) {
                    Some(v) => v == lit,
                    None => return Tri::May,
                },
            };
            Tri::from(hit == *eq)
        }
        Expr::Call { a: side, name, arg } => {
            let f = if *side { a } else { b };
            match (name.as_str(), arg) {
                ("hasNetclass", Some(c)) => Tri::from(f.netclasses.iter().any(|n| n == c)),
                ("inDiffPair", Some(n)) => Tri::from(f.diff_pair.as_deref().map_or(false, |d| wild(n, d))),
                ("isCoupledDiffPair", None) => Tri::from(f.coupled),
                ("isPlated", None) => Tri::from(f.plated),
                _ => Tri::May,
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Constraint {
    pub kind: String,
    pub min: Option<i64>,
    pub opt: Option<i64>,
    pub max: Option<i64>,
}

#[derive(Clone, Debug)]
pub struct Rule {
    pub name: String,
    pub line: usize,
    /// None = any layer; Some("outer" | "inner" | a layer name)
    pub layer: Option<String>,
    pub cond: Option<Expr>,
    pub constraints: Vec<Constraint>,
}

#[derive(Clone, Debug, Default)]
pub struct Dru {
    pub rules: Vec<Rule>,
    pub unsupported: Vec<String>,
    pub coverage: String,
}

/// Length with unit: mm (default), mil, in, um -> nm.
pub fn parse_len(s: &str) -> Option<i64> {
    let (num, mult) = if let Some(v) = s.strip_suffix("mm") {
        (v, 1e6)
    } else if let Some(v) = s.strip_suffix("mil") {
        (v, 25.4e3)
    } else if let Some(v) = s.strip_suffix("in") {
        (v, 25.4e6)
    } else if let Some(v) = s.strip_suffix("um") {
        (v, 1e3)
    } else {
        (s, 1e6)
    };
    num.parse::<f64>().ok().map(|v| (v * mult).round() as i64)
}

/// Constraint kinds that restrict copper and are modelled (K9) or deliberately ignored, versus the rest.
const COPPER: &[&str] = &["clearance", "track_width", "hole_clearance", "hole_to_hole", "hole_size", "via_diameter",
                          "annular_width", "edge_clearance"];
const NON_COPPER: &[&str] = &["courtyard_clearance", "silk_clearance", "silk_text", "silk_text_height",
                              "silk_text_thickness", "text_height", "text_thickness", "footprint_clearance"];

/// Names of the functions and properties an expression uses that the evaluator does not know (they are `may`).
fn unknown_atoms(e: &Expr, out: &mut Vec<String>) {
    match e {
        Expr::Or(v) | Expr::And(v) => v.iter().for_each(|x| unknown_atoms(x, out)),
        Expr::Not(x) => unknown_atoms(x, out),
        Expr::Lit(_) => {}
        Expr::Prop { name, .. } => {
            if !matches!(name.as_str(), "NetClass" | "NetName" | "Layer" | "Pad_Type" | "Via_Type" | "Type") {
                out.push(name.clone());
            }
        }
        Expr::Call { name, arg, .. } => {
            let known = matches!((name.as_str(), arg.is_some()), ("hasNetclass", true) | ("inDiffPair", true)
                | ("isCoupledDiffPair", false) | ("isPlated", false));
            if !known {
                out.push(name.clone());
            }
        }
    }
}

fn layer_of(n: &Node) -> Option<String> {
    n.first("layer").and_then(|l| l.str_at(1)).map(str::to_string)
}

pub fn parse(text: &str) -> Result<Dru, String> {
    // a .kicad_dru holds several top-level forms: parse them as the children of one wrapper list
    let wrapped = format!("({text}\n)");
    let root = sexpr::parse(&wrapped)?;
    let mut dru = Dru::default();
    let mut partial = false;
    for r in root.kids("rule") {
        let name = r.str_at(1).unwrap_or("").to_string();
        let line = 1 + wrapped[..r.span.0].bytes().filter(|&b| b == b'\n').count();
        if r.first("severity").and_then(|s| s.str_at(1)) == Some("ignore") {
            continue;
        }
        let cond = match r.first("condition").and_then(|c| c.str_at(1)) {
            None => None,
            Some(src) => match parse_condition(src) {
                Ok(e) => Some(e),
                Err(e) => {
                    dru.unsupported.push(format!("rule {name} (line {line}): condition not parsed ({e}); rule may apply"));
                    partial = true;
                    Some(Expr::Lit(Tri::May))
                }
            },
        };
        let mut constraints = Vec::new();
        for c in r.kids("constraint") {
            let kind = c.str_at(1).unwrap_or("").to_string();
            if NON_COPPER.contains(&kind.as_str()) {
                continue;
            }
            if !COPPER.contains(&kind.as_str()) {
                dru.unsupported.push(format!("rule {name} (line {line}): constraint {kind} not modelled"));
                partial = true;
                continue;
            }
            let val = |tag: &str| c.first(tag).and_then(|v| v.str_at(1)).and_then(parse_len);
            constraints.push(Constraint { kind, min: val("min"), opt: val("opt"), max: val("max") });
        }
        if let Some(e) = &cond {
            let mut unk = Vec::new();
            unknown_atoms(e, &mut unk);
            for u in unk {
                // a condition we cannot evaluate: the rule stays, and only raises clearance-like values (K9)
                dru.unsupported.push(format!("rule {name} (line {line}): condition uses {u}; rule may apply, only raises"));
                partial = true;
            }
        }
        dru.rules.push(Rule { name, line, layer: layer_of(r), cond, constraints });
    }
    dru.coverage = if partial || !dru.unsupported.is_empty() {
        "partial".into()
    } else if dru.rules.is_empty() {
        "none".into()
    } else {
        "full".into()
    };
    Ok(dru)
}

/// Rule constraint value for `kind`: the minimum (clearance-like: the largest applicable value wins in K9).
pub fn min_of(rule: &Rule, kind: &str) -> Option<i64> {
    rule.constraints.iter().find(|c| c.kind == kind).and_then(|c| c.min)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(kind: &str, net: &str, class: &str, layer: &str) -> Facts {
        Facts { kind: kind.into(), net_name: net.into(), net_class: class.into(), layer: layer.into(),
                netclasses: vec![class.into()], ..Default::default() }
    }

    fn ev(src: &str, a: &Facts, b: &Facts) -> Tri {
        eval(&parse_condition(src).unwrap(), a, b)
    }

    #[test]
    fn atoms_on_both_sides() {
        let a = f("Track", "GND", "HS", "F.Cu");
        let b = f("Pad", "SIG_1", "Default", "F.Cu");
        assert_eq!(ev("A.NetClass == 'HS'", &a, &b), Tri::True);
        assert_eq!(ev("B.NetClass != 'HS'", &a, &b), Tri::True);
        assert_eq!(ev("A.NetName == 'GND'", &a, &b), Tri::True);
        assert_eq!(ev("B.NetName == 'SIG*'", &a, &b), Tri::True);
        assert_eq!(ev("A.Type == 'Track|Via'", &a, &b), Tri::True);
        assert_eq!(ev("B.Type == 'Track|Via'", &a, &b), Tri::False);
        assert_eq!(ev("A.Layer == 'In1.Cu'", &a, &b), Tri::False);
        assert_eq!(ev("A.hasNetclass('HS')", &a, &b), Tri::True);
        assert_eq!(ev("B.isPlated()", &a, &b), Tri::False);
        assert_eq!(ev("A.inDiffPair('USB*')", &a, &b), Tri::False);
        assert_eq!(ev("A.isCoupledDiffPair()", &a, &b), Tri::False);
        assert_eq!(ev("B.Pad_Type == 'SMD'", &a, &b), Tri::May, "not known for this fact set");
        assert_eq!(ev("A.insideArea('x')", &a, &b), Tri::May);
    }

    #[test]
    fn logic_with_may() {
        let a = f("Track", "GND", "HS", "F.Cu");
        let b = f("Track", "SIG", "HS", "F.Cu");
        assert_eq!(ev("A.NetName == 'GND' && B.NetName == 'SIG'", &a, &b), Tri::True);
        assert_eq!(ev("A.NetName == 'X' || B.NetName == 'SIG'", &a, &b), Tri::True);
        assert_eq!(ev("!(A.NetName == 'GND')", &a, &b), Tri::False);
        assert_eq!(ev("A.insideArea('x') && A.NetName == 'X'", &a, &b), Tri::False, "false dominates may");
        assert_eq!(ev("A.insideArea('x') || A.NetName == 'X'", &a, &b), Tri::May);
        assert_eq!(ev("!A.insideArea('x')", &a, &b), Tri::May);
        assert_eq!(ev("true && (false || B.NetName == 'SIG')", &a, &b), Tri::True);
    }

    #[test]
    fn units_and_severity_and_parse() {
        assert_eq!(parse_len("0.2mm"), Some(200_000));
        assert_eq!(parse_len("5mil"), Some(127_000));
        assert_eq!(parse_len("0.1in"), Some(2_540_000));
        assert_eq!(parse_len("100um"), Some(100_000));
        assert_eq!(parse_len("0.15"), Some(150_000));
        let src = "(version 1)\n(rule A (condition \"A.NetClass == 'HS'\") (constraint clearance (min 0.25mm)))\n\
(rule IGN (severity ignore) (constraint clearance (min 9mm)))\n\
(rule C (layer outer) (constraint track_width (min 5mil)) (constraint courtyard_clearance (min 0.2mm)))\n\
(rule D (condition \"A.insideArea('x')\") (constraint clearance (min 0.3mm)))\n\
(rule E (constraint length (max 10mm)))";
        let d = parse(src).unwrap();
        assert_eq!(d.rules.len(), 4, "ignored rule dropped");
        assert_eq!(min_of(&d.rules[0], "clearance"), Some(250_000));
        assert_eq!(d.rules[1].layer.as_deref(), Some("outer"));
        assert_eq!(d.rules[1].constraints.len(), 1, "non-copper constraint ignored");
        assert_eq!(d.coverage, "partial");
        assert!(d.unsupported.iter().any(|u| u.contains("length")));
        assert!(d.unsupported.iter().any(|u| u.contains("rule D") && u.contains("insideArea")));
        assert_eq!(parse(src.lines().next().unwrap()).unwrap().coverage, "none");
    }

    #[test]
    fn demo_rule_files_parse_when_present() {
        for p in ["/usr/share/kicad/demos/vme-wren/vme-wren.kicad_dru",
                  "/usr/share/kicad/demos/jetson-agx-thor-baseboard/jetson-agx-thor-baseboard.kicad_dru"] {
            let Ok(text) = std::fs::read_to_string(p) else { continue };
            let d = parse(&text).unwrap_or_else(|e| panic!("{p}: {e}"));
            assert!(!d.coverage.is_empty());
            eprintln!("{p}: {} rules, coverage {}, {} unsupported", d.rules.len(), d.coverage, d.unsupported.len());
        }
    }
}
