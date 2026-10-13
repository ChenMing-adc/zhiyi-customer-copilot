"""Independent local evaluation workspace. Never opens the product DB."""
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from evaluation.cli import ROOT, load_bundle, load_jsonl
from evaluation.scoring import evaluate
from evaluation.workflow import Reviews, Runs, PROMPT, PROMPT_VERSION, LABEL_POLICY, POLICY_VERSION, outbound, valid_gold, label_policy
from evaluation.prompts import PromptLibrary


def create_app(data_root=None, client_factory=None):
    root = Path(data_root or ROOT / 'data/evaluation').resolve()
    reviews, runner = Reviews(root), Runs(root, client_factory)
    prompts = PromptLibrary(root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        pending = [t for t in runner.tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title='知意 · 评测实验室', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                return JSONResponse({'detail': '需要 JSON 请求'}, status_code=415)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '不允许跨站导入'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def bundle(key):
        if not key or '/' in key or '\\' in key or key.startswith('.'):
            raise HTTPException(404, '数据包不存在')
        path = (root / key).resolve()
        if path.parent != root:
            raise HTTPException(404, '数据包不存在')
        try:
            manifest, samples = load_bundle(path)
            refs = load_jsonl(path / 'annotations.jsonl')
            if len(refs) != len(samples) or {r['sample_id'] for r in refs} != {s['sample_id'] for s in samples}:
                raise ValueError('annotations mismatch')
            return manifest, samples, reviews.overlay(manifest['dataset_id'], {r['sample_id']: r for r in refs})
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(422, '数据包校验失败，请检查固定文件及标注文件')

    def run_path(run_id):
        if len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
            raise HTTPException(404, '运行记录不存在')
        directory = (root / 'ui-runs').resolve()
        path = (directory / (run_id + '.json')).resolve()
        if directory.parent != root or path.parent != directory:
            raise HTTPException(400, '运行记录目录无效')
        return path

    def get_run(run_id, manifest, split):
        try:
            data = json.loads(run_path(run_id).read_text(encoding='utf-8'))
            if data['dataset_id'] != manifest['dataset_id'] or data['split'] != split:
                raise ValueError('mismatch')
            if data.get('status') == 'running' and run_id not in runner.tasks:
                data['status'] = 'interrupted'
            return data
        except (OSError, ValueError, KeyError):
            raise HTTPException(422, '运行记录与所选数据版本或分组不匹配')

    def summarize(manifest, samples, refs, run):
        result = {'tasks': {}, 'sample_count': len(samples), 'pending_gold': {}, 'errors': [],
                  'received_predictions': len(run['predictions']) if run else 0, 'quality': 'not_run', 'runtime': {}}
        if not run:
            return result
        predictions = {p['sample_id']: p for p in run['predictions']}
        for task, classes in ({} if run.get('mode') == 'reply' else manifest['labels']).items():
            eligible = []
            for sample in samples:
                ref = refs[sample['sample_id']]
                value = ref.get('gold', {}).get(task)
                valid = (isinstance(value, list) and all(isinstance(v, str) for v in value) and len(value) == len(set(value)) and set(value) <= set(classes)) if task == 'risk' else isinstance(value, str) and value in classes
                if valid:
                    eligible.append(ref)
            result['pending_gold'][task] = len(samples) - len(eligible)
            if not eligible:
                continue
            ids = {r['sample_id'] for r in eligible}
            chosen = [p for sid, p in predictions.items() if sid in ids]
            metrics = evaluate(eligible, chosen, {task: classes}, allow_unreviewed=True)
            task_data = metrics['tasks'][task]
            task_data['scored_samples'] = len(eligible)
            result['tasks'][task] = task_data
            result['errors'] += metrics['errors']
        # Measure all returned predictions, including those without gold labels.
        durations = sorted(p['elapsed_ms'] for p in run['predictions'] if type(p.get('elapsed_ms')) in (int, float) and p['elapsed_ms'] >= 0)
        import math
        result['runtime'] = {'p95_ms': durations[math.ceil(len(durations) * .95) - 1] if durations else None,
                             'timed_predictions': len(durations),
                             'tokens': sum(p['total_tokens'] for p in run['predictions'] if type(p.get('total_tokens')) is int and p['total_tokens'] >= 0),
                             'token_measurements': sum(type(p.get('total_tokens')) is int and p['total_tokens'] >= 0 for p in run['predictions'])}
        result['quality'] = 'human_reviewed' if samples and all(
            refs[s['sample_id']].get('review_status') in {'confirmed', 'adjudicated'}
            and len(set(refs[s['sample_id']].get('reviewers', []))) >= 1
            and refs[s['sample_id']].get('annotation_version')
            for s in samples) and all(result['pending_gold'].get(k) == 0 for k in manifest['labels']) else 'exploratory'
        return result

    @app.get('/api/prompts')
    def prompt_versions(mode: str = 'isolated_classifier'):
        defaults = {'analysis': 'builtin:situation-v1', 'reply': 'builtin:reply-v1', 'isolated_classifier': 'builtin:' + PROMPT_VERSION}
        if mode not in defaults:
            raise HTTPException(422, '未知评测节点')
        return {'entries': [e for e in prompts.entries() if e['mode'] == mode], 'default_id': defaults[mode]}

    @app.post('/api/prompts')
    async def save_prompt(request: Request):
        content = await request.body()
        if len(content) > 50000:
            raise HTTPException(413, 'Prompt 文件过大')
        try:
            data = json.loads(content)
            return prompts.save(data.get('version'), data.get('prompt'), data.get('mode', 'isolated_classifier'))
        except (ValueError, AttributeError) as exc:
            raise HTTPException(422, str(exc))

    @app.get('/api/datasets')
    def datasets():
        output, invalid = [], []
        for path in sorted(root.glob('*/manifest.json')):
            try:
                manifest, samples, refs = bundle(path.parent.name)
                output.append({'key': path.parent.name, 'source': manifest['source'], 'dataset_id': manifest['dataset_id'],
                               'input_scope': manifest.get('input_scope', 'replay'), 'count': len(samples), 'conversations': len({s['conversation_id'] for s in samples}),
                               'splits': {split: sum(s['split'] == split for s in samples) for split in ['dev', 'validation', 'test']}})
            except HTTPException:
                invalid.append(path.parent.name)
        return {'datasets': output, 'invalid': invalid}

    @app.get('/api/view/{key}')
    def view(key: str, split: str = 'dev', run_id: str = '', q: str = '', errors_only: bool = False, page: int = 1, conversation: str = ''):
        if split not in {'dev', 'validation', 'test'} or page < 1 or len(q) > 200:
            raise HTTPException(422, '查询参数无效')
        manifest, all_samples, refs = bundle(key)
        current_refs = refs
        selected = [s for s in all_samples if s['split'] == split]
        run = get_run(run_id, manifest, split) if run_id else None
        if run and run.get('sample_ids'):
            selected = [s for s in selected if s['sample_id'] in run['sample_ids']]
        if run and run.get('references'):
            refs = {r['sample_id']: r for r in run['references']}
        metrics = summarize(manifest, selected, refs, run)
        if run and run.get('mode') == 'reply':
            from evaluation.nodes import reply_metrics
            metrics['reply'] = reply_metrics(selected, run['predictions'], read_ratings(run_id))
        runs = []
        for path in sorted((root / 'ui-runs').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                item = get_run(path.stem, manifest, split)
                runs.append({'id': path.stem, 'name': item['run_id'], 'prompt_version': item['prompt_version'], 'mode': item.get('mode', 'imported')})
            except HTTPException:
                pass
        pred = {p['sample_id']: p for p in run['predictions']} if run else {}
        wrong = {e['sample_id'] for e in metrics['errors']}
        if run:
            wrong |= {s['sample_id'] for s in selected if s['sample_id'] not in pred
                      or pred[s['sample_id']].get('status') != 'ok'
                      or (pred[s['sample_id']].get('validator_pass') is False if run.get('mode') == 'reply' else not valid_gold({k: pred[s['sample_id']].get(k) for k in manifest['labels']}, manifest['labels']))}
        rows = []
        for s in selected:
            last = s['input']['messages'][-1]
            if q.casefold() not in (s['sample_id'] + last['text']).casefold() or (errors_only and s['sample_id'] not in wrong):
                continue
            p = pred.get(s['sample_id'])
            rows.append({'id': s['sample_id'], 'conversation': s['conversation_id'], 'text': last['text'],
                         'target_seq': s['target_seq'], 'gold': current_refs[s['sample_id']]['gold'], 'prediction': {k: p.get(k) for k in ['emotion', 'intent', 'risk']} if p else None,
                         'status': '待评测' if not run else '未返回' if not p else '调用失败' if p.get('status') != 'ok' else '需检查' if s['sample_id'] in wrong else '已返回',
                         'review_status': current_refs[s['sample_id']].get('review_status')})
        groups = {}
        for row in rows:
            group = groups.setdefault(row['conversation'], {'id': row['conversation'], 'count': 0, 'reviewed': 0, 'errors': 0, 'preview': row['text']})
            group['count'] += 1
            group['reviewed'] += row['review_status'] in {'confirmed', 'adjudicated'}
            group['errors'] += row['status'] in {'需检查', '调用失败', '未返回'}
        active = conversation if conversation in groups else next(iter(groups), '')
        rows = [r for r in rows if r['conversation'] == active]
        page = min(page, max(1, (len(rows) + 29) // 30))
        return {'dataset': {'key': key, 'source': manifest['source'], 'labels': manifest['labels'], 'dataset_id': manifest['dataset_id']},
                'groups': list(groups.values()), 'conversation': active, 'metrics': metrics, 'runs': runs, 'rows': rows[(page - 1) * 30:page * 30], 'total': len(rows), 'page': page,
                'run': {k: run.get(k) for k in ['run_id', 'prompt_version', 'status', 'mode', 'model', 'started_at', 'retry_limit', 'current_attempt']} if run else None,
                'counts': {'samples': len(selected), 'conversations': len({s['conversation_id'] for s in selected}),
                           'reviewed': sum(current_refs[s['sample_id']].get('review_status') in {'confirmed', 'adjudicated'} and len(set(current_refs[s['sample_id']].get('reviewers', []))) >= 1 for s in selected)}}

    @app.get('/api/sample/{key}')
    def sample(key: str, sample_id: str, split: str = 'dev', run_id: str = ''):
        manifest, samples, refs = bundle(key)
        item = next((s for s in samples if s['sample_id'] == sample_id and s['split'] == split), None)
        if not item:
            raise HTTPException(404, '样本不在当前分组')
        run = get_run(run_id, manifest, split) if run_id else None
        return {'sample': item, 'annotation': refs[sample_id],
                'label_policy': label_policy(manifest)[0], 'policy_version': label_policy(manifest)[1],
                'reply_review': read_ratings(run_id).get(sample_id) if run else None,
                'run_mode': run.get('mode') if run else None,
                'review': reviews.read(manifest['dataset_id']).get(sample_id, {'revision': 0, 'votes': {}, 'history': []}),
                'scored_annotation': next((r for r in run.get('references', []) if r['sample_id'] == sample_id), None) if run else None,
                'prediction': next((p for p in run['predictions'] if p['sample_id'] == sample_id), None) if run else None}

    def read_ratings(rid):
        run_path(rid)
        path = root / 'reply-reviews' / (rid + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    @app.post('/api/reply-review/{key}')
    async def reply_review(key: str, request: Request):
        from evaluation.workflow import atomic_json, now
        data = await request.json()
        manifest, _, _ = bundle(key)
        rid = data.get('run_id', '')
        run = get_run(rid, manifest, data.get('split', 'dev'))
        sid = data.get('sample_id')
        if run.get('mode') != 'reply' or not any(p['sample_id'] == sid and p.get('status') == 'ok' for p in run['predictions']):
            raise HTTPException(422, '仅对已有回复节点输出评分')
        scores = data.get('scores')
        if not isinstance(scores, dict) or set(scores) != {'grounded', 'responsive', 'empathetic', 'actionable'} or not all(type(v) is bool for v in scores.values()):
            raise HTTPException(422, '请完成四项回复质量评分')
        reviewer, note = data.get('reviewer'), data.get('note')
        if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 60 or not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise HTTPException(422, '填写评分人和评分理由')
        ratings = read_ratings(rid)
        old = ratings.get(sid, {})
        if data.get('revision') != old.get('revision', 0):
            raise HTTPException(409, '评分已变化，请刷新后重试')
        ratings[sid] = dict(scores=scores, reviewer=reviewer.strip(), note=note.strip(), at=now(),
                            revision=old.get('revision', 0) + 1, history=[*old.get('history', []), {k: v for k, v in old.items() if k != 'history'}] if old else [])
        atomic_json(root / 'reply-reviews' / (rid + '.json'), ratings)
        return ratings[sid]

    @app.post('/api/review/{key}')
    async def review(key: str, request: Request):
        manifest, samples, refs = bundle(key)
        data = await request.json()
        item = next((s for s in samples if s['sample_id'] == data.get('sample_id')), None)
        if not item:
            raise HTTPException(404, '样本不存在')
        try:
            return reviews.save(manifest, item, data)
        except ValueError as exc:
            raise HTTPException(409 if '冲突' in str(exc) else 422, str(exc))

    @app.get('/api/annotations/export/{key}')
    def export_annotations(key: str):
        from evaluation.sync import export_package
        manifest, _, _ = bundle(key)
        return export_package(reviews, manifest)

    @app.post('/api/annotations/{action}/{key}')
    async def import_annotations(action: str, key: str, request: Request):
        from evaluation.sync import plan_merge, apply_merge
        if action not in {'preview', 'merge'}:
            raise HTTPException(404, '未知操作')
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 20 * 1024 * 1024:
                raise HTTPException(413, '标注包超过 20 MB')
        try:
            data = json.loads(content)
            manifest, samples, _ = bundle(key)
            if action == 'preview':
                return plan_merge(reviews, manifest, samples, data['package'])
            return apply_merge(reviews, manifest, samples, data['package'], data.get('token'), data.get('choices', {}))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(422, str(exc))

    def selected_samples(key, split, conversation='', limit=0, sample_ids=None):
        manifest, samples, refs = bundle(key)
        if manifest['source'] not in {'official_mock_replay', 'csds_local_annotations'}:
            raise HTTPException(403, '不支持此数据来源')
        if split not in {'dev', 'validation', 'test'} or type(limit) is not int or limit < 0:
            raise HTTPException(422, '分组或数量无效')
        selected = [s for s in samples if s['split'] == split and (not conversation or s['conversation_id'] == conversation)]
        if sample_ids is not None:
            allowed = {s['sample_id'] for s in selected}
            if not isinstance(sample_ids, list) or not sample_ids or not all(isinstance(sid, str) and sid in allowed for sid in sample_ids) or len(set(sample_ids)) != len(sample_ids):
                raise HTTPException(422, '请选择当前分组内的不重复样本')
            selected = [s for s in selected if s['sample_id'] in set(sample_ids)]
        if limit:
            selected = selected[:limit]
        if not selected:
            raise HTTPException(422, '所选范围没有样本')
        return manifest, selected, refs

    def execution(data, manifest, samples):
        mode = data.get('mode', 'isolated_classifier')
        if mode not in {'analysis', 'reply', 'isolated_classifier'}:
            raise HTTPException(422, '未知评测节点')
        defaults = {'analysis': 'builtin:situation-v1', 'reply': 'builtin:reply-v1'}
        identity = data.get('prompt_id') or defaults.get(mode)
        prompt, version = data.get('prompt', PROMPT), data.get('prompt_version', PROMPT_VERSION)
        if identity:
            try:
                entry = prompts.get(identity)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            if entry['mode'] != mode:
                raise HTTPException(422, 'Prompt 不属于所选节点')
            prompt, version = entry['prompt'], entry['version']
        if not isinstance(prompt, str) or not 50 <= len(prompt) <= 8000 or not isinstance(version, str) or not 1 <= len(version) <= 100:
            raise HTTPException(422, 'Prompt 长度 50–8000 字，版本名称 1–100 字')
        base = None
        if mode == 'reply':
            rid = data.get('analysis_run_id', '')
            run = get_run(rid, manifest, samples[0]['split'])
            if run.get('mode') != 'analysis' or run.get('status') == 'running':
                raise HTTPException(422, '请选择已结束的分析节点运行记录')
            analyses = {p['sample_id']: p['analysis'] for p in run['predictions'] if p.get('status') == 'ok' and p.get('analysis')}
            if any(s['sample_id'] not in analyses for s in samples):
                raise HTTPException(422, '所选截点必须全部具有该分析记录的有效结果')
            base = {'id': rid, 'prompt_version': run['prompt_version'], 'analyses': {s['sample_id']: analyses[s['sample_id']] for s in samples}}
        return mode, identity, prompt, version, base

    def prepared_inputs(samples, manifest, mode, prompt, base):
        if mode == 'isolated_classifier':
            return [{'sample_id': s['sample_id'], 'input': outbound(s)} for s in samples]
        from evaluation.nodes import node_input
        try:
            return [{'sample_id': s['sample_id'], 'input': [m.model_dump() for m in node_input(s, mode, prompt, manifest['labels'], (base or {}).get('analyses', {}).get(s['sample_id']))[1]]} for s in samples]
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, '节点输入或固定分析结果未通过校验：' + type(exc).__name__)

    def selection_hash(manifest, inputs, settings):
        import hashlib
        return hashlib.sha256(json.dumps({'dataset': manifest['dataset_id'], 'inputs': inputs,
            'destination': settings.openai_base_url, 'model': settings.openai_model},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @app.get('/api/baseline/preview/{key}')
    def preview(key: str, split: str = 'dev', conversation: str = '', limit: int = 0, sample_ids=None,
                mode: str = 'isolated_classifier', prompt_id: str = '', analysis_run_id: str = ''):
        from app.core.config import get_settings
        from urllib.parse import urlsplit
        manifest, samples, refs = selected_samples(key, split, conversation, limit, sample_ids)
        mode, identity, prompt, version, base = execution(dict(mode=mode, prompt_id=prompt_id, analysis_run_id=analysis_run_id), manifest, samples)
        inputs = prepared_inputs(samples, manifest, mode, prompt, base)
        settings = get_settings()
        return {'prompt': prompt, 'prompt_version': version, 'count': len(samples), 'mode': mode,
                'selection_hash': selection_hash(manifest, inputs, settings),
                'host': urlsplit(settings.openai_base_url).hostname, 'model': settings.openai_model,
                'samples': inputs,
                'reviewed': sum(refs[s['sample_id']].get('review_status') in {'confirmed', 'adjudicated'} for s in samples)}

    @app.get('/api/baseline/catalog/{key}')
    def selection_catalog(key: str, split: str = 'dev', analysis_run_id: str = ''):
        manifest, samples, refs = selected_samples(key, split)
        if analysis_run_id:
            run = get_run(analysis_run_id, manifest, split)
            if run.get('mode') != 'analysis' or run.get('status') == 'running':
                raise HTTPException(422, '请选择已结束的分析节点运行记录')
            available = {p['sample_id'] for p in run['predictions'] if p.get('status') == 'ok' and p.get('analysis')}
            samples = [s for s in samples if s['sample_id'] in available]
        groups = {}
        for sample in samples:
            groups.setdefault(sample['conversation_id'], []).append({
                'id': sample['sample_id'], 'seq': sample['target_seq'],
                'text': sample['input']['messages'][-1]['text'][:180],
                'reviewed': refs[sample['sample_id']].get('review_status') in {'confirmed', 'adjudicated'}})
        return {'groups': [{'id': cid, 'samples': rows} for cid, rows in groups.items()]}

    @app.post('/api/baseline/preview/{key}')
    async def selected_preview(key: str, request: Request):
        data = await request.json()
        return preview(key, data.get('split', 'dev'), data.get('conversation', ''), data.get('limit', 0), data.get('sample_ids'), data.get('mode', 'isolated_classifier'), data.get('prompt_id', ''), data.get('analysis_run_id', ''))

    @app.post('/api/baseline/{key}')
    async def baseline(key: str, request: Request):
        from app.core.config import get_settings
        from urllib.parse import urlsplit
        data = await request.json()
        manifest, samples, refs = selected_samples(key, data.get('split', 'dev'), data.get('conversation', ''), data.get('limit', 0), data.get('sample_ids'))
        settings = get_settings()
        if settings.ai_provider != 'openai' or urlsplit(settings.openai_base_url).hostname != 'ws-k0ry66e5zn5pf7wy.cn-beijing.maas.aliyuncs.com' or settings.openai_model != 'qwen3.8-flash':
            raise HTTPException(403, '模型目的地已变化，需要重新确认外发授权')
        mode, identity, prompt, version, base = execution(data, manifest, samples)
        inputs = prepared_inputs(samples, manifest, mode, prompt, base)
        if data.get('selection_hash') != selection_hash(manifest, inputs, settings):
            raise HTTPException(409, '请先预览；样本、Prompt、固定分析结果或模型变化后需重新预览')
        if runner.active():
            raise HTTPException(409, '已有评测正在运行，请等待完成或停止')
        rid = runner.start(manifest, samples, refs, settings, prompt, version, identity, mode, base)
        return {'id': rid, 'count': len(samples)}

    @app.post('/api/stop/{run_id}')
    async def stop(run_id: str):
        run_path(run_id)
        if run_id not in runner.tasks or runner.tasks[run_id].done():
            raise HTTPException(409, '该任务未在当前进程运行')
        runner.stops.add(run_id)
        return {'status': 'stopping', 'note': '当前请求结束后停止，不再发起下一次调用'}

    def resume_plan(key, rid, split):
        import hashlib
        from app.core.config import get_settings
        from urllib.parse import urlsplit
        manifest, samples, _ = bundle(key)
        run = get_run(rid, manifest, split)
        settings = get_settings()
        if run.get('status') not in {'failed', 'cancelled', 'interrupted'} or run.get('mode') not in {'analysis', 'reply', 'isolated_classifier'}:
            raise HTTPException(422, '只有失败、已停止或中断的在线运行可以断点重试')
        if settings.ai_provider != 'openai' or urlsplit(settings.openai_base_url).hostname != 'ws-k0ry66e5zn5pf7wy.cn-beijing.maas.aliyuncs.com':
            raise HTTPException(403, '模型目的地已变化，不能沿用旧运行重试')
        for field, value in [('provider', settings.ai_provider), ('model', settings.openai_model), ('temperature', settings.ai_temperature), ('max_tokens', settings.ai_max_tokens)]:
            if run.get(field) != value:
                raise HTTPException(409, '模型参数已变化，请恢复原配置或新建评测，不能混入原运行')
        if not run.get('prompt') or hashlib.sha256(run['prompt'].encode()).hexdigest() != run.get('prompt_sha256'):
            raise HTTPException(422, '原运行 Prompt 快照缺失或校验失败')
        ids = run.get('sample_ids', [])
        selected = [s for s in samples if s['sample_id'] in ids and s['split'] == split]
        if not ids or len(selected) != len(ids):
            raise HTTPException(422, '原运行的样本范围不完整')
        success = {p['sample_id'] for p in run['predictions'] if p.get('status') == 'ok'}
        pending = [s for s in selected if s['sample_id'] not in success]
        if not pending:
            raise HTTPException(422, '所有样本已成功，无需重试')
        prepared_inputs(pending, manifest, run['mode'], run['prompt'], run.get('analysis_run'))
        token = hashlib.sha256(json.dumps(run, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        failures = [p for p in run['predictions'] if p.get('status') != 'ok']
        info = {'token': token, 'reused': len(success), 'pending': len(pending), 'total': len(selected),
                'prompt_version': run['prompt_version'], 'model': run['model'],
                'last_error': failures[-1].get('error', '') if failures else '',
                'failed_sample': failures[-1]['sample_id'] if failures else ''}
        return run, selected, settings, info

    @app.get('/api/resume/{key}')
    def preview_resume(key: str, run_id: str, split: str = 'dev'):
        return resume_plan(key, run_id, split)[3]

    @app.post('/api/resume/{key}')
    async def resume(key: str, request: Request):
        data = await request.json()
        if runner.active():
            raise HTTPException(409, '已有评测正在运行，请等待或停止')
        original_id = data.get('run_id', '')
        run, samples, settings, info = resume_plan(key, original_id, data.get('split', 'dev'))
        if data.get('token') != info['token']:
            raise HTTPException(409, '运行记录已变化，请重新预览断点重试')
        rid = runner.resume(original_id, run, samples, settings)
        if run['mode'] == 'reply':
            from evaluation.workflow import atomic_json
            success = {p['sample_id'] for p in run['predictions'] if p.get('status') == 'ok'}
            ratings = {sid: r for sid, r in read_ratings(original_id).items() if sid in success}
            if ratings:
                atomic_json(root / 'reply-reviews' / (rid + '.json'), ratings)
        return {'id': rid, **info}

    @app.post('/api/rescore/{key}')
    async def rescore(key: str, request: Request):
        manifest, samples, refs = bundle(key)
        data = await request.json()
        run = get_run(data.get('run_id', ''), manifest, data.get('split', 'dev'))
        if run.get('status') == 'running':
            raise HTTPException(409, '请等待运行结束再重新计分')
        if run.get('mode') == 'reply':
            raise HTTPException(422, '回复质量评分保存后自动更新，无需重算分类标签')
        ids = run.get('sample_ids') or [s['sample_id'] for s in samples if s['split'] == run['split']]
        run.update(references=[refs[sid] for sid in ids], sample_ids=ids,
                   run_id=run['run_id'] + ' · 复核后重算', rescored_from=data['run_id'])
        rid = uuid4().hex
        from evaluation.workflow import atomic_json
        atomic_json(run_path(rid), run)
        return {'id': rid}

    @app.post('/api/import/{key}')
    async def import_run(key: str, request: Request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(413, '预测文件超过 10 MB')
        try:
            data = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
            json.dumps(data, allow_nan=False)
            manifest, samples, refs = bundle(key)
            if not isinstance(data, dict) or data.get('dataset_id') != manifest['dataset_id'] or data.get('split') not in {'dev', 'validation', 'test'}:
                raise ValueError('数据版本或分组不匹配')
            if not all(isinstance(data.get(k), str) and 0 < len(data[k]) <= 200 for k in ['run_id', 'prompt_version']):
                raise ValueError('需要 run_id 和 prompt_version')
            predictions = data.get('predictions')
            if not isinstance(predictions, list) or not predictions:
                raise ValueError('predictions 必须为非空数组')
            allowed = {s['sample_id'] for s in samples if s['split'] == data['split']}
            seen = set()
            for p in predictions:
                if not isinstance(p, dict) or not isinstance(p.get('sample_id'), str) or p['sample_id'] not in allowed or p['sample_id'] in seen:
                    raise ValueError('预测样本重复或不属于该分组')
                seen.add(p['sample_id'])
            run_id = uuid4().hex
            path = run_path(run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as f:
                json.dump({k: data[k] for k in ['dataset_id', 'split', 'run_id', 'prompt_version', 'predictions']}, f, ensure_ascii=False)
            return {'id': run_id, 'split': data['split'], 'count': len(predictions)}
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc))

    @app.get('/api/demo')
    def demo():
        labels = {'emotion': ['neutral', 'anger', 'anxiety'], 'intent': ['refund', 'logistics']}
        texts = ['退款进度可以帮我查一下吗？', '问了好几次都没有答复，太气人了！', '包裹还没有送到，有点担心。', '好的，请帮我查一下物流。', '这个处理结果让我很生气。']
        emotions = ['neutral', 'anger', 'anxiety', 'neutral', 'anger']
        details, rows, refs = [], [], {}
        for i, text in enumerate(texts):
            sid = f'DEMO:{i + 1}'
            sample = {'sample_id': sid, 'conversation_id': f'示例 {i + 1}', 'target_seq': 1,
                      'input': {'messages': [{'id': sid, 'role': 'customer', 'text': text}], 'facts': []}}
            ref = {'sample_id': sid, 'gold': {'emotion': emotions[i], 'intent': 'refund' if i < 2 else 'logistics'}, 'review_status': 'unreviewed'}
            refs[sid] = ref
            details.append({'sample': sample, 'annotation': ref, 'prediction': None})
        preds = [dict(sample_id='DEMO:1', status='ok', emotion='neutral', intent='refund', elapsed_ms=1400, total_tokens=310, raw_output='{"emotion":"neutral","intent":"refund"}', draft='人工构造的展示文本：我会先核实退款申请的处理进度，再与您确认。'),
                 dict(sample_id='DEMO:2', status='ok', emotion='neutral', intent='refund', elapsed_ms=1700, total_tokens=320, raw_output='{"emotion":"neutral","intent":"refund"}'),
                 dict(sample_id='DEMO:3', status='error', error='人工构造的超时示例'),
                 dict(sample_id='DEMO:4', status='ok', emotion='neutral', intent='logistics', elapsed_ms=1300, total_tokens=290)]
        for detail in details:
            sid = detail['sample']['sample_id']
            p = next((p for p in preds if p['sample_id'] == sid), None)
            detail['prediction'] = p
            rows.append({'id': sid, 'conversation': detail['sample']['conversation_id'], 'text': detail['sample']['input']['messages'][0]['text'],
                         'gold': refs[sid]['gold'], 'prediction': p, 'status': '未返回' if not p else '调用失败' if p['status'] != 'ok' else '需检查' if sid == 'DEMO:2' else '已返回'})
        metrics = summarize({'labels': labels}, [d['sample'] for d in details], refs, {'predictions': preds})
        metrics['quality'] = 'synthetic'
        return {'dataset': {'key': 'synthetic', 'labels': labels}, 'metrics': metrics, 'rows': rows, 'details': details,
                'total': 5, 'page': 1, 'runs': [], 'run': {'run_id': '人工构造评分示例', 'prompt_version': '非真实模型'},
                'counts': {'samples': 5, 'conversations': 5, 'reviewed': 0}}

    app.mount('/', StaticFiles(directory=Path(__file__).parent / 'static', html=True), name='evaluation-ui')
    return app


app = create_app()
