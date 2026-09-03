"""
Minimal DeepSeek chat client used to drive the ReAct agent.

DeepSeek thinking-mode note: when `thinking` is enabled every assistant message
in the request history MUST carry its `reasoning_content` back, otherwise the
API rejects the call with 400 (reasoning_content must be passed back). This
module handles that transparently by remembering the last assistant
reasoning_content and re-attaching it on the next request.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


def _load_key_from_env_files() -> str:
    """Load DEEPSEEK_API_KEY from environment or a local .env on Desktop."""
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    candidates = [
        os.path.join(os.path.expanduser("~"), "Desktop", ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ):
        # Lazy key loading: init never raises, so the agent can run in fully
        # offline mode (fallback planner) when no key is configured. The key
        # is only required when a real LLM round-trip happens.
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.model = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", DEEPSEEK_ENDPOINT)
        self._last_reasoning: str = ""  # carries reasoning_content across turns
        self._env_key_loaded = False

    def _ensure_key(self) -> None:
        """Load the key from env files on first use (not at construction)."""
        if self.api_key:
            return
        if not self._env_key_loaded:
            self.api_key = _load_key_from_env_files()
            self._env_key_loaded = True
        if not self.api_key:
            raise RuntimeError(
                "DeepSeek API key not found. Set DEEPSEEK_API_KEY env var or "
                "put it in ~/Desktop/.env"
            )

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ensure every assistant message carries reasoning_content when thinking
        mode produced one - required by the DeepSeek API."""
        cleaned: list[dict[str, Any]] = []
        for message in messages:
            message = dict(message)
            role = message.get("role")
            if role == "assistant":
                # pass back the reasoning of the *previous* assistant turn; the
                # model may omit it in pure-text rounds -> empty string is valid
                message.setdefault("reasoning_content", self._last_reasoning)
                # pop any tool-call only roles handled by caller
            cleaned.append(message)
        return cleaned

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """One round-trip; returns {content, reasoning_content, tool_calls}."""
        self._ensure_key()  # key required only when an actual call happens
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        # thinking mode (the interview SDK enables it; here we keep it optional
        # via env flag so plain mode also works)
        if os.getenv("DEEPSEEK_THINKING", "").strip().lower() in ("1", "true", "yes"):
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = "medium"
        else:
            payload["temperature"] = temperature

        response = httpx.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120.0,
        )
        if response.status_code != 200:
            detail = response.text[:500]
            raise RuntimeError(f"DeepSeek API error {response.status_code}: {detail}")

        data = response.json()
        message = data["choices"][0]["message"]
        reasoning = message.get("reasoning_content", "")
        if reasoning:
            self._last_reasoning = reasoning
        result: dict[str, Any] = {
            "content": message.get("content") or "",
            "reasoning_content": reasoning,
        }
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        return result
