"""Model advisor: builds the prompt, calls the client with bounded retries/usage, validates the reply.

What model judgment contributes (and what it does not):
  * contributes: correction wording for deterministic findings, concerns about prose/code the parser could
    not read, triage hints. All of it is ADVISORY (`severity: advisory`, never a blocker).
  * never contributes: status, approval authority, rule mapping, counterexamples. A model claim that is not
    backed by a deterministic finding cannot create or clear a blocker.
Validation discards (and counts) invented rule refs, invented evidence ids, malformed/truncated JSON,
and mutually conflicting claims about the same rule.
"""
import hashlib
import json
import os

from .client import ModelRequest, ModelTimeout

PROMPT_VERSION = "reviewer_prompt/1"
_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "prompts", "reviewer_prompt.md")
CLAIMS = ("defect", "concern", "ok")


def build_prompt(evidence, det_findings):
    with open(_TEMPLATE) as f:
        t = f.read()
    ev = "\n".join(f"{e['id']} [{e['artifact']}@{e['version']} {e['location']}]: " +
                   (("UNPARSED/UNTRUSTED " + json.dumps(e["text"])) if e.get("unparsed") else e["text"]) for e in evidence)
    fd = "\n".join(f"- {f['category']} rule={f.get('rule_ref')} artifact={f['artifact']} at {f['location']}"
                   for f in det_findings if f["severity"] == "blocker") or "(none)"
    return t.replace("{{FINDINGS}}", fd).replace("{{EVIDENCE}}", ev)


def parse_response(resp):
    """-> (outcome, data). outcome: ok | truncated | malformed_json | schema_invalid"""
    if resp.finish_reason == "length":
        return "truncated", None
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError:
        return ("truncated" if resp.text.count("{") > resp.text.count("}") else "malformed_json"), None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return "schema_invalid", None
    for f in data["findings"]:
        if not (isinstance(f, dict) and f.get("claim_type") in CLAIMS and isinstance(f.get("summary"), str)
                and isinstance(f.get("evidence_ids", []), list) and isinstance(f.get("correction", ""), str)
                and (f.get("rule_ref") is None or isinstance(f.get("rule_ref"), str))):
            return "schema_invalid", None
    return "ok", data


def check_findings(data, known_refs, evidence_ids):
    accepted, discarded, conflicts = [], [], []
    ok = []
    for f in data["findings"]:
        if f.get("rule_ref") is not None and f["rule_ref"] not in known_refs:
            discarded.append({"reason": "invented_rule_ref", "finding": f})
        elif any(e not in evidence_ids for e in f.get("evidence_ids", [])):
            discarded.append({"reason": "invented_evidence_id", "finding": f})
        else:
            ok.append(f)
    by_ref = {}
    for f in ok:
        by_ref.setdefault(f.get("rule_ref"), []).append(f)
    for ref, fs in by_ref.items():
        kinds = {f["claim_type"] for f in fs}
        if ref is not None and "ok" in kinds and ("defect" in kinds or "concern" in kinds):
            conflicts.append({"rule_ref": ref, "claims": [f["claim_type"] for f in fs]})
            for f in fs:
                discarded.append({"reason": "conflicting_claims", "finding": f})
        else:
            accepted.extend(fs)
    return accepted, discarded, conflicts


def advise(client, evidence, det_findings, known_refs, cfg):
    sec = {"status": "not_run", "reason": "", "client": getattr(client, "name", None), "prompt_version": PROMPT_VERSION,
           "calls_made": 0, "retries": 0, "calls": [], "accepted": [], "discarded": [], "conflicts": [],
           "opinion_reported": None, "opinion_used": False, "tokens_used": 0}
    if client is None:
        sec["reason"] = "no model client configured"
        return sec
    prompt = build_prompt(evidence, det_findings)
    if len(prompt) > cfg.max_prompt_chars:
        sec["reason"] = f"prompt of {len(prompt)} chars exceeds max_prompt_chars={cfg.max_prompt_chars}; not truncated, model not called"
        return sec
    ids = {e["id"] for e in evidence}
    last = "no attempt made"
    sec["status"] = "failed"
    for attempt in range(cfg.model_max_calls):
        if sec["tokens_used"] >= cfg.model_max_tokens:
            last = f"token budget {cfg.model_max_tokens} exhausted"
            break
        req = ModelRequest(prompt, getattr(client, "model", "unspecified"), cfg.model_temperature, cfg.model_response_tokens,
                           cfg.model_seed, cfg.model_timeout_s, f"req-{attempt + 1}")
        call = {"request": {"model": req.model, "temperature": req.temperature, "max_tokens": req.max_tokens, "seed": req.seed,
                            "timeout_s": req.timeout_s, "request_id": req.request_id, "prompt_version": PROMPT_VERSION,
                            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_chars": len(prompt)}}
        sec["calls_made"] += 1
        if attempt:
            sec["retries"] += 1
        try:
            resp = client.complete(req)
        except ModelTimeout:
            call["outcome"] = "timeout"
            sec["calls"].append(call)
            last = "timeout"
            continue
        sec["tokens_used"] += resp.usage.get("prompt_tokens", 0) + resp.usage.get("completion_tokens", 0)
        call["response"] = {"request_id": resp.request_id, "model": resp.model, "finish_reason": resp.finish_reason,
                            "latency_s": resp.latency_s, "usage": resp.usage, "text": resp.text,
                            "text_sha256": hashlib.sha256(resp.text.encode()).hexdigest()}
        outcome, data = parse_response(resp)
        call["outcome"] = outcome
        sec["calls"].append(call)
        if outcome != "ok":
            last = outcome
            continue
        acc, disc, conf = check_findings(data, known_refs, ids)
        sec.update(status="ok", reason="", accepted=acc, discarded=disc, conflicts=conf,
                   opinion_reported=data.get("overall_opinion"))
        return sec
    sec["reason"] = f"model check failed after {sec['calls_made']} call(s): {last}"
    return sec
