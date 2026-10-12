"""Independent local evaluation viewer. No product DB or model calls."""
import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from evaluation.cli import ROOT, load_bundle, load_jsonl
from evaluation.scoring import evaluate


def create_app(data_root=None):
    root = Path(data_root or ROOT / 'data/evaluation').resolve()
    app = FastAPI(title='知意 · 评测实验室')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
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
            return manifest, samples, {r['sample_id']: r for r in refs}
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
            return data
        except (OSError, ValueError, KeyError):
            raise HTTPException(422, '运行记录与所选数据版本或分组不匹配')

    def summarize(manifest, samples, refs, run):
        result = {'tasks': {}, 'sample_count': len(samples), 'pending_gold': {}, 'errors': [],
                  'received_predictions': len(run['predictions']) if run else 0, 'quality': 'not_run', 'runtime': {}}
        if not run:
            return result
        predictions = {p['sample_id']: p for p in run['predictions']}
        for task, classes in manifest['labels'].items():
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
        result['quality'] = 'exploratory'
        return result

    @app.get('/api/datasets')
    def datasets():
        output, invalid = [], []
        for path in sorted(root.glob('*/manifest.json')):
            try:
                manifest, samples, refs = bundle(path.parent.name)
                output.append({'key': path.parent.name, 'source': manifest['source'], 'dataset_id': manifest['dataset_id'],
                               'count': len(samples), 'conversations': len({s['conversation_id'] for s in samples}),
                               'splits': {split: sum(s['split'] == split for s in samples) for split in ['dev', 'validation', 'test']}})
            except HTTPException:
                invalid.append(path.parent.name)
        return {'datasets': output, 'invalid': invalid}

    @app.get('/api/view/{key}')
    def view(key: str, split: str = 'dev', run_id: str = '', q: str = '', errors_only: bool = False, page: int = 1):
        if split not in {'dev', 'validation', 'test'} or page < 1 or len(q) > 200:
            raise HTTPException(422, '查询参数无效')
        manifest, all_samples, refs = bundle(key)
        selected = [s for s in all_samples if s['split'] == split]
        run = get_run(run_id, manifest, split) if run_id else None
        metrics = summarize(manifest, selected, refs, run)
        runs = []
        for path in sorted((root / 'ui-runs').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                item = get_run(path.stem, manifest, split)
                runs.append({'id': path.stem, 'name': item['run_id'], 'prompt_version': item['prompt_version']})
            except HTTPException:
                pass
        pred = {p['sample_id']: p for p in run['predictions']} if run else {}
        wrong = {e['sample_id'] for e in metrics['errors']}
        rows = []
        for s in selected:
            last = s['input']['messages'][-1]
            if q.casefold() not in (s['sample_id'] + last['text']).casefold() or (errors_only and s['sample_id'] not in wrong):
                continue
            p = pred.get(s['sample_id'])
            rows.append({'id': s['sample_id'], 'conversation': s['conversation_id'], 'text': last['text'],
                         'gold': refs[s['sample_id']]['gold'], 'prediction': {k: p.get(k) for k in ['emotion', 'intent', 'risk']} if p else None,
                         'status': '待评测' if not run else '未返回' if not p else '调用失败' if p.get('status') != 'ok' else '需检查' if s['sample_id'] in wrong else '已返回',
                         'review_status': refs[s['sample_id']].get('review_status')})
        return {'dataset': {'key': key, 'source': manifest['source'], 'labels': manifest['labels'], 'dataset_id': manifest['dataset_id']},
                'metrics': metrics, 'runs': runs, 'rows': rows[(page - 1) * 30:page * 30], 'total': len(rows), 'page': page,
                'run': {'run_id': run['run_id'], 'prompt_version': run['prompt_version']} if run else None,
                'counts': {'samples': len(selected), 'conversations': len({s['conversation_id'] for s in selected}),
                           'reviewed': sum(refs[s['sample_id']].get('review_status') == 'adjudicated' and len(set(refs[s['sample_id']].get('reviewers', []))) >= 2 for s in selected)}}

    @app.get('/api/sample/{key}')
    def sample(key: str, sample_id: str, split: str = 'dev', run_id: str = ''):
        manifest, samples, refs = bundle(key)
        item = next((s for s in samples if s['sample_id'] == sample_id and s['split'] == split), None)
        if not item:
            raise HTTPException(404, '样本不在当前分组')
        run = get_run(run_id, manifest, split) if run_id else None
        return {'sample': item, 'annotation': refs[sample_id],
                'prediction': next((p for p in run['predictions'] if p['sample_id'] == sample_id), None) if run else None}

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
