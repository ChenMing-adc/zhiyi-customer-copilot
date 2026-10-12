"""Metrics with explicit failure denominators; no sklearn or model dependency."""
from collections import Counter
import math
import statistics


def divide(a, b):
    return a / b if b else None


def label_metrics(tp, fp, fn, support):
    return {'support': support, 'tp': tp, 'fp': fp, 'fn': fn,
            'precision': divide(tp, tp + fp), 'recall': divide(tp, tp + fn),
            'f1': divide(2 * tp, 2 * tp + fp + fn)}


def evaluate(references, predictions, labels, allow_unreviewed=False):
    if not isinstance(references, list) or not isinstance(predictions, list) or not all(isinstance(r, dict) for r in [*references, *predictions]):
        raise ValueError('References and predictions must be arrays of objects')
    if not isinstance(labels, dict) or not labels:
        raise ValueError('Missing label spaces')
    for classes in labels.values():
        if not isinstance(classes, list) or not classes or not all(isinstance(c, str) and c != '__failure__' for c in classes) or len(set(classes)) != len(classes):
            raise ValueError('Invalid label space')
    if not references:
        raise ValueError('No samples in selected split')
    gold_by_id = {}
    for reference in references:
        sid = reference['sample_id']
        if sid in gold_by_id:
            raise ValueError('Duplicate reference sample_id')
        reviewers = reference.get('reviewers', [])
        reviewed = (reference.get('review_status') == 'adjudicated' and isinstance(reviewers, list)
                    and all(isinstance(r, str) and r.strip() for r in reviewers)
                    and len(set(reviewers)) >= 2 and bool(reference.get('annotation_version')))
        if not allow_unreviewed and not reviewed:
            raise ValueError('Unreviewed gold: use adjudicated references, or explicitly select exploratory --allow-unreviewed')
        for task, classes in labels.items():
            value = reference.get('gold', {}).get(task)
            if task == 'risk':
                valid = isinstance(value, list) and all(isinstance(v, str) for v in value) and len(value) == len(set(value)) and set(value) <= set(classes)
            else:
                valid = isinstance(value, str) and value in classes
            if not valid:
                raise ValueError(f'Missing or invalid gold {task} for {sid}; complete annotations before scoring')
        gold_by_id[sid] = reference
    by_id = {}
    for prediction in predictions:
        sid = prediction.get('sample_id')
        if sid not in gold_by_id:
            raise ValueError(f'Prediction outside selected split: {sid}')
        if sid in by_id:
            raise ValueError(f'Duplicate prediction sample_id: {sid}')
        by_id[sid] = prediction
    result = {'sample_count': len(references), 'received_predictions': len(by_id),
              'quality': 'exploratory_unreviewed' if allow_unreviewed else 'human_reviewed',
              'macro_policy': 'fixed label list, undefined class F1 contributes zero; also report supported-class macro',
              'tasks': {}, 'errors': []}
    all_valid = set(gold_by_id)
    for task, classes in labels.items():
        stats = {c: Counter() for c in classes}
        matrix = {c: dict.fromkeys([*classes, '__failure__'], 0) for c in classes}
        valid_count = correct = 0
        for sid, reference in gold_by_id.items():
            gold = reference['gold'][task]
            prediction = by_id.get(sid, {})
            value = prediction.get(task)
            valid = prediction.get('status') == 'ok'
            if task == 'risk':
                valid = valid and isinstance(value, list) and all(isinstance(v, str) for v in value)
                valid = valid and len(value) == len(set(value)) and set(value) <= set(classes)
            else:
                valid = valid and isinstance(value, str) and value in classes
            if valid:
                valid_count += 1
            else:
                all_valid.discard(sid)
            equal = valid and (set(gold) == set(value) if task == 'risk' else gold == value)
            correct += int(equal)
            if not equal:
                result['errors'].append({'sample_id': sid, 'task': task, 'gold': gold,
                                         'prediction': value if valid else None,
                                         'reason': 'wrong_label' if valid else 'missing_or_failed_or_invalid_output'})
            if task != 'risk':
                matrix[gold][value if valid else '__failure__'] += 1
            for c in classes:
                actual = c in gold if task == 'risk' else gold == c
                predicted = valid and (c in value if task == 'risk' else value == c)
                stats[c]['support'] += int(actual)
                stats[c]['tp'] += int(actual and predicted)
                stats[c]['fp'] += int(not actual and predicted)
                stats[c]['fn'] += int(actual and not predicted)
        per_class = {c: label_metrics(**{k: stats[c][k] for k in ['tp', 'fp', 'fn', 'support']}) for c in classes}
        supported = [v for v in per_class.values() if v['support']]
        summary = {'per_class': per_class, 'valid_outputs': valid_count, 'failures': len(references) - valid_count,
                   'coverage': valid_count / len(references), 'accuracy_all_samples': correct / len(references),
                   'accuracy_valid_outputs': divide(correct, valid_count),
                   'macro_f1_fixed': sum(v['f1'] or 0 for v in per_class.values()) / len(classes),
                   'macro_f1_supported': divide(sum(v['f1'] or 0 for v in supported), len(supported)),
                   'unsupported_labels': [c for c in classes if not stats[c]['support']]}
        if task == 'risk':
            totals = {k: sum(stats[c][k] for c in classes) for k in ['tp', 'fp', 'fn', 'support']}
            summary['micro'] = label_metrics(**totals)
            summary['note'] = 'Multi-label candidate types only; not priority classification or a production risk acceptance claim.'
        else:
            summary['confusion_matrix'] = matrix
        result['tasks'][task] = summary
    times = [p['elapsed_ms'] for p in predictions if type(p.get('elapsed_ms')) in (int, float) and math.isfinite(p['elapsed_ms']) and p['elapsed_ms'] >= 0]
    times.sort()
    result['runtime'] = {'timed_predictions': len(times), 'p50_ms': statistics.median(times) if times else None,
                         'p95_ms': times[max(0, math.ceil(.95 * len(times)) - 1)] if times else None,
                         'total_tokens_reported': sum(p['total_tokens'] for p in predictions if type(p.get('total_tokens')) is int and p['total_tokens'] >= 0),
                         'token_measurements': sum(type(p.get('total_tokens')) is int and p['total_tokens'] >= 0 for p in predictions)}
    result['complete_output_coverage'] = len(all_valid) / len(references)
    return result


