"""Thin wrapper over the OpenAI (Responses API) and Anthropic (Messages API) tool-calling chat.

Choose the model with PIANO2MIDI_MODEL, e.g. "openai:gpt-6-luna" or "anthropic:claude-haiku-5-5".
Without it, OpenAI is used when OPENAI_API_KEY is set, otherwise Anthropic.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

DEFAULTS = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


def model_spec() -> tuple[str, str]:
    spec = os.environ.get("PIANO2MIDI_MODEL", "").strip()
    if spec:
        provider, _, model = spec.partition(":")
        provider = provider.lower()
        if provider not in DEFAULTS:
            raise ValueError(f"PIANO2MIDI_MODEL must start with openai: or anthropic:, got {spec!r}")
        return provider, model or DEFAULTS[provider]
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", DEFAULTS["openai"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", DEFAULTS["anthropic"]
    raise ValueError("set OPENAI_API_KEY or ANTHROPIC_API_KEY (and optionally PIANO2MIDI_MODEL)")


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


class OpenAIChat:
    def __init__(self, model: str, instructions: str, tools: list[dict]):
        from openai import OpenAI

        self.client, self.model, self.instructions = OpenAI(), model, instructions
        self.tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["parameters"]} for t in tools]
        self.previous = self.turn_start = None

    def _call(self, items: list) -> Reply:
        kwargs = {"previous_response_id": self.previous} if self.previous else {}
        resp = self.client.responses.create(model=self.model, instructions=self.instructions,
                                            input=items, tools=self.tools, **kwargs)
        self.previous = resp.id
        calls = [ToolCall(o.call_id, o.name, _parse_args(o.arguments))
                 for o in resp.output if getattr(o, "type", "") == "function_call"]
        return Reply(resp.output_text or "", calls)

    def send_user(self, text: str) -> Reply:
        self.turn_start = self.previous
        return self._call([{"role": "user", "content": text}])

    def send_tool_results(self, results: list[tuple[ToolCall, str]]) -> Reply:
        return self._call([{"type": "function_call_output", "call_id": c.id, "output": out} for c, out in results])

    def discard_turn(self) -> None:
        """Forget the latest request and what followed it; the conversation before it stays."""
        self.previous = self.turn_start

    def reset(self) -> None:
        self.previous = self.turn_start = None


class AnthropicChat:
    def __init__(self, model: str, instructions: str, tools: list[dict]):
        import anthropic

        self.client, self.model, self.instructions = anthropic.Anthropic(), model, instructions
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                      for t in tools]
        self.messages: list[dict] = []
        self.turn_start = 0

    def _call(self) -> Reply:
        resp = self.client.messages.create(model=self.model, max_tokens=2000, system=self.instructions,
                                           messages=self.messages, tools=self.tools)
        self.messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, _parse_args(b.input)) for b in resp.content if b.type == "tool_use"]
        return Reply(text, calls)

    def send_user(self, text: str) -> Reply:
        self.turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        return self._call()

    def send_tool_results(self, results: list[tuple[ToolCall, str]]) -> Reply:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": out} for c, out in results]})
        return self._call()

    def discard_turn(self) -> None:
        """Forget the latest request and what followed it; the conversation before it stays."""
        del self.messages[self.turn_start:]

    def reset(self) -> None:
        self.messages = []
        self.turn_start = 0


def make_chat(instructions: str, tools: list[dict]):
    provider, model = model_spec()
    cls = OpenAIChat if provider == "openai" else AnthropicChat
    return cls(model, instructions, tools), f"{provider}:{model}"


def complete_json(prompt: str) -> dict:
    """One-shot request that should answer with a JSON object."""
    provider, model = model_spec()
    if provider == "openai":
        from openai import OpenAI

        text = OpenAI().responses.create(model=model, input=prompt).output_text
    else:
        import anthropic

        resp = anthropic.Anthropic().messages.create(model=model, max_tokens=4000,
                                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        return {}
