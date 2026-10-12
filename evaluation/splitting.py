"""Deterministic conversation-level grouping and frozen membership."""
from difflib import SequenceMatcher
import hashlib
import random
import re


def grouped_split(conversations, seed=20261012, forced_dev=(), links=(), threshold=0.92):
    ids = sorted(c['conversation_id'] for c in conversations)
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError('conversation IDs must be nonempty and unique')
    parent = {cid: cid for cid in ids}

    def find(cid):
        if parent[cid] != cid:
            parent[cid] = find(parent[cid])
        return parent[cid]

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for group in links:
        if not group or any(cid not in parent for cid in group):
            raise ValueError('group links contain unknown conversation IDs')
        for cid in group[1:]:
            union(group[0], cid)
    texts = {c['conversation_id']: '\n'.join(m['role'] + ':' + re.sub(r'\s+', '', m['text']) for m in c['messages']) for c in conversations}
    pairs = []
    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            ta, tb = texts[a], texts[b]
            similarity = 1.0 if ta == tb else 0.0
            if similarity < 1 and min(len(ta), len(tb)) >= 80:
                matcher = SequenceMatcher(None, ta, tb, autojunk=False)
                if matcher.real_quick_ratio() >= threshold and matcher.quick_ratio() >= threshold:
                    similarity = matcher.ratio()
            if similarity >= threshold:
                union(a, b)
                pairs.append({'a': a, 'b': b, 'similarity': round(similarity, 4)})
    groups = {}
    for cid in ids:
        groups.setdefault(find(cid), []).append(cid)
    forced = set(forced_dev)
    if forced - set(ids):
        raise ValueError('forced development IDs not found')
    assignments = {}
    counts = dict.fromkeys(['dev', 'validation', 'test'], 0)
    targets = {'dev': len(ids) * .6, 'validation': len(ids) * .2, 'test': len(ids) * .2}
    pending = list(groups.values())
    random.Random(seed).shuffle(pending)
    pending.sort(key=lambda group: (not bool(forced.intersection(group)), -len(group)))
    for group in pending:
        split = 'dev' if forced.intersection(group) else max(targets, key=lambda name: targets[name] - counts[name])
        gid = hashlib.sha256('\n'.join(group).encode()).hexdigest()[:16]
        for cid in group:
            assignments[cid] = {'group_id': gid, 'split': split}
        counts[split] += len(group)
    # Improve feasible class coverage without splitting groups or changing sizes.
    # This uses label presence only, before any model evaluation, not model scores.
    by_id = {c['conversation_id']: c for c in conversations}
    group_labels = {group[0]: {f'{field}:{msg[field]}' for cid in group for msg in by_id[cid]['messages']
                              for field in ['emotion', 'intent'] if isinstance(msg.get(field), str)} for group in pending}
    feasible = {label for values in group_labels.values() for label in values
                if sum(label in value for value in group_labels.values()) >= 3}
    for _ in range(len(feasible) * 3):
        coverage = {name: {label: sum(assignments[group[0]]['split'] == name and label in group_labels[group[0]] for group in pending)
                           for label in feasible} for name in counts}
        best = None
        best_gain = 0
        for index, a in enumerate(pending):
            if forced.intersection(a):
                continue
            sa = assignments[a[0]]['split']
            for b in pending[index + 1:]:
                sb = assignments[b[0]]['split']
                if sa == sb or len(a) != len(b) or forced.intersection(b):
                    continue
                gain = 0
                for label in feasible:
                    delta = int(label in group_labels[b[0]]) - int(label in group_labels[a[0]])
                    gain += int(coverage[sa][label] == 0) + int(coverage[sb][label] == 0)
                    gain -= int(coverage[sa][label] + delta == 0) + int(coverage[sb][label] - delta == 0)
                if gain > best_gain:
                    best, best_gain = (a, b, sa, sb), gain
        if best is None:
            break
        a, b, sa, sb = best
        for cid in a:
            assignments[cid]['split'] = sb
        for cid in b:
            assignments[cid]['split'] = sa
    return {'seed': seed, 'ratios': [.6, .2, .2], 'conversation_counts': counts,
            'assignments': assignments, 'near_duplicate_pairs': pairs, 'similarity_threshold': threshold,
            'forced_dev': sorted(forced), 'note': 'Grouped membership is fixed. Feasible label coverage improved by whole-group swaps; rare-class and cross-source template review remains manual.'}
