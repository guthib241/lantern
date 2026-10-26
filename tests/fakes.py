"""Shared offline fakes: a scripted Responses API client (never calls the real API)."""
import json
from types import SimpleNamespace as NS


def call(name, cid, **args):
    return NS(type="function_call", name=name, call_id=cid, arguments=json.dumps(args))


def usage(inp=0, cached=0, out=0):
    return NS(input_tokens=inp, input_tokens_details=NS(cached_tokens=cached),
              output_tokens=out, output_tokens_details=NS(reasoning_tokens=0),
              total_tokens=inp + out)


class FakeClient:
    """Returns scripted responses in order and records every request.

    Script items are (output, text) or (output, text, usage)."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.responses = self

    def create(self, **kw):
        self.requests.append(kw)
        item = self.script.pop(0)
        out, text = item[0], item[1]
        u = item[2] if len(item) > 2 else None
        return NS(id=f"resp_{len(self.requests)}", output=out, output_text=text, usage=u)
