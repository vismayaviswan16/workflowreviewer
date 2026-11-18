"""Model-client interface. Anything with `complete(ModelRequest) -> ModelResponse` can be plugged in.

Attributes read by the reviewer for provenance / cache keys: `name`, `model`, and (fake only) `script`.
A live client must raise ModelTimeout on timeout and must never be required for offline tests.
"""
from dataclasses import dataclass, field


class ModelTimeout(Exception):
    pass


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    model: str
    temperature: float
    max_tokens: int
    seed: int
    timeout_s: float
    request_id: str


@dataclass
class ModelResponse:
    text: str
    finish_reason: str          # "stop" | "length"
    request_id: str
    model: str
    latency_s: float = 0.0
    usage: dict = field(default_factory=dict)   # prompt_tokens, completion_tokens
