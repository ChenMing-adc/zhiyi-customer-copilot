"""Offline-only CLI. All generated artifacts stay in ignored data/evaluation/."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from evaluation.datasets import (CSDS_LABELS, PRODUCT_LABELS, SEEN_CSDS, audit_csds,
                                 csds_samples, official_samples, read_json, sha256)
from evaluation.scoring import evaluate, markdown_report
from evaluation.splitting import grouped_split

ROOT = Path(__file__).resolve().parents[1]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def jsonl(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')


def load_jsonl(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not all(isinstance(row, dict) and isinstance(row.get('sample_id'), str) for row in rows):
        raise ValueError('JSONL rows must be objects with string sample_id')
    return rows


def new_output(path):
    target = (ROOT / path).resolve()
    base = (ROOT / 'data/evaluation').resolve()
    # Reject redirected base directories as well as caller traversal/symlinks.
    if not base.is_relative_to(ROOT.resolve()) or not target.is_relative_to(base) or target == base:
        raise ValueError('Outputs must be a new subdirectory of Agent/data/evaluation/')
    target.mkdir(parents=True, exist_ok=False)
    return target


def write_bundle(out, source, source_hash, labels, samples, references, split, window):
    for sample in samples:
        sample.update(split['assignments'][sample['conversation_id']])
    jsonl(out / 'samples.jsonl', samples)
    jsonl(out / 'annotations.jsonl', references)
    dump(out / 'split.json', split)
    files = {name: sha256(out / name) for name in ['samples.jsonl', 'split.json']}
    dataset_id = hashlib.sha256(json.dumps({'source': source_hash, 'files': files, 'labels': labels}, sort_keys=True).encode()).hexdigest()
    dump(out / 'manifest.json', {'format_version': 1, 'dataset_id': dataset_id, 'source': source,
                               'source_sha256': source_hash, 'labels': labels, 'window': window,
                               'immutable_files': files, 'initial_annotations_sha256': sha256(out / 'annotations.jsonl'),
                               'sample_count': len(samples), 'label_review': 'unconfirmed',
                               'privacy_review': 'pending', 'external_model_calls': 0,
                               'split_policy': 'fixed membership; development IDs and near duplicates grouped; manual template/coverage review required'})
    coverage = {}
    refs = {row['sample_id']: row for row in references}
    for name in ['dev', 'validation', 'test']:
        chosen = [s for s in samples if s['split'] == name]
        counts = {task: dict(Counter(refs[s['sample_id']]['gold'][task] for s in chosen if isinstance(refs[s['sample_id']]['gold'][task], str))) for task in labels}
        coverage[name] = {'samples': len(chosen), 'conversations': len({s['conversation_id'] for s in chosen}),
                          'label_counts': counts, 'missing_labels': {task: [c for c in classes if c not in counts[task]] for task, classes in labels.items()}}
    dump(out / 'coverage.json', coverage)
    (out / 'README.txt').write_text(
        'OFFLINE DATA BUNDLE. Not a model result. Do not publish or send externally.\n'
        'samples.jsonl is model input only; annotations.jsonl contains gold and review state.\n'
        'Only edit annotations.jsonl for human review; keep split and samples immutable.\n'
        'Set review_status=adjudicated, at least two distinct reviewers, and annotation_version after review.\n'
        'Never treat missing risk gold (null) as a negative ([]).\n'
        'Membership is fixed, but class coverage/template grouping still needs human acceptance.\n', encoding='utf-8')


def load_bundle(path):
    path = Path(path)
    manifest = read_json(path / 'manifest.json')
    if manifest.get('format_version') != 1:
        raise ValueError('Unknown bundle version')
    for name in ['samples.jsonl', 'split.json']:
        if sha256(path / name) != manifest['immutable_files'][name]:
            raise ValueError(f'Frozen file modified: {name}; create a new dataset version')
    identity = hashlib.sha256(json.dumps({'source': manifest['source_sha256'], 'files': manifest['immutable_files'], 'labels': manifest['labels']}, sort_keys=True).encode()).hexdigest()
    if identity != manifest['dataset_id']:
        raise ValueError('Dataset manifest identity mismatch')
    samples = load_jsonl(path / 'samples.jsonl')
    if len(samples) != manifest['sample_count'] or len({s['sample_id'] for s in samples}) != len(samples):
        raise ValueError('Invalid sample count or duplicate sample IDs')
    return manifest, samples


def score_bundle(args):
    manifest, samples = load_bundle(args.bundle)
    predictions = read_json(args.predictions)
    if not isinstance(predictions, dict):
        raise ValueError('Predictions file must be an object containing run metadata and predictions array')
    if predictions.get('dataset_id') != manifest['dataset_id'] or predictions.get('split') != args.split:
        raise ValueError('Predictions must identify the exact dataset_id and selected split')
    if not predictions.get('run_id') or not predictions.get('prompt_version'):
        raise ValueError('Predictions require run_id and prompt_version')
    annotation_path = Path(args.annotations) if args.annotations else Path(args.bundle) / 'annotations.jsonl'
    references = load_jsonl(annotation_path)
    ids = {s['sample_id'] for s in samples}
    if len({r['sample_id'] for r in references}) != len(references) or {r['sample_id'] for r in references} != ids:
        raise ValueError('Annotations must match every bundle sample exactly once')
    selected = {s['sample_id'] for s in samples if s['split'] == args.split}
    result = evaluate([r for r in references if r['sample_id'] in selected], predictions['predictions'],
                      manifest['labels'], args.allow_unreviewed)
    result['run'] = {'dataset_id': manifest['dataset_id'], 'source': manifest['source'], 'split': args.split,
                     'run_id': predictions['run_id'], 'prompt_version': predictions['prompt_version'],
                     'annotations_sha256': sha256(annotation_path), 'predictions_sha256': sha256(args.predictions),
                     'external_model_calls_by_scorer': 0}
    out = new_output(args.out)
    write_report(out, result)
    return {'output': str(out), 'samples': result['sample_count'], 'quality': result['quality']}


def write_report(out, result):
    dump(out / 'metrics.json', result)
    jsonl(out / 'errors.jsonl', result['errors'])
    (out / 'report.md').write_text(markdown_report(result), encoding='utf-8')


def demo(args):
    # Deliberately contains wrong, missing and failed predictions: not fabricated model results.
    refs = [{'sample_id': str(i), 'gold': {'emotion': label}, 'review_status': 'adjudicated',
             'reviewers': ['synthetic-A', 'synthetic-B'], 'annotation_version': 'synthetic-v1'}
            for i, label in enumerate(['neutral', 'anger', 'anxiety', 'neutral', 'anger'])]
    preds = [{'sample_id': '0', 'status': 'ok', 'emotion': 'neutral'},
             {'sample_id': '1', 'status': 'ok', 'emotion': 'neutral'},
             {'sample_id': '2', 'status': 'error'},
             {'sample_id': '3', 'status': 'ok', 'emotion': 'neutral'}]
    result = evaluate(refs, preds, {'emotion': CSDS_LABELS['emotion']})
    result['quality'] = 'synthetic_example_NOT_model_accuracy'
    out = new_output(args.out)
    dump(out / 'synthetic-input.json', {'references': refs, 'predictions': preds})
    write_report(out, result)
    return {'output': str(out), 'quality': result['quality']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Isolated offline evaluation. No model calls or product DB access.')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['audit', 'prepare', 'prepare-official']:
        command = commands.add_parser(name)
        command.add_argument('--source', required=True)
        command.add_argument('--out', required=True, help='New directory within data/evaluation/')
        if name != 'audit':
            command.add_argument('--seed', type=int, default=20261012)
            command.add_argument('--window', type=int, default=28)
            command.add_argument('--groups', help='Local JSON array of linked conversation-ID arrays')
            command.add_argument('--dev-ids', nargs='*', default=[])
    command = commands.add_parser('score')
    command.add_argument('--bundle', required=True)
    command.add_argument('--predictions', required=True)
    command.add_argument('--annotations')
    command.add_argument('--split', choices=['dev', 'validation', 'test'], required=True)
    command.add_argument('--allow-unreviewed', action='store_true')
    command.add_argument('--out', required=True)
    command = commands.add_parser('demo')
    command.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'score':
            summary = score_bundle(args)
        elif args.command == 'demo':
            summary = demo(args)
        else:
            path = Path(args.source)
            if args.command == 'prepare-official':
                if args.window < 1:
                    raise ValueError('window must be positive')
                samples, references, conversations, links = official_samples(path, args.window)
                labels, source, report = PRODUCT_LABELS, 'official_mock_replay', None
                default_dev = {'S00053'}
            else:
                conversations = read_json(path)
                report = audit_csds(conversations)
                if args.command == 'audit':
                    out = new_output(args.out)
                    dump(out / 'audit.json', {**report, 'source_sha256': sha256(path)})
                    print(json.dumps({'output': str(out), 'errors': len(report['errors'])}, ensure_ascii=False))
                    return 1 if report['errors'] else 0
                if report['errors']:
                    raise ValueError('Dataset contract errors: run audit first')
                if args.window < 1:
                    raise ValueError('window must be positive')
                samples, references = csds_samples(conversations, args.window)
                labels, source, links, default_dev = CSDS_LABELS, 'csds_local_annotations', [], SEEN_CSDS
            if args.groups:
                links += read_json(args.groups)
            available = {c['conversation_id'] for c in conversations}
            forced = (default_dev & available) | set(args.dev_ids)
            split = grouped_split(conversations, args.seed, forced, links)
            out = new_output(args.out)
            write_bundle(out, source, sha256(path), labels, samples, references, split, args.window)
            if report:
                dump(out / 'audit.json', report)
            summary = {'output': str(out), 'samples': len(samples), 'conversations': len(conversations),
                       'split_counts': split['conversation_counts'], 'external_model_calls': 0}
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Evaluation stopped: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
