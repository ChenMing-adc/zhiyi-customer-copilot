from __future__ import annotations

import json

import httpx

from app.core.config import Settings
from app.schemas.dtos import AiMessage


class AiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, messages: list[AiMessage]) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return self._ollama(messages, stream=False)
        if provider == "openai":
            return self._openai(messages, stream=False)
        raise ValueError("Unsupported model provider: configure openai or ollama")

    async def stream(self, messages: list[AiMessage]):
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            async for token in self._ollama_stream(messages):
                yield token
            return
        if provider == "openai":
            async for token in self._openai_stream(messages):
                yield token
            return
        raise ValueError("Unsupported model provider: configure openai or ollama")

    def _ollama(self, messages: list[AiMessage], stream: bool) -> str:
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        response = httpx.post(f"{self.settings.ollama_base_url}/api/chat", json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["message"]["content"]

    async def _ollama_stream(self, messages: list[AiMessage]):
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": True,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", f"{self.settings.ollama_base_url}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token

    def _openai(self, messages: list[AiMessage], stream: bool) -> str:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": stream,
        }
        response = httpx.post(f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _openai_stream(self, messages: list[AiMessage]):
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if raw == "[DONE]":
                        break
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if data.get("error"):
                        raise RuntimeError("Model API returned an error in the response stream")
                    choices = data.get("choices") or []
                    # Some providers send usage-only chunks with no choices.
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    token = delta.get("content")
                    if token:
                        yield token