def markdown_report(result):
    lines = ['# Offline evaluation report', '', f"Quality: **{result['quality']}**",
             f"Samples: {result['sample_count']}; complete output coverage: {result['complete_output_coverage']:.1%}",
             '', 'Missing, failed and invalid outputs remain in the denominator.',
             'Unsupported classes are N/A in the class table and contribute zero to fixed-label Macro-F1.', '']
    for task, values in result['tasks'].items():
        lines += [f'## {task}', '', f"Macro-F1 (fixed): {values['macro_f1_fixed']:.4f}; accuracy (all): {values['accuracy_all_samples']:.4f}; failures: {values['failures']}",
                  '', '| Class | Support | TP | FP | FN | Precision | Recall | F1 |', '|---|---:|---:|---:|---:|---:|---:|---:|']
        for label, row in values['per_class'].items():
            cells = [str(row[k]) if k in {'support', 'tp', 'fp', 'fn'} else ('N/A' if row[k] is None else f'{row[k]:.4f}') for k in ['support', 'tp', 'fp', 'fn', 'precision', 'recall', 'f1']]
            lines.append('| ' + label + ' | ' + ' | '.join(cells) + ' |')
        lines += ['', f"Unsupported labels: {', '.join(values['unsupported_labels']) or 'none'}", '']
    lines += ['## Interpretation', '', 'This report does not establish production acceptance. Check data provenance, label review, split coverage and runtime failures.',
              'Full confusion matrices, runtime counters and sample IDs are in metrics.json and errors.jsonl.', '']
    return '\n'.join(lines)
