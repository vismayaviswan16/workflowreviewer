"""LIVE model client (Anthropic Messages API over HTTPS, standard library only -- no SDK dependency to pin).

NOT used by any offline test or by the reported benchmark numbers: it is opt-in (`python -m reviewer B.json --live-model MODEL`,
requires ANTHROPIC_API_KEY) and was NOT run for this submission (no key / no network in the evaluation environment), so
real-model performance was NOT measured. Its network layer is injectable (`opener`) so the request/response mapping is unit-tested
with a stub. Contract with the reviewer: raise ModelTimeout on timeout; transport/HTTP/JSON failures raise ModelError, a ModelTimeout
subclass, so the advisor counts them as a failed attempt inside its bounded retry loop (a model failure never yields PASS).
The API has no sampling seed: `seed` is recorded in provenance but cannot be enforced (stated limitation).
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from .client import ModelResponse, ModelTimeout

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class ModelError(ModelTimeout):
    """Transport/HTTP failure; handled by the advisor exactly like a timeout (bounded retries, never PASS)."""


class AnthropicClient:
    name = "anthropic-live"

    def __init__(self, model, api_key=None, opener=None, url=API_URL):
        self.model = model
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._key and opener is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; live model unavailable")
        self._open, self._url = opener or urllib.request.urlopen, url

    def complete(self, req):
        body = json.dumps({"model": self.model, "max_tokens": req.max_tokens, "temperature": req.temperature,
                           "messages": [{"role": "user", "content": req.prompt}]}).encode()
        r = urllib.request.Request(self._url, data=body, method="POST",
                                   headers={"content-type": "application/json", "x-api-key": self._key or "",
                                            "anthropic-version": API_VERSION})
        t0 = time.perf_counter()
        try:
            with self._open(r, timeout=req.timeout_s) as h:
                data = json.loads(h.read().decode())
        except (socket.timeout, TimeoutError) as e:
            raise ModelTimeout() from e
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
                raise ModelTimeout() from e
            raise ModelError(str(e)) from e
        except (ValueError, OSError) as e:
            raise ModelError(str(e)) from e
        text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        fin = "length" if data.get("stop_reason") == "max_tokens" else "stop"
        u = data.get("usage") or {}
        return ModelResponse(text, fin, req.request_id, data.get("model", self.model), round(time.perf_counter() - t0, 4),
                             {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)})
