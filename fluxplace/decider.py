"""AI decider: answers a structured flow question (questions.py shape) instead of stopping for a person.

    [ai] decide = "on"          # off (default): open questions stop the stage (exit 3) as before
    [ai] rule   = "..."         # what to optimise; default RULE
    [ai] decide_cmd = [...]     # model command, prompt on stdin, answer on stdout (env FLUXPLACE_AI_MODEL = the model)
    [ai] models = ["haiku", "sonnet"]   # tried in order: the next one only when an answer is unparsable or cites nothing
    [ai] decide_timeout_s = 120

decide() sends one small prompt (question, options with consequences, default, evidence lines, rule) and expects
{"choice", "rationale", "evidence"}.  Every decision is appended to `<dir>/decisions.jsonl` and mirrored as an [answers]
table in `<dir>/decisions.answers.toml`; a question whose hash (id, text, options, evidence, rule) is already in the log is
answered from it without a model call (logged with cached = true), so a re-run is deterministic and reviewable.  No model (FLUXPLACE_AI_OFFLINE=1, no
command, timeout, unparsable or off-menu answer): the question's default, marked "default, not reasoned".  Never raises."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time

RULE = "fewest FAILs without growing the outline or adding layers"
DEFAULT_CMD = ["claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config"]   # no tools: evidence is inline
MODELS = ["haiku", "sonnet"]
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


def _models(cons):
    return [str(x) for x in _ai(cons).get("models") or MODELS]


def _cmd(cons):
    c = _ai(cons).get("decide_cmd") or os.environ.get("FLUXPLACE_AI_CMD") or DEFAULT_CMD
    return shlex.split(c) if isinstance(c, str) else [str(x) for x in c]


def call_model(text, cons, model, tools=None, cwd=None, timeout=None, info=None):
    """-> (answer text or None, why not).  The CLI's JSON envelope ({"result": ...}) is unwrapped.
    tools: the built-in tools the model may use ("Read" for page pictures in `cwd`, "WebSearch,WebFetch" to find documents; default
    none), run without session or project settings; info: a dict that receives cost_usd (the CLI's total_cost_usd)."""
    if os.environ.get("FLUXPLACE_AI_OFFLINE"):
        return None, "offline (FLUXPLACE_AI_OFFLINE)"
    cmd = _cmd(cons)
    if not cmd or not shutil.which(cmd[0]):
        return None, f"no model command ({cmd[0] if cmd else 'empty'})"
    if cmd == DEFAULT_CMD:
        cmd = cmd + ["--model", model]
        if tools is not None:
            cmd[cmd.index("--tools") + 1] = tools
            cmd += ["--no-session-persistence", "--setting-sources", ""] + (["--allowedTools", tools] if tools else [])   # -p asks for no permission
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, cwd=cwd,
                           timeout=float(timeout or _ai(cons).get("decide_timeout_s", 120)), env=dict(os.environ, FLUXPLACE_AI_MODEL=model))
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"model call failed ({type(e).__name__})"
    if r.returncode != 0:
        return None, f"model exit {r.returncode}"
    try:
        env = json.loads(r.stdout)
    except ValueError:
        return r.stdout, ""
    if info is not None and isinstance(env, dict):
        info["cost_usd"] = float(env.get("total_cost_usd") or 0.0)
    return (str(env["result"]) if isinstance(env, dict) and "result" in env else r.stdout), ""


def _first_obj(s, ok):
    """the first JSON object in `s` that `ok` accepts, else None."""
    s = s or ""
    i = s.find("{")
    while i >= 0:
        try:
            d, _ = json.JSONDecoder().raw_decode(s[i:])
        except ValueError:
            d = None
        if isinstance(d, dict) and ok(d):
            return d
        i = s.find("{", i + 1)
    return None


