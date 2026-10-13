"""Portable annotations with dataset identity and explicit conflict resolution."""
from copy import deepcopy
import hashlib
import json

from evaluation.workflow import atomic_json, valid_gold, now


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def export_package(reviews, manifest):
    return {'format': 'zhiyi-annotations-v1', 'dataset_id': manifest['dataset_id'],
            'source': manifest['source'], 'exported_at': now(), 'records': reviews.read(manifest['dataset_id'])}


def plan_merge(reviews, manifest, samples, package):
    if not isinstance(package, dict) or package.get('format') != 'zhiyi-annotations-v1' or package.get('dataset_id') != manifest['dataset_id']:
        raise ValueError('标注包与本地数据版本不一致；先确保双方使用相同数据包')
    records = package.get('records')
    if not isinstance(records, dict):
        raise ValueError('标注记录格式无效')
    by_id = {s['sample_id']: s for s in samples}
    local = reviews.read(manifest['dataset_id'])
    actions, conflicts = {}, []
    for sid, record in records.items():
        if sid not in by_id or not isinstance(record, dict):
            raise ValueError('存在未知样本')
        r = record.get('resolved', {})
        if not isinstance(r, dict) or not valid_gold(r.get('gold'), manifest['labels']):
            raise ValueError('标注标签无效')
        people = r.get('reviewers')
        if r.get('review_status') not in {'confirmed', 'adjudicated', 'reviewing', 'conflict'} or not isinstance(people, list) or not people or not all(isinstance(x, str) and x.strip() for x in people):
            raise ValueError('标注状态或作者无效')
        visible = {m['id'] for m in by_id[sid]['input']['messages']}
        evidence = r.get('evidence_ids')
        if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) and e in visible for e in evidence):
            raise ValueError('标注依据不属于当前样本')
        if type(record.get('revision')) is not int or record['revision'] < 1 or not isinstance(record.get('history'), list) or not record['history'] or not all(isinstance(h, dict) for h in record['history']):
            raise ValueError('标注历史无效')
        if not r.get('annotation_version'):
            raise ValueError('缺少标注版本')
        old = local.get(sid)
        if old is None:
            actions[sid] = 'add'
        elif old['resolved'] == record['resolved'] and old['history'] == record['history']:
            actions[sid] = 'skip'
        elif old['history'] == record['history'][:len(old['history'])] and len(old['history']) < len(record['history']):
            actions[sid] = 'update'
        elif record['history'] == old['history'][:len(record['history'])] and len(record['history']) < len(old['history']):
            actions[sid] = 'skip'
        else:
            actions[sid] = 'conflict'
            conflicts.append({'sample_id': sid, 'local': old['resolved'], 'incoming': r})
    return {'token': digest({'local': local, 'incoming': package}), 'actions': actions,
            'conflicts': conflicts, 'counts': {name: list(actions.values()).count(name) for name in ['add', 'update', 'skip', 'conflict']}}


def apply_merge(reviews, manifest, samples, package, token, choices):
    plan = plan_merge(reviews, manifest, samples, package)
    if token != plan['token']:
        raise ValueError('本地标注已变化，请重新预览合并')
    if not isinstance(choices, dict) or any(choices.get(c['sample_id']) not in {'local', 'incoming'} for c in plan['conflicts']):
        raise ValueError('请逐条选择冲突保留本地或采用导入版本')
    local = reviews.read(manifest['dataset_id'])
    changed = 0
    for sid, action in plan['actions'].items():
        if action in {'add', 'update'} or (action == 'conflict' and choices[sid] == 'incoming'):
            record = deepcopy(package['records'][sid])
            if sid in local:
                # Retain the displaced record, even when choosing the remote branch.
                record.setdefault('merge_history', []).append({'at': now(), 'previous_local': local[sid]})
                record['revision'] = max(local[sid]['revision'], record['revision']) + 1
            local[sid] = record
            changed += 1
    if changed:
        atomic_json(reviews.path(manifest['dataset_id']), local)
    return {'changed': changed, 'kept': len(plan['actions']) - changed}
