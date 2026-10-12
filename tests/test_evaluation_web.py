import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from evaluation.cli import write_bundle
from evaluation.datasets import CSDS_LABELS, csds_samples
from evaluation.splitting import grouped_split
from evaluation.web import create_app


class EvaluationWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        path = self.root / 'csds-test'
        path.mkdir()
        source = [{'conversation_id': 'A', 'messages': [{'seq': 1, 'role': 'customer', 'text': '<script>alert(1)</script>', 'emotion': 'neutral', 'intent': 'refund'}]}]
        samples, refs = csds_samples(source)
        write_bundle(path, 'csds_local_annotations', 'synthetic', CSDS_LABELS, samples, refs, grouped_split(source), 28)
        self.manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        self.client = TestClient(create_app(self.root))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def payload(self):
        return {'dataset_id': self.manifest['dataset_id'], 'split': 'dev', 'run_id': 'synthetic test', 'prompt_version': 'v0',
                'predictions': [{'sample_id': 'csds:A:1', 'status': 'ok', 'emotion': 'neutral', 'intent': 'refund', 'raw_output': '{"test":true}', 'draft': '人工测试草稿'}]}

    def test_catalog_and_waiting_state_have_no_fake_scores(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(len(self.client.get('/api/datasets').json()['datasets']), 1)
        view = self.client.get('/api/view/csds-test').json()
        self.assertEqual(view['metrics']['tasks'], {})
        self.assertEqual(view['rows'][0]['status'], '待评测')
        self.assertFalse((self.root / 'ui-runs').exists())

    def test_import_score_and_raw_response_are_persisted_separately(self):
        original = (self.root / 'csds-test/annotations.jsonl').read_bytes()
        response = self.client.post('/api/import/csds-test', json=self.payload())
        self.assertEqual(response.status_code, 200)
        rid = response.json()['id']
        view = self.client.get('/api/view/csds-test', params={'run_id': rid}).json()
        self.assertEqual(view['metrics']['tasks']['emotion']['accuracy_all_samples'], 1)
        self.assertEqual(view['metrics']['quality'], 'exploratory')
        detail = self.client.get('/api/sample/csds-test', params={'sample_id': 'csds:A:1', 'run_id': rid}).json()
        self.assertEqual(detail['prediction']['draft'], '人工测试草稿')
        self.assertEqual((self.root / 'csds-test/annotations.jsonl').read_bytes(), original)

    def test_cross_origin_and_unknown_samples_rejected(self):
        self.assertEqual(self.client.post('/api/import/csds-test', json=self.payload(), headers={'Origin': 'https://example.org'}).status_code, 403)
        data = self.payload()
        data['predictions'][0]['sample_id'] = 'unknown'
        self.assertEqual(self.client.post('/api/import/csds-test', json=data).status_code, 422)
        self.assertFalse((self.root / 'ui-runs').exists())
        self.assertEqual(self.client.get('/api/view/csds-test', params={'run_id': '../secret'}).status_code, 404)

    def test_official_missing_gold_does_not_become_zero_accuracy(self):
        path = self.root / 'csds-test/annotations.jsonl'
        row = json.loads(path.read_text(encoding='utf-8'))
        row['gold'] = {'emotion': None, 'intent': None}
        path.write_text(json.dumps(row) + '\n', encoding='utf-8')
        rid = self.client.post('/api/import/csds-test', json=self.payload()).json()['id']
        metrics = self.client.get('/api/view/csds-test', params={'run_id': rid}).json()['metrics']
        self.assertEqual(metrics['tasks'], {})
        self.assertEqual(metrics['pending_gold']['emotion'], 1)

    def test_demo_is_explicit_and_creates_no_files(self):
        before = set(self.root.rglob('*'))
        demo = self.client.get('/api/demo').json()
        self.assertEqual(demo['metrics']['quality'], 'synthetic')
        self.assertEqual(len(demo['details']), 5)
        self.assertEqual(before, set(self.root.rglob('*')))


if __name__ == '__main__':
    unittest.main()
