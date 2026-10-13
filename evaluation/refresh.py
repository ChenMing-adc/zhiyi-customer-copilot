"""Import user-completed JSON annotations as a new, traceable dataset version."""
import argparse
from pathlib import Path

from evaluation.cli import ROOT, dump, load_bundle, new_output, write_bundle
from evaluation.datasets import CSDS_LABELS, csds_samples, read_json, sha256, validate_csds


def refresh(source, previous, out, reviewer):
    data = read_json(source)
    errors = validate_csds(data)
    if errors:
        raise ValueError('; '.join(errors[:10]))
    if not reviewer.strip():
        raise ValueError('需要记录标注来源')
    manifest, old_samples = load_bundle(previous)
    split = read_json(Path(previous) / 'split.json')
    if set(split['assignments']) != {c['conversation_id'] for c in data}:
        raise ValueError('会话成员变化，需要重新审核数据划分')
    samples, refs = csds_samples(data, manifest['window'])
    prefix = 'official' if manifest['source'] == 'official_mock_replay' else 'csds'
    for sample, ref in zip(samples, refs):
        sample['import_contract'] = 'user-confirmed-chat-json-v1'
        sample['sample_id'] = ref['sample_id'] = f"{prefix}:{sample['conversation_id']}:{sample['target_seq']}"
        ref.update(review_status='confirmed', reviewers=[reviewer],
                   annotation_version='supplied-json-' + sha256(source)[:12],
                   evidence_ids=[sample['input']['target_message_id']],
                   note='用户提供并确认标注完成；按源文件原值导入，未由模型重标。')
    old_ids = {(s['conversation_id'], s['target_seq']) for s in old_samples}
    if old_ids != {(s['conversation_id'], s['target_seq']) for s in samples}:
        raise ValueError('买家截点变化，需要人工核对后创建新划分')
    target = new_output(out)
    write_bundle(target, manifest['source'], sha256(source), CSDS_LABELS,
                 samples, refs, split, manifest['window'])
    updated = read_json(target / 'manifest.json')
    updated.update(label_review='user_confirmed', supersedes=manifest['dataset_id'],
                   input_origin='annotated_chat_json', input_scope='chat_only',
                   annotation_source=Path(source).name,
                   annotation_policy='supplied-json-labels',
                   annotation_note='只有情绪与意图标准答案；不含风险和推荐回复质量标注。')
    dump(target / 'manifest.json', updated)
    return {'key': target.name, 'samples': len(samples), 'conversations': len(data), 'confirmed': len(refs)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'previous', 'out', 'reviewer'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    print(refresh(args.source, ROOT / args.previous, args.out, args.reviewer))
