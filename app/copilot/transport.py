"""Service transport diagnostics, retaining the existing provider/model configuration."""
import json
from time import perf_counter
from urllib.parse import urlsplit

import httpx

from app.services.ai import AiClient


class OutputBudgetExceeded(ValueError):
    pass


class ServiceAiClient(AiClient):
    def __init__(self, settings):
        super().__init__(settings)
        self.metrics = {}

    async def _openai_stream(self, messages):
        start = perf_counter()
        self.metrics = {"events": 0, "content_chars": 0, "reasoning_chars": 0, "usage": None}
        payload = {"model": self.settings.openai_model,
                   "messages": [m.model_dump() for m in messages],
                   "temperature": self.settings.ai_temperature,
                   "max_tokens": self.settings.ai_max_tokens, "stream": True}
        # Qwen's hybrid thinking defaults make this small, bounded classification
        # task spend tens of seconds reasoning. Limit this extension to the verified
        # Aliyun model; keep every existing .env value and other providers intact.
        # https://help.aliyun.com/zh/model-studio/deep-thinking/
        host = urlsplit(self.settings.openai_base_url).hostname or ''
        if host.endswith('.aliyuncs.com') and self.settings.openai_model == 'qwen3.8-flash':
            payload['enable_thinking'] = False
            self.metrics['thinking_enabled'] = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(55, connect=15)) as client:
            async with client.stream("POST", self.settings.openai_base_url.rstrip('/') + '/chat/completions',
                    headers={"Authorization": "Bearer " + self.settings.openai_api_key}, json=payload) as response:
                self.metrics.update(http_status=response.status_code, headers_ms=round((perf_counter()-start)*1000))
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == '[DONE]':
                        if raw == '[DONE]':
                            break
                        continue
                    data = json.loads(raw)
                    self.metrics['events'] += 1
                    if data.get('error'):
                        raise ValueError('Provider stream error')
                    if data.get('usage'):
                        self.metrics['usage'] = data['usage']
                    for choice in data.get('choices') or []:
                        delta = choice.get('delta') or {}
                        token = delta.get('content') or ''
                        # Count only; never persist or expose chain-of-thought text.
                        self.metrics['reasoning_chars'] += len(delta.get('reasoning_content') or '')
                        if token:
                            self.metrics.setdefault('first_content_ms', round((perf_counter()-start)*1000))
                            self.metrics['content_chars'] += len(token)
                            yield token
                        if choice.get('finish_reason') == 'length':
                            raise OutputBudgetExceeded('Configured output budget exhausted')
