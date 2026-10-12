"""Synthetic evaluation regressions: no source dataset, credentials or network."""
import copy
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation import cli
from evaluation.datasets import audit_csds, csds_samples, validate_csds, CSDS_LABELS
from evaluation.scoring import evaluate
from evaluation.splitting import grouped_split


def conversation(cid, text='人工构造的普通咨询'):
    return {'conversation_id': cid, 'messages': [
        {'seq': 1, 'role': 'agent', 'text': '你好，请说明问题'},
        {'seq': 2, 'role': 'customer', 'text': text, 'emotion': 'neutral', 'intent': 'product_use'},
        {'seq': 3, 'role': 'agent', 'text': '这是未来回复，不可出现在第二条的输入里'},
        {'seq': 4, 'role': 'customer', 'text': '我还想查询其他事情', 'emotion': 'confusion', 'intent': 'logistics'}]}


def reference(sid, **gold):
    return {'sample_id': sid, 'gold': gold, 'review_status': 'adjudicated',
            'reviewers': ['reviewer-a', 'reviewer-b'], 'annotation_version': 'synthetic-v1'}


class DatasetTests(unittest.TestCase):
    def test_contract_catches_schema_and_sequence_errors(self):
        valid = [conversation('A')]
        self.assertEqual(validate_csds(valid), [])
        broken = copy.deepcopy(valid)
        broken[0]['messages'][1]['seq'] = True
        broken[0]['messages'][0]['emotion'] = 'neutral'
        broken[0]['messages'][2]['role'] = {}
        broken[0]['extra'] = 'not allowed'
        self.assertGreaterEqual(len(validate_csds(broken)), 4)
        self.assertTrue(validate_csds(valid + valid))

    def test_prefix_strips_labels_future_and_masks_contacts(self):
        data = [conversation('A', '请联系13812345678或tester@example.org')]
        before = copy.deepcopy(data)
        samples, refs = csds_samples(data, window=2)
        text = json.dumps(samples[0]['input'], ensure_ascii=False)
        self.assertNotIn('未来回复', text)
        self.assertNotIn('emotion', text)
        self.assertNotIn('intent', text)
        self.assertNotIn('13812345678', text)
        self.assertNotIn('tester@example.org', text)
        self.assertEqual(samples[0]['input']['target_message_id'], 'A:2')
        self.assertEqual(len(samples[1]['input']['messages']), 2)
        self.assertEqual(refs[0]['review_status'], 'unreviewed')
        self.assertEqual(data, before)

    def test_audit_reports_conflicts_without_relabelling(self):
        data = [conversation('A', '谢谢')]
        data[0]['messages'][1]['emotion'] = 'positive'
        report = audit_csds(data)
        self.assertEqual(len(report['review_queue']), 1)
        self.assertEqual(data[0]['messages'][1]['emotion'], 'positive')

    def test_grouping_reproducible_under_input_reorder(self):
        conversations = [conversation(str(i), f'独立语料{i}') for i in range(10)]
        split = grouped_split(conversations, forced_dev=['0'], links=[['0', '1']])
        again = grouped_split(list(reversed(conversations)), forced_dev=['0'], links=[['0', '1']])
        self.assertEqual(split, again)
        self.assertEqual(split['assignments']['0'], split['assignments']['1'])
        self.assertEqual(split['assignments']['0']['split'], 'dev')
        with self.assertRaises(ValueError):
            grouped_split(conversations, links=[['missing']])

    def test_exact_and_near_duplicates_cannot_cross_split(self):
        a = conversation('A', '测试句子甲乙丙丁戊己庚辛壬癸' * 15)
        b = copy.deepcopy(a)
        b['conversation_id'] = 'B'
        c = copy.deepcopy(a)
        c['conversation_id'] = 'C'
        c['messages'][1]['text'] += '补充'
        split = grouped_split([a, b, c], forced_dev=['A'])
        self.assertEqual(len({v['group_id'] for v in split['assignments'].values()}), 1)
        self.assertTrue(all(v['split'] == 'dev' for v in split['assignments'].values()))


