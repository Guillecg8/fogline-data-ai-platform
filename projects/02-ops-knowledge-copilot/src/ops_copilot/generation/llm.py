"""Chat client for any OpenAI-compatible endpoint (Ollama, Groq, Gemini, OpenAI...).

The provider is chosen with environment variables, so the code never changes:

    OPS_COPILOT_LLM_BASE_URL   default http://localhost:11434/v1  (Ollama on this machine)
    OPS_COPILOT_LLM_MODEL      default llama3.2:3b
    OPS_COPILOT_LLM_API_KEY    default "ollama" (Ollama ignores it)
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

Message = dict[str, str]


@dataclass(frozen=True)
class LLMReply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


LLM = Callable[[list[Message]], LLMReply]


class ChatClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url or os.environ.get(
            "OPS_COPILOT_LLM_BASE_URL", "http://localhost:11434/v1"
        )
        self.model = model or os.environ.get("OPS_COPILOT_LLM_MODEL", "llama3.2:3b")
        api_key = api_key or os.environ.get("OPS_COPILOT_LLM_API_KEY", "ollama")
        is_local = urlparse(self.base_url).hostname in {"localhost", "127.0.0.1"}
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
            trust_env=not is_local,  # never send local calls through a system proxy
        )

    def __call__(self, messages: list[Message]) -> LLMReply:
        response = self._http.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage") or {}
        return LLMReply(
            text=body["choices"][0]["message"]["content"] or "",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
