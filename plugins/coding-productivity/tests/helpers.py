"""Fakes for scoring tests: Messages-API-shaped responses and clients."""

import json
from types import SimpleNamespace as _NS


DEFAULT_RESPONSE_MODEL = "claude-haiku-5-5-20260301"


def make_response(files, model=DEFAULT_RESPONSE_MODEL, stop_reason="end_turn",
                  thinking_first=True, tokens=(100, 50), overall="feature"):
    """A Messages-API-shaped response. 5.x models put a thinking block first."""
    payload = {"files": files, "overall_category": overall, "flags": []}
    content = []
    if thinking_first:
        content.append(_NS(type="thinking", thinking="let me think", signature="sig"))
    content.append(_NS(type="text", text=json.dumps(payload)))
    return _NS(
        content=content, stop_reason=stop_reason, model=model,
        usage=_NS(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


class FakeMessages:
    """``client.messages`` stand-in: replays queued responses (or exceptions)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **params):
        self.calls.append(params)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        # A real API answers with the model that was asked for.
        if getattr(item, "model", None) == DEFAULT_RESPONSE_MODEL:
            item = _NS(**{**vars(item), "model": params["model"] + "-20260301"})
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)
        self.models = _NS(list=lambda limit=100: [])
