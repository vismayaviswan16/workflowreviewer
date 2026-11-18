"""Scripted offline client for reproducible tests. Its output says NOTHING about real-model quality.

Script entries (consumed one per call; the last entry repeats once the script is exhausted):
  {"kind": "valid",   "body": {...}}                    -> JSON-encoded body, finish_reason "stop"
  {"kind": "raw",     "text": str, "finish_reason": s}  -> verbatim text (truncated / malformed JSON)
  {"kind": "timeout"}                                   -> raises ModelTimeout
"""
import json

from .client import ModelResponse, ModelTimeout


class FakeClient:
    name = "fake"

    def __init__(self, script, model="fake-scripted-1"):
        if not script:
            raise ValueError("script must have at least one entry")
        self.script, self.model, self._i = list(script), model, 0

    def complete(self, req):
        e = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        k = e["kind"]
        if k == "timeout":
            raise ModelTimeout()
        if k == "raw":
            text, fin = e["text"], e.get("finish_reason", "stop")
        elif k == "valid":
            text, fin = json.dumps(e["body"]), "stop"
        else:
            raise ValueError(f"unknown script kind {k!r}")
        return ModelResponse(text, fin, req.request_id, self.model, 0.0,
                             {"prompt_tokens": len(req.prompt) // 4, "completion_tokens": len(text) // 4})
