"""Product node contracts without product persistence or product settings writes."""
import asyncio
import json
from time import perf_counter

from app.copilot.analysis import (Situation, analysis_prompt, reply_prompt, situation_messages,
                                  decode_situation, reply_messages, validate_situation,
                                  validate_draft, rule_signals, INTENTS, EMOTIONS)

MODES = {'analysis': 'SituationAnalyst', 'reply': 'ReplyPlanner'}


def builtin_prompts():
    template = analysis_prompt().replace(str(sorted(INTENTS)), '{{intent_labels}}').replace(str(sorted(EMOTIONS)), '{{emotion_labels}}')
    return [dict(id='builtin:situation-v1', version='situation-v1', mode='analysis', source='builtin', prompt=template),
            dict(id='builtin:reply-v1', version='reply-v1', mode='reply', source='builtin', prompt=reply_prompt())]


def context(sample):
    from evaluation.workflow import outbound
    data = outbound(sample)
    messages = [{**m, 'role': {'customer': '买家', 'agent': '客服', 'system': '系统'}.get(m['role'], m['role'])} for m in data['messages']]
    evidence = {m['id']: {} for m in messages}
    facts = []
    for i, fact in enumerate(data['facts']):
        fields = {}
        for j, (name, value) in enumerate(fact['fields'].items()):
            eid = f'fact:{i}:{j}'
            evidence[eid] = {}
            fields[name] = {'value': value, 'evidence_id': eid}
        facts.append({**fact, 'fields': fields})
    return {'messages': messages, 'facts': facts, 'evidence': evidence, 'mode': 'replay'}


def node_input(sample, mode, prompt, labels, analysis=None):
    ctx = context(sample)
    if mode == 'analysis':
        messages, aliases = situation_messages(ctx, prompt, labels)
    elif mode == 'reply':
        situation = Situation.model_validate(analysis)
        validate_situation(situation, ctx, labels)
        messages, aliases = reply_messages(ctx, situation, prompt), {}
    else:
        raise ValueError('未知节点')
    return ctx, messages, aliases


async def predict_node(sample, labels, prompt, client, mode, analysis=None):
    start, chunks = perf_counter(), []
    result = {'sample_id': sample['sample_id'], 'node': MODES[mode], 'status': 'error'}
    try:
        ctx, messages, aliases = node_input(sample, mode, prompt, labels, analysis)
        result['model_input'] = [m.model_dump() for m in messages]
        async with asyncio.timeout(30):
            async for token in client.stream(messages):
                chunks.append(token)
        raw = ''.join(chunks)
        if mode == 'analysis':
            situation = decode_situation(raw, aliases, ctx, labels)
            result.update(situation.model_dump(), analysis=situation.model_dump(), status='ok')
            result['risk'] = [r['type'] for r in rule_signals(ctx)[0]]
            result['risk_source'] = 'product_keyword_rules_not_prompt'
        else:
            errors = validate_draft(raw, ctx)
            result.update(status='ok', draft=raw.strip(), analysis_input=analysis,
                          validation_errors=errors, validator_pass=not errors,
                          length_pass=80 <= len(raw.strip()) <= 180)
    except Exception as exc:
        result['error'] = '节点调用或结构/引用校验失败：' + type(exc).__name__
        if type(exc) is ValueError and str(exc) in {'情绪缺少买家证据', '情绪引用了非买家消息', '摘要引用不存在或在当前截点不可见', '标签不在客服枚举中', '模型没有返回 JSON'}:
            result['error'] += '（' + str(exc) + '）'
    result['raw_output'] = ''.join(chunks)
    result['elapsed_ms'] = round((perf_counter() - start) * 1000)
    usage = getattr(client, 'metrics', {}).get('usage') or {}
    if type(usage.get('total_tokens')) is int:
        result['total_tokens'] = usage['total_tokens']
    return result


def reply_metrics(samples, predictions, ratings):
    by_id = {p['sample_id']: p for p in predictions}
    rows = [by_id.get(s['sample_id'], {}) for s in samples]
    n = len(rows)
    graded = [ratings[s['sample_id']] for s in samples if s['sample_id'] in ratings]
    return {'samples': n, 'validator_pass_rate': sum(p.get('validator_pass') is True for p in rows) / n if n else None,
            'length_pass_rate': sum(p.get('length_pass') is True for p in rows) / n if n else None,
            'failures': sum(p.get('status') != 'ok' for p in rows), 'reviewed': len(graded),
            'human_pass_rate': sum(all(r['scores'].values()) for r in graded) / len(graded) if graded else None,
            'rubric_pass_rates': {key: sum(r['scores'][key] for r in graded) / len(graded) if graded else None
                                  for key in ('grounded', 'responsive', 'empathetic', 'actionable')}}