def parse(out, options):
    """the first JSON object in `out` whose choice is on the menu -> dict, else None."""
    d = _first_obj(out, lambda d: str(d.get("choice")) in options)
    if d is None:
        return None
    ev = d.get("evidence")
    return {"choice": str(d["choice"]), "rationale": str(d.get("rationale") or "")[:400],
            "evidence": [str(x)[:EV_CHARS] for x in (ev if isinstance(ev, list) else [ev] if ev else [])][:8]}


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
        last = {r["id"]: r for r in _log_rows(d) if "envelope" not in r}      # rung envelopes are not [answers]
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
    hit = next((r for r in reversed(_log_rows(log_dir)) if r.get("hash") == h and r.get("reasoned")), None) if log_dir else None
    if hit and hit["choice"] in options:
        row = dict(hit, by="cache", cached=True)
    else:
        text, got, why = prompt(q, evidence, rule_), None, ""
        for m in _models(cons):
            out, why = call_model(text, cons, m)
            if out is None:
                break                                            # no model at all (offline, missing CLI, timeout): no retry
            p = parse(out, options)
            if not p:
                why = "no valid choice in the model answer"
                continue
            got = dict(p, model=m)
            if p["evidence"]:
                break
        if got:
            row = dict(got, by="ai", reasoned=True, cached=False)
        else:
            row = {"choice": q["default"], "by": "default", "reasoned": False, "evidence": [], "cached": False, "model": None,
                   "rationale": "default, not reasoned: " + why}
    row.update(id=q["id"], hash=h, options=options, at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if log_dir:
        _write(log_dir, row)
    return row


def act(packet, cons=None, log_dir=None, key=""):
    """The close `ai` rung without a person: the packet (its own `actions` text is the syntax) goes to the model, which
    answers an action envelope {"actions": [...], "why"}.  -> the envelope, or None (no model / nothing usable: the packet
    stays on disk for an agent, as before).  The envelope is not trusted: close validates and guards it like any answer.
    Logged as id `ai:<key>` in the decision log; cached by packet hash."""
    body = json.dumps(packet, sort_keys=True, default=str)
    h = hashlib.sha1((body + rule(cons)).encode()).hexdigest()[:16]
    hit = next((r for r in reversed(_log_rows(log_dir)) if r.get("hash") == h and r.get("envelope")), None) if log_dir else None
    if hit:
        env, model, cached = hit["envelope"], hit.get("model"), True
    else:
        text = ("You repair one open gap on a routed PCB so the flow needs no person. Rule to optimise: " + rule(cons) + ".\n"
                "The packet below describes the gap, the copper near it and the allowed actions (key `actions`). Use only ids, "
                "nets, layers and coordinates from the packet. If nothing safe exists, answer give_up with the reason.\n"
                "Answer with ONLY the JSON action envelope.\nPacket:\n" + body[:12000])
        env, model, cached = None, None, False
        for m in _models(cons):
            out, _why = call_model(text, cons, m)
            if out is None:
                break
            env = _first_obj(out, lambda d: isinstance(d.get("actions"), list) and d["actions"])
            if env:
                model = m
                break
    if not env:
        return None
    env.setdefault("why", "")
    env["tokens"] = int(env.get("tokens") or 0)
    if log_dir:
        _write(log_dir, {"id": "ai:" + str(key), "hash": h, "choice": ",".join(str(a.get("do")) for a in env["actions"])[:80],
                         "by": "cache" if cached else "ai", "reasoned": True, "cached": cached, "model": model,
                         "rationale": str(env.get("why"))[:400], "evidence": ["packet " + str(key)], "envelope": env,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return env


def ask(qid, topic, found, options, default, settle, evidence=(), cons=None, log_dir=None):
    """decide() for a call site that is not a questions.Session: `options` {choice: consequence}.  -> the decision row."""
    q = {"id": qid, "topic": topic, "found": found, "default": default, "settle": settle,
         "options": [{"choice": k, "consequence": v} for k, v in options.items()]}
    return decide(q, evidence, cons, log_dir)


def cited(row, sources):
    """True when a reasoned decision cites at least one of `sources` (e.g. 'X.pdf#p3') in its evidence."""
    ev = " ".join(row.get("evidence") or [])
    return bool(row.get("reasoned")) and any(s and s in ev for s in sources)
