"""Diagnose configured transport without business data or credential output."""
import asyncio
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.core.config import get_settings


async def main():
    s = get_settings()
    start = perf_counter()
    report = {"provider": s.ai_provider, "max_tokens": s.ai_max_tokens}
    if s.ai_provider != "openai":
        print(json.dumps({**report, "note": "probe supports compatible provider only"}))
        return
    report["host"] = urlsplit(s.openai_base_url).hostname
    report["model"] = s.openai_model
    payload = {"model": s.openai_model, "messages": [{"role": "user", "content": "连通性测试，请只回复 OK。"}],
               "temperature": s.ai_temperature, "max_tokens": s.ai_max_tokens, "stream": True}
    try:
        async with asyncio.timeout(60):
            async with httpx.AsyncClient(timeout=httpx.Timeout(55, connect=15)) as client:
                async with client.stream("POST", s.openai_base_url.rstrip('/') + '/chat/completions',
                    headers={"Authorization": "Bearer " + s.openai_api_key}, json=payload) as response:
                    report.update(http_status=response.status_code, headers_ms=round((perf_counter()-start)*1000))
                    print(json.dumps(report), flush=True)
                    response.raise_for_status()
                    counts = {"events": 0, "content_chars": 0, "reasoning_chars": 0}
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        raw = line[5:].strip()
                        if raw == '[DONE]':
                            break
                        data = json.loads(raw)
                        counts['events'] += 1
                        for choice in data.get('choices') or []:
                            delta = choice.get('delta') or {}
                            counts['content_chars'] += len(delta.get('content') or '')
                            counts['reasoning_chars'] += len(delta.get('reasoning_content') or '')
                        if counts['events'] == 1:
                            print(json.dumps({"first_event_ms":round((perf_counter()-start)*1000)}), flush=True)
                    report.update(counts)
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    report['elapsed_ms'] = round((perf_counter()-start)*1000)
    print(json.dumps(report, ensure_ascii=False), flush=True)

asyncio.run(main())
