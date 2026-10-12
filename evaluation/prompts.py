"""Immutable local prompt versions, including historical run snapshots."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from evaluation.workflow import PROMPT, PROMPT_VERSION, atomic_json, now


class PromptLibrary:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = (self.root / 'prompts/library.json').resolve()
        if self.path.parent != self.root / 'prompts':
            raise ValueError('Prompt 目录无效')

    def custom(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []

    def entries(self):
        entries = [{'id': 'builtin:' + PROMPT_VERSION, 'version': PROMPT_VERSION,
                    'prompt': PROMPT, 'source': 'builtin', 'mode': 'isolated_classifier'}]
        entries += self.custom()
        seen = {(e['version'], e['prompt']) for e in entries}
        for path in sorted((self.root / 'ui-runs').glob('*.json')):
            if path.is_symlink():
                continue
            try:
                run = json.loads(path.read_text(encoding='utf-8'))
                version, prompt = run.get('prompt_version'), run.get('prompt')
                if run.get('mode') != 'isolated_classifier' or not isinstance(version, str) or not isinstance(prompt, str) or not prompt.strip() or (version, prompt) in seen:
                    continue
                identity = hashlib.sha256(json.dumps([version, prompt], ensure_ascii=False).encode()).hexdigest()
                entries.append({'id': 'history:' + identity, 'version': version, 'prompt': prompt,
                                'source': 'history', 'mode': 'isolated_classifier'})
                seen.add((version, prompt))
            except (OSError, ValueError, AttributeError):
                continue
        return [{**e, 'sha256': hashlib.sha256(e['prompt'].encode()).hexdigest()} for e in entries]

    def get(self, identity):
        entry = next((e for e in self.entries() if e['id'] == identity), None)
        if not entry:
            raise ValueError('Prompt 版本不存在，请刷新版本列表')
        return entry

    def save(self, version, prompt):
        if not isinstance(version, str) or not 1 <= len(version.strip()) <= 100:
            raise ValueError('版本名称需要 1–100 个字符')
        if not isinstance(prompt, str) or not 50 <= len(prompt.strip()) <= 8000:
            raise ValueError('Prompt 需要 50–8000 个字符')
        version, prompt = version.strip(), prompt.strip()
        if any(e['version'].casefold() == version.casefold() for e in self.entries()):
            raise ValueError('版本名称已存在，请使用新名称；旧版本不覆盖')
        entry = {'id': 'custom:' + uuid4().hex, 'version': version, 'prompt': prompt,
                 'source': 'custom', 'mode': 'isolated_classifier', 'created_at': now()}
        atomic_json(self.path, [*self.custom(), entry])
        return self.get(entry['id'])
