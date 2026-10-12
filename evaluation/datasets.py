"""Read-only dataset adapters. CSDS requires only the Python standard library."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

CSDS_LABELS = {
    'emotion': ['neutral', 'positive', 'anxiety', 'confusion', 'disappointment', 'anger'],
    'intent': ['product_use', 'logistics', 'order_change', 'price', 'refund', 'product_problem', 'complaint', 'provide_info'],
}
PRODUCT_LABELS = {
    'emotion': ['平静', '疑惑', '焦急', '不满', '愤怒', '缓和', '无法判断'],
    'intent': ['产品咨询', '订单服务', '补发换货', '售后退货', '物流异常', '退款打款', '不良反应', '物流服务', '会员服务', '其他服务', '暂无法判断'],
    'risk': ['不适或就医自述', '投诉升级'],
}
SEEN_CSDS = {'CSDS_4300', 'CSDS_3372', 'CSDS_4959', 'CSDS_6341', 'CSDS_7174', 'CSDS_9428'}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def scrub(text):
    # Partial protection only: names/addresses require human review before egress.
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', text)
    return re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱已隐藏]', text)


def validate_csds(data):
    """Enforce the supplied CSDS contract, including cross-record constraints.

    This is a dedicated adapter validator, not a general JSON Schema engine.
    """
    errors = []
    if not isinstance(data, list) or not data:
        return ['root: expected a nonempty conversation array']
    ids = set()
    for ci, conversation in enumerate(data):
        loc = f'conversation[{ci}]'
        if not isinstance(conversation, dict):
            errors.append(f'{loc}: expected object')
            continue
        if set(conversation) != {'conversation_id', 'messages'}:
            errors.append(f'{loc}: missing or unexpected conversation fields')
        cid = conversation.get('conversation_id')
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f'{loc}: invalid conversation_id')
        elif cid in ids:
            errors.append(f'{loc}: duplicate conversation_id {cid}')
        else:
            ids.add(cid)
        messages = conversation.get('messages')
        if not isinstance(messages, list) or not messages:
            errors.append(f'{loc}: messages must be nonempty array')
            continue
        previous = 0
        for mi, msg in enumerate(messages):
            at = f'{loc}/message[{mi}]'
            if not isinstance(msg, dict):
                errors.append(f'{at}: expected object')
                continue
            if not {'seq', 'role', 'text'} <= msg.keys() or set(msg) - {'seq', 'role', 'text', 'timestamp', 'emotion', 'intent'}:
                errors.append(f'{at}: missing or unexpected message fields')
            seq = msg.get('seq')
            if type(seq) is not int or seq <= previous:
                errors.append(f'{at}: seq must be positive, unique and increasing')
            else:
                previous = seq
            if not isinstance(msg.get('text'), str) or not msg['text'].strip():
                errors.append(f'{at}: text must be nonempty string')
            if msg.get('timestamp') is not None and (not isinstance(msg['timestamp'], str) or not msg['timestamp'].strip()):
                errors.append(f'{at}: invalid timestamp')
            role = msg.get('role')
            if not isinstance(role, str) or role not in {'customer', 'agent', 'system'}:
                errors.append(f'{at}: invalid role')
            if role == 'customer':
                for field, labels in CSDS_LABELS.items():
                    if msg.get(field) not in labels:
                        errors.append(f'{at}: invalid {field}')
            elif 'emotion' in msg or 'intent' in msg:
                errors.append(f'{at}: only customers may have labels')
    return errors


def audit_csds(data):
    errors = validate_csds(data)
    report = {'contract_version': 'csds-local-v1', 'errors': errors}
    if errors:
        return report
    messages = [m for c in data for m in c['messages']]
    customers = [m for m in messages if m['role'] == 'customer']
    report.update(conversations=len(data), messages=len(messages), customer_messages=len(customers),
                  over_28_messages=sum(len(c['messages']) > 28 for c in data),
                  max_messages=max(len(c['messages']) for c in data),
                  label_counts={k: dict(Counter(m[k] for m in customers)) for k in CSDS_LABELS},
                  conversation_support={k: {v: sum(any(m.get(k) == v for m in c['messages']) for c in data) for v in labels} for k, labels in CSDS_LABELS.items()},
                  review_queue=[{'conversation_id': c['conversation_id'], 'seq': m['seq'], 'reason': 'courtesy labelled non-neutral'}
                                for c in data for m in c['messages'] if m['role'] == 'customer'
                                and m['text'].strip() in {'谢谢', '好的', '嗯', '对', '辛苦了', '麻烦你了'} and m['emotion'] != 'neutral'],
                  label_provenance='unconfirmed', privacy_review='pending', risk_gold_available=False)
    return report


def csds_samples(data, window=28):
    samples, references = [], []
    for conversation in data:
        cid = conversation['conversation_id']
        prefix = []
        for msg in conversation['messages']:
            # Explicit allowlist: no source labels or future messages can enter input.
            prefix.append({'id': f'{cid}:{msg["seq"]}', 'role': msg['role'], 'text': scrub(msg['text'])})
            if msg['role'] != 'customer':
                continue
            sid = f'csds:{cid}:{msg["seq"]}'
            samples.append({'sample_id': sid, 'conversation_id': cid, 'target_seq': msg['seq'],
                            'input': {'target_message_id': prefix[-1]['id'], 'messages': list(prefix[-window:]), 'facts': []}})
            references.append({'sample_id': sid, 'gold': {k: msg[k] for k in CSDS_LABELS},
                               'review_status': 'unreviewed', 'reviewers': [], 'annotation_version': 'csds-import-v1'})
    return samples, references


def official_samples(path, window=28):
    """Reuse product replay visibility in an exclusively temporary database.

    Imports happen only for this command; neither app.main nor model clients run.
    """
    from tempfile import TemporaryDirectory
    from sqlalchemy import select
    from app.copilot.store import Store, Source
    from app.copilot.importer import import_workbook
    from app.copilot.context import get_context

    samples, references, conversations, links = [], [], [], []
    with TemporaryDirectory(prefix='zhiyi-eval-') as folder:
        store = Store(Path(folder) / 'evaluation.sqlite3')
        try:
            import_workbook(store, Path(path))
            with store.transaction() as db:
                rows = list(db.scalars(select(Source).where(Source.sheet == '聊天记录')))
                by_id = {}
                orders = {}
                for row in rows:
                    by_id.setdefault(row.session_id, []).append(row)
                    order = row.payload.get('关联订单号')
                    if order:
                        orders.setdefault(order, set()).add(row.session_id)
                links = [sorted(ids) for ids in orders.values() if len(ids) > 1]
                for cid in sorted(by_id):
                    chat = sorted(by_id[cid], key=lambda r: (r.payload['发送时间'], int(r.payload['消息序号'])))
                    conversations.append({'conversation_id': cid, 'messages': [
                        {'role': r.payload['角色'], 'text': r.payload.get('message_text') or ''} for r in chat]})
                    for cutoff, row in enumerate(chat, 1):
                        if row.payload['角色'] != '买家':
                            continue
                        ctx = get_context(store, cid, mode='replay', cutoff=cutoff)
                        sid = f'official:{cid}:{cutoff}'
                        # Strip provenance, nicknames, scene tags and source file paths.
                        messages = [{'id': m['id'], 'role': {'买家': 'customer', '客服': 'agent', '系统': 'system'}[m['role']], 'text': m['text']}
                                    for m in ctx['messages'][-window:]]
                        samples.append({'sample_id': sid, 'conversation_id': cid, 'target_seq': cutoff,
                                        'input': {'target_message_id': messages[-1]['id'], 'messages': messages, 'facts': ctx['facts']}})
                        references.append({'sample_id': sid, 'gold': {'emotion': None, 'intent': None, 'risk': None},
                                           'evidence_ids': [], 'review_status': 'unreviewed', 'reviewers': [], 'annotation_version': 'official-pending-v1'})
        finally:
            store.engine.dispose()
    return samples, references, conversations, links