class ScoringTests(unittest.TestCase):
    def test_known_metrics_include_missing_failure_and_wrong_label(self):
        refs = [reference('1', emotion='neutral'), reference('2', emotion='anger'), reference('3', emotion='anger')]
        preds = [{'sample_id': '1', 'status': 'ok', 'emotion': 'neutral'}, {'sample_id': '2', 'status': 'ok', 'emotion': 'neutral'}]
        result = evaluate(refs, preds, {'emotion': ['neutral', 'anger', 'anxiety']})
        task = result['tasks']['emotion']
        self.assertAlmostEqual(task['accuracy_all_samples'], 1 / 3)
        self.assertAlmostEqual(task['accuracy_valid_outputs'], .5)
        self.assertAlmostEqual(task['macro_f1_fixed'], (2 / 3) / 3)
        self.assertEqual(task['per_class']['anger']['fn'], 2)
        self.assertEqual(task['confusion_matrix']['anger']['__failure__'], 1)
        self.assertEqual(task['unsupported_labels'], ['anxiety'])

    def test_wrong_type_and_explicit_error_never_count_as_success(self):
        refs = [reference('1', emotion='neutral'), reference('2', emotion='neutral')]
        preds = [{'sample_id': '1', 'status': 'error', 'emotion': 'neutral'}, {'sample_id': '2', 'status': 'ok', 'emotion': ['neutral']}]
        result = evaluate(refs, preds, {'emotion': ['neutral']})
        self.assertEqual(result['tasks']['emotion']['failures'], 2)
        self.assertEqual(result['complete_output_coverage'], 0)

    def test_unknown_and_duplicate_predictions_rejected(self):
        refs = [reference('1', emotion='neutral')]
        for preds in [[{'sample_id': 'bad'}], [{'sample_id': '1'}, {'sample_id': '1'}]]:
            with self.assertRaises(ValueError):
                evaluate(refs, preds, {'emotion': ['neutral']})

    def test_unreviewed_and_unknown_gold_are_not_formal_results(self):
        ref = reference('1', emotion='neutral')
        ref['reviewers'] = ['same', 'same']
        with self.assertRaises(ValueError):
            evaluate([ref], [], {'emotion': ['neutral']})
        self.assertEqual(evaluate([ref], [], {'emotion': ['neutral']}, True)['quality'], 'exploratory_unreviewed')
        ref['gold']['emotion'] = None
        with self.assertRaises(ValueError):
            evaluate([ref], [], {'emotion': ['neutral']}, True)

    def test_multilabel_risk_and_missing_gold(self):
        refs = [reference('1', risk=['health']), reference('2', risk=[])]
        preds = [{'sample_id': '1', 'status': 'ok', 'risk': []}, {'sample_id': '2', 'status': 'ok', 'risk': ['health']}]
        task = evaluate(refs, preds, {'risk': ['health', 'complaint']})['tasks']['risk']
        self.assertEqual(task['micro']['fn'], 1)
        self.assertEqual(task['micro']['fp'], 1)
        refs[0]['gold']['risk'] = None
        with self.assertRaises(ValueError):
            evaluate(refs, preds, {'risk': ['health']})


class CLITests(unittest.TestCase):
    def test_output_boundary_and_no_overwrite(self):
        with TemporaryDirectory() as folder, patch.object(cli, 'ROOT', Path(folder)):
            with self.assertRaises(ValueError):
                cli.new_output('data/zhiyi.sqlite3')
            with self.assertRaises(ValueError):
                cli.new_output('data/evaluation/../../outside')
            cli.new_output('data/evaluation/test')
            with self.assertRaises(FileExistsError):
                cli.new_output('data/evaluation/test')

    def test_prepare_score_and_tamper_protection_offline(self):
        with TemporaryDirectory() as folder, patch.object(cli, 'ROOT', Path(folder)), patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')):
            root = Path(folder)
            source = root / 'source.json'
            source.write_text(json.dumps([conversation('A')]), encoding='utf-8')
            original = source.read_bytes()
            self.assertEqual(cli.main(['prepare', '--source', str(source), '--out', 'data/evaluation/bundle']), 0)
            bundle = root / 'data/evaluation/bundle'
            manifest, samples = cli.load_bundle(bundle)
            refs = cli.load_jsonl(bundle / 'annotations.jsonl')
            for ref in refs:
                ref.update(review_status='adjudicated', reviewers=['A', 'B'])
            cli.jsonl(bundle / 'annotations.jsonl', refs)
            preds = root / 'predictions.json'
            cli.dump(preds, {'dataset_id': manifest['dataset_id'], 'split': 'dev', 'run_id': 'synthetic',
                             'prompt_version': 'none', 'predictions': [{'sample_id': r['sample_id'], 'status': 'ok', **r['gold']} for r in refs]})
            self.assertEqual(cli.main(['score', '--bundle', str(bundle), '--predictions', str(preds), '--split', 'dev', '--out', 'data/evaluation/report']), 0)
            metrics = cli.read_json(root / 'data/evaluation/report/metrics.json')
            self.assertEqual(metrics['tasks']['intent']['accuracy_all_samples'], 1)
            self.assertEqual(source.read_bytes(), original)
            (bundle / 'samples.jsonl').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Frozen file modified'):
                cli.load_bundle(bundle)

    def test_demo_is_explicitly_synthetic(self):
        with TemporaryDirectory() as folder, patch.object(cli, 'ROOT', Path(folder)):
            self.assertEqual(cli.main(['demo', '--out', 'data/evaluation/example']), 0)
            result = cli.read_json(Path(folder) / 'data/evaluation/example/metrics.json')
            self.assertIn('NOT_model_accuracy', result['quality'])


if __name__ == '__main__':
    unittest.main()
