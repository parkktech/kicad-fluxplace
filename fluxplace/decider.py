"""AI decider: answers a structured flow question (questions.py shape) instead of stopping for a person.

    [ai] decide = "on"          # off (default): open questions stop the stage (exit 3) as before
    [ai] rule   = "..."         # what to optimise; default RULE
    [ai] decide_cmd = [...]     # model command, prompt on stdin, answer on stdout; default `claude -p --model haiku`
    [ai] decide_timeout_s = 90

decide() sends one small prompt (question, options with consequences, default, evidence lines, rule) and expects
{"choice", "rationale", "evidence"}.  Every decision is appended to `<dir>/decisions.jsonl` and mirrored as an [answers]
table in `<dir>/decisions.answers.toml`; a question whose hash (id, text, options, evidence, rule) is already in the log is
answered from it without a model call, so a re-run is deterministic and reviewable.  No model (FLUXPLACE_AI_OFFLINE=1, no
command, timeout, unparsable or off-menu answer): the question's default, marked "default, not reasoned".  Never raises."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time

RULE = "fewest FAILs without growing the outline or adding layers"
DEFAULT_CMD = ["claude", "-p", "--model", "haiku"]
LOG, ANSWERS = "decisions.jsonl", "decisions.answers.toml"
EV_LINES, EV_CHARS = 24, 240                     # prompt budget: evidence lines kept, chars per line


def _ai(cons):
    return (cons or {}).get("ai") or {}


def enabled(cons):
    return str(_ai(cons).get("decide", "off")).lower() in ("on", "true", "1", "yes")


def rule(cons):
    return str(_ai(cons).get("rule") or RULE)


def qhash(q, evidence, rule_):
    body = {k: q.get(k) for k in ("id", "topic", "found", "options", "default", "settle")}
    body.update(evidence=list(evidence or []), rule=rule_)
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def prompt(q, evidence, rule_):
    L = ["You decide one PCB design-flow question so the flow needs no person.",
         f"Rule to optimise: {rule_}.",
         "Pick exactly one option. Base it on the evidence; cite the lines you used.",
         f"Question {q['id']} [{q.get('topic', '')}]: {q.get('found', '')}", "Options:"]
    L += [f"- {o['choice']}: {o.get('consequence', '')}" for o in q["options"]]
    L.append(f"Default when undecided: {q.get('default')}. Settled by: {q.get('settle', '')}.")
    ev = [str(e)[:EV_CHARS] for e in (evidence or [])][:EV_LINES]
    if ev:
        L.append("Evidence:")
        L += [f"- {e}" for e in ev]
    L.append('Answer with ONLY a JSON object: {"choice": "<option>", "rationale": "<= 2 sentences", "evidence": ["<cited line>", ...]}')
    return "\n".join(L)


def _cmd(cons):
    c = _ai(cons).get("decide_cmd") or os.environ.get("FLUXPLACE_AI_CMD") or DEFAULT_CMD
    return shlex.split(c) if isinstance(c, str) else [str(x) for x in c]


def call_model(text, cons):
    """-> (stdout text or None, model label or why not)."""
    if os.environ.get("FLUXPLACE_AI_OFFLINE"):
        return None, "offline (FLUXPLACE_AI_OFFLINE)"
    cmd = _cmd(cons)
    if not cmd or not shutil.which(cmd[0]):
        return None, f"no model command ({cmd[0] if cmd else 'empty'})"
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=float(_ai(cons).get("decide_timeout_s", 90)))
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"model call failed ({type(e).__name__})"
    if r.returncode != 0:
        return None, f"model exit {r.returncode}"
    return r.stdout, " ".join(cmd)[:80]


def parse(out, options):
    """the first JSON object in `out` whose choice is on the menu -> dict, else None."""
    s = out or ""
    i = s.find("{")
    while i >= 0:
        try:
            d, _ = json.JSONDecoder().raw_decode(s[i:])
        except ValueError:
            d = None
        if isinstance(d, dict) and str(d.get("choice")) in options:
            ev = d.get("evidence")
            return {"choice": str(d["choice"]), "rationale": str(d.get("rationale") or "")[:400],
                    "evidence": [str(x)[:EV_CHARS] for x in (ev if isinstance(ev, list) else [ev] if ev else [])][:8]}
        i = s.find("{", i + 1)
    return None


def _log_rows(d):
    try:
        with open(os.path.join(d, LOG)) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return []


def _write(d, row):
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, LOG), "a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        last = {}
        for r in _log_rows(d):
            last[r["id"]] = r
        L = ["# written by fluxplace decider: the latest decision per question ([answers] syntax); log: " + LOG, "[answers]"]
        for qid, r in sorted(last.items()):
            L.append(f"{json.dumps(qid)} = {json.dumps(r['choice'])}   # {r['by']}: {r.get('rationale', '')[:120]}".replace("\n", " "))
        with open(os.path.join(d, ANSWERS), "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def decide(q, evidence=(), cons=None, log_dir=None):
    """-> {id, hash, choice, by ("ai" | "cache" | "default"), reasoned, rationale, evidence, model}.  Never raises."""
    rule_ = rule(cons)
    h = qhash(q, evidence, rule_)
    options = [o["choice"] for o in q["options"]]
    if log_dir:
        hit = next((r for r in reversed(_log_rows(log_dir)) if r.get("hash") == h and r.get("reasoned")), None)
        if hit and hit["choice"] in options:
            return dict(hit, by="cache")
    out, model = call_model(prompt(q, evidence, rule_), cons)
    got = parse(out, options) if out is not None else None
    if got:
        row = dict(got, by="ai", reasoned=True, model=model)
    else:
        row = {"choice": q["default"], "by": "default", "reasoned": False, "evidence": [],
               "rationale": "default, not reasoned: " + (model if out is None else "no valid choice in the model answer"), "model": model}
    row.update(id=q["id"], hash=h, at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if log_dir:
        _write(log_dir, row)
    return row
