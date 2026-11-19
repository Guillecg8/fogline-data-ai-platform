"""The model interface (ADR 0007). Tests use `ScriptedModel`; only batches use `OllamaModel`."""

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"


@dataclass(frozen=True)
class ToolCall:
    name: object
    arguments: object


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...]
    prompt_eval_count: int = 0
    prompt_eval_cached_count: int = 0
    eval_count: int = 0
    done_reason: str = "stop"
    durations_ms: dict = field(default_factory=dict)
    raw_message: dict = field(default_factory=dict)  # appended to the history as returned


@dataclass(frozen=True)
class Options:
    temperature: float = 0.0
    seed: int = 7
    num_ctx: int = 8192
    num_predict: int = 512
    keep_alive: str = "30m"

    def as_ollama(self) -> dict:
        return {
            "temperature": self.temperature,
            "seed": self.seed,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }


class Model(Protocol):
    name: str

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


def ollama_host() -> str:
    """`OLLAMA_HOST` may be a bare `host:port` or a URL; the default is the local WSL server."""
    value = os.environ.get("OLLAMA_HOST", "").strip() or DEFAULT_HOST
    return value if "://" in value else f"http://{value}"


def _message_from(reply: dict) -> ModelReply:
    message = reply.get("message") or {}
    calls = tuple(
        ToolCall(call.get("function", {}).get("name"), call.get("function", {}).get("arguments"))
        for call in message.get("tool_calls") or []
    )
    durations = {
        key.removesuffix("_duration"): reply.get(key, 0) // 1_000_000
        for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
    }
    return ModelReply(
        content=message.get("content") or "",
        tool_calls=calls,
        prompt_eval_count=reply.get("prompt_eval_count", 0),
        prompt_eval_cached_count=reply.get("prompt_eval_cached_count", 0),
        eval_count=reply.get("eval_count", 0),
        done_reason=reply.get("done_reason", ""),
        durations_ms=durations,
        raw_message={k: message[k] for k in ("role", "content", "tool_calls") if k in message},
    )


class OllamaModel:
    """Native `POST /api/chat`, not the OpenAI-compatible endpoint (ADR 0007)."""

    def __init__(
        self,
        options: Options,
        model: str = DEFAULT_MODEL,
        host: str | None = None,
        timeout_s: float = 900.0,
    ) -> None:
        self.name = model
        self.options = options
        self.host = host or ollama_host()
        self.timeout_s = timeout_s
        # Local calls never go through a system proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path: str, body: dict | None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.host + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        with self._opener.open(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        body = {
            "model": self.name,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "keep_alive": self.options.keep_alive,
            "options": self.options.as_ollama(),
        }
        return _message_from(self._post("/api/chat", body))

    def identity(self) -> dict:
        """Model digest and Ollama version, for the record and the freeze."""
        version = self._post("/api/version", None).get("version")
        tags = self._post("/api/tags", None).get("models", [])
        digest = next((m.get("digest") for m in tags if m.get("name") == self.name), None)
        return {"model": self.name, "digest": digest, "ollama_version": version}


class ScriptedModel:
    """Returns fixed replies in order; records what it was sent. For tests only."""

    def __init__(self, replies: list[ModelReply], name: str = "scripted") -> None:
        self.name = name
        self._replies = list(replies)
        self.requests: list[tuple[list[dict], list[dict]]] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.requests.append(([dict(m) for m in messages], tools))
        if not self._replies:
            return ModelReply("done", ())
        reply = self._replies.pop(0)
        if not reply.raw_message:
            calls = [
                {"function": {"name": c.name, "arguments": c.arguments}} for c in reply.tool_calls
            ]
            raw = {"role": "assistant", "content": reply.content}
            if calls:
                raw["tool_calls"] = calls
            reply = ModelReply(**{**reply.__dict__, "raw_message": raw})
        return reply

    def identity(self) -> dict:
        return {"model": self.name, "digest": None, "ollama_version": None}


def call(name: object, **arguments: object) -> ModelReply:
    """A scripted reply with one tool call."""
    return ModelReply("", (ToolCall(name, arguments),), prompt_eval_count=100, eval_count=10)


def answer(text: str) -> ModelReply:
    """A scripted final answer with no tool call."""
    return ModelReply(text, (), prompt_eval_count=100, eval_count=10)


def now_ms() -> int:
    return time.time_ns() // 1_000_000
