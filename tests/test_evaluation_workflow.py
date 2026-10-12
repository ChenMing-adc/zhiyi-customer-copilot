import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from evaluation.cli import write_bundle
from evaluation.datasets import CSDS_LABELS, csds_samples
from evaluation.splitting import grouped_split
from evaluation.web import create_app
from evaluation.workflow import Reviews, Runs, predict, PROMPT, outbound


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        folder = self.root / 'fixture'
        folder.mkdir()
        source = [{'conversation_id': 'S00053', 'messages': [{'seq': 1, 'role': 'customer', 'text': '退款进度？', 'emotion': 'neutral', 'intent': 'refund'}]}]
        self.samples, self.refs = csds_samples(source)
        write_bundle(folder, 'official_mock_replay', 'synthetic', CSDS_LABELS, self.samples, self.refs, grouped_split(source), 28)
        self.manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        self.reviews = Reviews(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def vote(self, name, revision, emotion='neutral', action='vote'):
        return {'sample_id': self.samples[0]['sample_id'], 'revision': revision, 'reviewer': name,
                'gold': {'emotion': emotion, 'intent': 'refund'}, 'evidence_ids': ['S00053:1'],
                'note': '人工测试标注依据', 'action': action}

    def test_review_conflict_requires_third_person_and_preserves_original(self):
        before = (self.root / 'fixture/annotations.jsonl').read_bytes()
        with TestClient(create_app(self.root)) as c:
            self.assertEqual(c.post('/api/review/fixture', json=self.vote('A', 0)).json()['resolved']['review_status'], 'reviewing')
            self.assertEqual(c.post('/api/review/fixture', json=self.vote('B', 1, 'anger')).json()['resolved']['review_status'], 'conflict')
            self.assertEqual(c.post('/api/review/fixture', json=self.vote('A', 2, action='adjudicate')).status_code, 422)
            r = c.post('/api/review/fixture', json=self.vote('C', 2, action='adjudicate')).json()
            self.assertEqual(r['resolved']['review_status'], 'adjudicated')
            self.assertEqual(len(r['history']), 3)
            self.assertEqual(c.post('/api/review/fixture', json=self.vote('A', 0)).status_code, 409)
        self.assertEqual(before, (self.root / 'fixture/annotations.jsonl').read_bytes())

    def test_repeat_same_person_does_not_pass_and_invalid_evidence_rejected(self):
        r = self.reviews.save(self.manifest, self.samples[0], self.vote('A', 0))
        r = self.reviews.save(self.manifest, self.samples[0], self.vote('A', 1))
        self.assertEqual(r['resolved']['review_status'], 'reviewing')
        invalid = self.vote('B', 2)
        invalid['evidence_ids'] = ['future']
        with self.assertRaises(ValueError):
            self.reviews.save(self.manifest, self.samples[0], invalid)
        r = self.reviews.save(self.manifest, self.samples[0], self.vote('B', 2))
        self.assertEqual(r['resolved']['review_status'], 'adjudicated')

    def test_outbound_has_no_gold_and_opaque_ids(self):
        value = outbound(self.samples[0])
        self.assertNotIn('gold', value)
        self.assertNotIn('S00053', json.dumps(value))
        self.assertEqual(value['target_message_id'], 'M1')

    def test_unauthorized_source_and_invalid_review_cannot_start_model(self):
        other = self.root / 'csds'
        other.mkdir()
        source = [{'conversation_id': 'S00053', 'messages': [{'seq': 1, 'role': 'customer', 'text': '测试', 'emotion': 'neutral', 'intent': 'refund'}]}]
        samples, refs = csds_samples(source)
        write_bundle(other, 'csds_local_annotations', 'synthetic', CSDS_LABELS, samples, refs, grouped_split(source), 28)
        with TestClient(create_app(self.root, lambda _: self.fail('must not call model'))) as c:
            self.assertEqual(c.post('/api/baseline/csds', json={'confirmed_scope': 'official:S00053'}).status_code, 403)
            self.assertEqual(c.post('/api/baseline/fixture', content='{}', headers={'Content-Type': 'text/plain'}).status_code, 415)
            invalid = self.vote('A', 0)
            invalid['reviewer'] = 123
            self.assertEqual(c.post('/api/review/fixture', json=invalid).status_code, 422)
        self.assertFalse((self.root / 'ui-runs').exists())

    def test_model_error_preserves_raw_without_secret_exception(self):
        class Client:
            async def stream(self, messages):
                yield 'not json'
                raise RuntimeError('secret-api-key')
        result = asyncio.run(predict(self.samples[0], CSDS_LABELS, PROMPT, Client()))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['raw_output'], 'not json')
        self.assertNotIn('secret-api-key', json.dumps(result))

    def test_run_scope_snapshot_and_rescore_without_calls(self):
        class Client:
            metrics = {'usage': {'total_tokens': 20}}
            async def stream(self, messages):
                yield json.dumps({'emotion': 'neutral', 'intent': 'refund', 'draft': '我先核实进度。'})
        settings = SimpleNamespace(ai_provider='openai', openai_model='qwen3.8-flash',
                    openai_base_url='https://ws-k0ry66e5zn5pf7wy.cn-beijing.maas.aliyuncs.com/v1', ai_temperature=.35, ai_max_tokens=512)
        with patch('app.core.config.get_settings', return_value=settings), TestClient(create_app(self.root, lambda _: Client())) as c:
            self.assertEqual(c.post('/api/baseline/fixture', json={}).status_code, 422)
            rid = c.post('/api/baseline/fixture', json={'confirmed_scope': 'official:S00053'}).json()['id']
            # In-process fake stream finishes without awaits after response dispatch.
            view = c.get('/api/view/fixture', params={'run_id': rid}).json()
            self.assertEqual(view['run']['status'], 'completed')
            self.assertEqual(view['counts']['samples'], 1)
            self.assertEqual(view['metrics']['received_predictions'], 1)
            c.post('/api/review/fixture', json=self.vote('A', 0, 'anger'))
            c.post('/api/review/fixture', json=self.vote('B', 1, 'anger'))
            old = c.get('/api/view/fixture', params={'run_id': rid}).json()
            self.assertEqual(old['metrics']['tasks']['emotion']['accuracy_all_samples'], 1)
            new_id = c.post('/api/rescore/fixture', json={'run_id': rid}).json()['id']
            new = c.get('/api/view/fixture', params={'run_id': new_id}).json()
            self.assertEqual(new['metrics']['tasks']['emotion']['accuracy_all_samples'], 0)
            self.assertEqual(new['metrics']['quality'], 'human_reviewed')

    def test_stop_and_failure_do_not_call_remaining_samples(self):
        calls = []
        runner = Runs(self.root)
        settings = SimpleNamespace()
        data = {'predictions': [], 'labels': CSDS_LABELS, 'prompt': PROMPT, 'status': 'running'}
        async def fake(*args):
            calls.append(1)
            runner.stops.add('a' * 32)
            return {'status': 'ok'}
        with patch('evaluation.workflow.predict', fake), patch('app.copilot.transport.ServiceAiClient', lambda _: None):
            asyncio.run(runner.execute('a' * 32, data, self.samples * 3, settings))
        self.assertEqual(len(calls), 1)
        self.assertEqual(data['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
