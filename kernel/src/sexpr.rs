//! KiCad s-expression parser. Tokens: parens, quoted strings (with \" \\ \n \t escapes), numbers, symbols.
//! Every node keeps its byte span in the source. Numbers stay text; `nm` converts a decimal mm string to integer nm
//! exactly (no float). Errors carry the line number.

#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    List(Vec<Node>),
    Sym(String),
    Str(String),
    Num(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Node {
    pub kind: Kind,
    pub span: (usize, usize),
}

pub fn parse(src: &str) -> Result<Node, String> {
    let mut p = Parser { s: src.as_bytes(), i: 0 };
    let n = p.node()?;
    p.skip_ws();
    if p.i != p.s.len() {
        return Err(p.err("trailing text after the root list"));
    }
    if !matches!(n.kind, Kind::List(_)) {
        return Err("root must be a list".into());
    }
    Ok(n)
}

/// Exact decimal mm string -> integer nm. Up to 6 decimals; a 7th decimal rounds half up. Errors on non-numbers.
pub fn nm(s: &str) -> Result<i64, String> {
    let bad = || format!("not a number: {s}");
    let (neg, body) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let (ip, fp) = match body.find('.') {
        Some(k) => (&body[..k], &body[k + 1..]),
        None => (body, ""),
    };
    if (ip.is_empty() && fp.is_empty()) || !ip.bytes().all(|b| b.is_ascii_digit()) || !fp.bytes().all(|b| b.is_ascii_digit()) {
        return Err(bad());
    }
    let int: i64 = if ip.is_empty() { 0 } else { ip.parse().map_err(|_| bad())? };
    let mut frac: i64 = 0;
    for b in fp.bytes().take(6) {
        frac = frac * 10 + i64::from(b - b'0');
    }
    for _ in fp.len().min(6)..6 {
        frac *= 10;
    }
    let round = i64::from(fp.len() > 6 && fp.as_bytes()[6] >= b'5');
    let mag = int.checked_mul(1_000_000).and_then(|v| v.checked_add(frac + round)).ok_or_else(bad)?;
    Ok(if neg { -mag } else { mag })
}

fn is_number(t: &[u8]) -> bool {
    let body = match t.first() {
        Some(b'-') | Some(b'+') => &t[1..],
        _ => t,
    };
    let dot = body.iter().position(|&b| b == b'.');
    let (ip, fp) = match dot {
        Some(k) => (&body[..k], &body[k + 1..]),
        None => (body, &body[..0]),
    };
    !(ip.is_empty() && fp.is_empty()) && ip.iter().all(u8::is_ascii_digit) && fp.iter().all(u8::is_ascii_digit)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn line(&self, at: usize) -> usize {
        1 + self.s[..at.min(self.s.len())].iter().filter(|&&b| b == b'\n').count()
    }

    fn err(&self, msg: &str) -> String {
        format!("line {}: {msg}", self.line(self.i))
    }

    fn skip_ws(&mut self) {
        while self.i < self.s.len() && self.s[self.i].is_ascii_whitespace() {
            self.i += 1;
        }
    }

    fn node(&mut self) -> Result<Node, String> {
        self.skip_ws();
        match self.s.get(self.i) {
            None => Err(self.err("unexpected end of file")),
            Some(b'(') => {
                let start = self.i;
                self.i += 1;
                let mut kids = Vec::new();
                loop {
                    self.skip_ws();
                    match self.s.get(self.i) {
                        None => return Err(format!("line {}: unclosed '(' ", self.line(start))),
                        Some(b')') => {
                            self.i += 1;
                            break;
                        }
                        Some(_) => kids.push(self.node()?),
                    }
                }
                Ok(Node { kind: Kind::List(kids), span: (start, self.i) })
            }
            Some(b')') => Err(self.err("unexpected ')'")),
            Some(b'"') => self.string(),
            Some(_) => self.atom(),
        }
    }

    fn string(&mut self) -> Result<Node, String> {
        let start = self.i;
        self.i += 1;
        let mut out = Vec::new();
        loop {
            match self.s.get(self.i) {
                None => return Err(format!("line {}: unterminated string", self.line(start))),
                Some(b'"') => {
                    self.i += 1;
                    break;
                }
                Some(b'\\') => {
                    let esc = self.s.get(self.i + 1).copied();
                    let c = match esc {
                        Some(b'n') => b'\n',
                        Some(b't') => b'\t',
                        Some(c) => c,
                        None => return Err(self.err("unterminated escape")),
                    };
                    out.push(c);
                    self.i += 2;
                }
                Some(&c) => {
                    out.push(c);
                    self.i += 1;
                }
            }
        }
        let text = String::from_utf8(out).map_err(|_| self.err("string is not UTF-8"))?;
        Ok(Node { kind: Kind::Str(text), span: (start, self.i) })
    }

    fn atom(&mut self) -> Result<Node, String> {
        let start = self.i;
        while self.i < self.s.len() {
            let b = self.s[self.i];
            if b.is_ascii_whitespace() || b == b'(' || b == b')' || b == b'"' {
                break;
            }
            self.i += 1;
        }
        let tok = &self.s[start..self.i];
        let text = String::from_utf8(tok.to_vec()).map_err(|_| self.err("token is not UTF-8"))?;
        let kind = if is_number(tok) { Kind::Num(text) } else { Kind::Sym(text) };
        Ok(Node { kind, span: (start, self.i) })
    }
}

impl Node {
    pub fn text<'a>(&self, src: &'a str) -> &'a str {
        &src[self.span.0..self.span.1]
    }

    pub fn children(&self) -> &[Node] {
        match &self.kind {
            Kind::List(v) => v,
            _ => &[],
        }
    }

    /// The head of a list: a symbol ("segment" in (segment ...)) or a number (layer ids in (0 "F.Cu" ...)).
    pub fn tag(&self) -> Option<&str> {
        match self.children().first().map(|n| &n.kind) {
            Some(Kind::Sym(s)) | Some(Kind::Num(s)) => Some(s),
            _ => None,
        }
    }

    /// Child lists whose head is `tag`.
    pub fn kids<'a>(&'a self, tag: &str) -> impl Iterator<Item = &'a Node> + 'a {
        let tag = tag.to_string();
        self.children().iter().filter(move |n| n.tag() == Some(tag.as_str()))
    }

    pub fn first(&self, tag: &str) -> Option<&Node> {
        self.kids(tag).next()
    }

    /// Value of the argument at position i (the head is 0): symbol, string or number text.
    pub fn str_at(&self, i: usize) -> Option<&str> {
        match &self.children().get(i)?.kind {
            Kind::Sym(s) | Kind::Str(s) | Kind::Num(s) => Some(s),
            Kind::List(_) => None,
        }
    }

    /// Non-list arguments after the head, in order.
    pub fn atoms(&self) -> Vec<&Node> {
        self.children().iter().skip(1).filter(|n| !matches!(n.kind, Kind::List(_))).collect()
    }

    /// Numeric arguments after the head, as integer nm.
    pub fn nums(&self) -> Result<Vec<i64>, String> {
        self.atoms().into_iter().filter_map(|n| match &n.kind {
            Kind::Num(s) => Some(nm(s)),
            _ => None,
        }).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "(kicad_pcb (version 20260206) (generator \"pcbnew\")\n\
  (layers\n    (0 \"F.Cu\" signal)\n    (31 \"B.Cu\" signal)\n  )\n\
  (net 0 \"\")\n  (net 1 \"GND\")\n\
  (segment (start 1.475 2) (end -0.9375 3) (width 0.2) (layer \"F.Cu\") (net \"GND\") (uuid \"u-1\"))\n\
  (via (at 4 5) (size 0.6) (drill 0.3) (layers \"F.Cu\" \"B.Cu\") (net \"GND\"))\n\
  (gr_text \"a\\\"b\\\\c\" (at 0 0))\n\
  (footprint \"R\" (at 1 2 90) (pad \"1\" smd rect (at -0.5 0) (size 0.6 0.6)))\n\
  (zone (net 1) (net_name \"GND\") (layer \"F.Cu\") (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))\n\
  (a) (b) (c) (d) (e) (f) (g) (h) (i) (j) (k) (l)\n)\n";

    #[test]
    fn parses_sample_and_walks_it() {
        let root = parse(SAMPLE).unwrap();
        assert_eq!(root.tag(), Some("kicad_pcb"));
        assert_eq!(root.first("version").unwrap().str_at(1), Some("20260206"));
        assert_eq!(root.kids("net").count(), 2);
        let seg = root.first("segment").unwrap();
        assert_eq!(seg.first("start").unwrap().nums().unwrap(), vec![1_475_000, 2_000_000]);
        assert_eq!(seg.first("end").unwrap().nums().unwrap(), vec![-937_500, 3_000_000]);
        assert_eq!(seg.first("net").unwrap().str_at(1), Some("GND"));
        assert_eq!(root.first("layers").unwrap().kids("0").count(), 1);
    }

    #[test]
    fn string_escapes_are_decoded() {
        let root = parse(SAMPLE).unwrap();
        assert_eq!(root.first("gr_text").unwrap().str_at(1), Some("a\"b\\c"));
    }

    #[test]
    fn span_is_the_node_text() {
        let root = parse(SAMPLE).unwrap();
        let via = root.first("via").unwrap();
        assert!(via.text(SAMPLE).starts_with("(via"));
        assert!(via.text(SAMPLE).ends_with(')'));
        assert_eq!(via.text(SAMPLE).len(), via.span.1 - via.span.0);
        let s = root.first("gr_text").unwrap().children()[1].text(SAMPLE);
        assert_eq!(s, "\"a\\\"b\\\\c\"");
    }

    #[test]
    fn nm_is_exact() {
        assert_eq!(nm("1.475").unwrap(), 1_475_000);
        assert_eq!(nm("-0.9375").unwrap(), -937_500);
        assert_eq!(nm("2").unwrap(), 2_000_000);
        assert_eq!(nm("0.0000015").unwrap(), 2);
        assert_eq!(nm("0.0000014").unwrap(), 1);
        assert_eq!(nm("-.5").unwrap(), -500_000);
        assert!(nm("abc").is_err());
        assert!(nm("1.2.3").is_err());
    }

    #[test]
    fn errors_name_the_line() {
        assert!(parse("(a\n(b)").unwrap_err().contains("line"));
        let e = parse("(a\n\n)  )").unwrap_err();
        assert!(e.starts_with("line 3"), "{e}");
        assert!(parse("(a \"open)").unwrap_err().contains("unterminated"));
    }

    #[test]
    fn w14_r5_parses_fast_when_present() {
        let path = std::env::var("FPK_W14_R5").unwrap_or_else(|_| {
            "/var/kicad/utv-comms-bridge/W14/stageF/hand/r5.kicad_pcb".to_string()
        });
        let Ok(src) = std::fs::read_to_string(&path) else { return };
        let t0 = std::time::Instant::now();
        parse(&src).unwrap();
        assert!(t0.elapsed().as_millis() < 400, "parse took {:?}", t0.elapsed());
    }
}
