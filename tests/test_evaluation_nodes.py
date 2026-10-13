import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.copilot.analysis import analysis_prompt, reply_prompt, situation_messages, reply_messages, Situation
from evaluation.cli import write_bundle, load_bundle, load_jsonl
from evaluation.datasets import CSDS_LABELS, csds_samples
from evaluation.nodes import builtin_prompts, context, predict_node
from evaluation.refresh import refresh
from evaluation.splitting import grouped_split
from evaluation.web import create_app


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'fixture'
        self.folder.mkdir()
        self.data = [{'conversation_id': 'C1', 'messages': [
            dict(seq=1, role='customer', text='退款进度？', emotion='neutral', intent='refund'),
            dict(seq=2, role='agent', text='未来信息绝不能进入第一个截点'),
            dict(seq=3, role='customer', text='好的', emotion='neutral', intent='refund')]}]
        self.samples, refs = csds_samples(self.data)
        write_bundle(self.folder, 'csds_local_annotations', 'synthetic', CSDS_LABELS, self.samples, refs, grouped_split(self.data), 28)
        self.situation = dict(intent='refund', emotion='neutral', trend='平稳', emotion_evidence=['M1'],
                              summary=[dict(text='询问退款进度', evidence_ids=['M1'])], missing_info=[], next_action='核实进度')
        self.settings = SimpleNamespace(ai_provider='openai', openai_model='qwen3.8-flash',
            openai_base_url='https://ws-k0ry66e5zn5pf7wy.cn-beijing.maas.aliyuncs.com/v1', ai_temperature=.35, ai_max_tokens=512)

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_contract_and_no_gold_or_future(self):
        ctx = context(self.samples[0])
        default, aliases = situation_messages(ctx)
        self.assertEqual(default[0].content, analysis_prompt())
        product = Situation.model_validate({**self.situation, 'intent': '退款打款', 'emotion': '平静'})
        self.assertEqual(reply_messages(ctx, product)[0].content, reply_prompt())
        captured = []
        output = self.situation
        class Client:
            async def stream(self, messages):
                captured.extend(messages)
                yield json.dumps(output)
        result = asyncio.run(predict_node(self.samples[0], CSDS_LABELS, builtin_prompts()[0]['prompt'], Client(), 'analysis'))
        self.assertEqual(result['status'], 'ok')
        user = json.loads(captured[1].content)
        self.assertEqual(len(user['messages']), 1)
        self.assertNotIn('gold', user)
        self.assertNotIn('emotion', user['messages'][0])
        self.assertNotIn('未来信息', captured[1].content)
        self.assertNotIn('C1', captured[1].content)
        self.assertNotIn('{{intent_labels}}', captured[0].content)
        self.assertIn('refund', captured[0].content)
        # Evaluation label overrides must not change the product defaults.
        self.assertIn('退款打款', analysis_prompt())
        self.assertNotIn('refund', analysis_prompt())

    def test_analysis_reply_and_review_api(self):
        calls = []
        output = self.situation
        class Client:
            async def stream(self, messages):
                calls.append([m.model_dump() for m in messages])
                yield json.dumps(output) if '只分析' in messages[0].content else '保证明天到账40元'
        with patch('app.core.config.get_settings', return_value=self.settings), TestClient(create_app(self.root, lambda _: Client())) as c:
            for mode in ['analysis', 'reply']:
                library = c.get('/api/prompts', params={'mode': mode}).json()
                self.assertTrue(all(e['mode'] == mode for e in library['entries']))
            selection = dict(mode='analysis', split='dev', sample_ids=[self.samples[0]['sample_id']], prompt_id='builtin:situation-v1')
            preview = c.post('/api/baseline/preview/fixture', json=selection).json()
            selection['selection_hash'] = preview['selection_hash']
            rid = c.post('/api/baseline/fixture', json=selection).json()['id']
            view = c.get('/api/view/fixture', params={'run_id': rid}).json()
            self.assertEqual(view['run']['status'], 'completed')
            self.assertEqual(view['metrics']['tasks']['intent']['accuracy_all_samples'], 1)
            wrong = {**selection, 'mode': 'reply'}
            self.assertEqual(c.post('/api/baseline/fixture', json=wrong).status_code, 422)
            reply = dict(mode='reply', split='dev', prompt_id='builtin:reply-v1', analysis_run_id=rid,
                         sample_ids=selection['sample_ids'])
            catalog = c.get('/api/baseline/catalog/fixture', params={'analysis_run_id': rid}).json()
            self.assertEqual([s['id'] for g in catalog['groups'] for s in g['samples']], selection['sample_ids'])
            p = c.post('/api/baseline/preview/fixture', json=reply).json()
            self.assertEqual(json.loads(p['samples'][0]['input'][1]['content'])['analysis'], self.situation)
            missing = {**reply, 'sample_ids': [s['sample_id'] for s in self.samples]}
            self.assertEqual(c.post('/api/baseline/preview/fixture', json=missing).status_code, 422)
            reply['selection_hash'] = p['selection_hash']
            custom = c.post('/api/prompts', json=dict(mode='reply', version='reply-test', prompt=reply_prompt()+'请特别简洁。')).json()
            self.assertEqual(c.post('/api/baseline/fixture', json={**reply, 'prompt_id': custom['id']}).status_code, 409)
            rrid = c.post('/api/baseline/fixture', json=reply).json()['id']
            result = c.get('/api/view/fixture', params={'run_id': rrid}).json()
            self.assertEqual(result['metrics']['tasks'], {})
            self.assertEqual(result['metrics']['reply']['validator_pass_rate'], 0)
            self.assertEqual(result['metrics']['reply']['reviewed'], 0)
            self.assertEqual(result['rows'][0]['status'], '需检查')
            rating = dict(run_id=rrid, sample_id=selection['sample_ids'][0], split='dev', revision=0,
                          reviewer='测试评分人', note='包含未经证实的金额和时限',
                          scores=dict(grounded=False, responsive=True, empathetic=False, actionable=False))
            self.assertEqual(c.post('/api/reply-review/fixture', json=rating).status_code, 200)
            self.assertEqual(c.post('/api/reply-review/fixture', json=rating).status_code, 409)
            rated = c.get('/api/view/fixture', params={'run_id': rrid}).json()['metrics']['reply']
            self.assertEqual(rated['reviewed'], 1)
            self.assertEqual(rated['human_pass_rate'], 0)
            self.assertEqual(len(calls), 2)  # No hidden repair, judge, or re-analysis calls.
            saved = json.loads((self.root / 'ui-runs' / (rrid+'.json')).read_text(encoding='utf-8'))
            self.assertEqual(saved['analysis_run']['analyses'][selection['sample_ids'][0]], self.situation)

    def test_bad_evidence_fails_analysis(self):
        output = {**self.situation, 'emotion_evidence': ['M999']}
        class Client:
            async def stream(self, messages):
                yield json.dumps(output)
        result = asyncio.run(predict_node(self.samples[0], CSDS_LABELS, builtin_prompts()[0]['prompt'], Client(), 'analysis'))
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('analysis', result)

    def test_resume_reuses_success_and_preserves_failed_run(self):
        calls = []
        good = self.situation
        class Client:
            async def stream(self, messages):
                calls.append(messages)
                yield json.dumps({**good, 'emotion_evidence': []} if len(calls) == 2 else good)
        with patch('app.core.config.get_settings', return_value=self.settings), TestClient(create_app(self.root, lambda _: Client())) as c:
            selection = dict(mode='analysis', split='dev', prompt_id='builtin:situation-v1')
            preview = c.post('/api/baseline/preview/fixture', json=selection).json()
            rid = c.post('/api/baseline/fixture', json={**selection, 'selection_hash': preview['selection_hash']}).json()['id']
            original_path = self.root / 'ui-runs' / (rid + '.json')
            before = original_path.read_bytes()
            original = json.loads(before)
            self.assertEqual(original['status'], 'failed')
            self.assertIn('情绪缺少买家证据', original['predictions'][1]['error'])
            plan = c.get('/api/resume/fixture', params={'run_id': rid}).json()
            self.assertEqual((plan['reused'], plan['pending']), (1, 1))
            self.assertEqual(c.post('/api/resume/fixture', json={'run_id': rid, 'token': 'stale'}).status_code, 409)
            self.settings.ai_max_tokens = 513
            self.assertEqual(c.get('/api/resume/fixture', params={'run_id': rid}).status_code, 409)
            self.settings.ai_max_tokens = 512
            resumed = c.post('/api/resume/fixture', json={'run_id': rid, 'token': plan['token']}).json()['id']
            saved = json.loads((self.root / 'ui-runs' / (resumed + '.json')).read_text(encoding='utf-8'))
            self.assertEqual(len(calls), 3)
            self.assertEqual(saved['status'], 'completed')
            self.assertEqual(saved['resumed_from'], rid)
            self.assertEqual(saved['references'], original['references'])
            self.assertEqual(saved['prompt'], original['prompt'])
            self.assertEqual(saved['predictions'][0], original['predictions'][0])
            self.assertEqual(len({p['sample_id'] for p in saved['predictions']}), 2)
            self.assertEqual(original_path.read_bytes(), before)
            self.assertEqual(c.get('/api/resume/fixture', params={'run_id': resumed}).status_code, 422)

    def test_refresh_keeps_split_and_changes_identity_without_old_mutation(self):
        src = self.root / 'annotations.json'
        src.write_text(json.dumps(self.data), encoding='utf-8')
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        with patch('evaluation.refresh.new_output', side_effect=lambda _: self.make_new()):
            refresh(src, self.folder, 'unused', '用户提供的已完成标注')
        old, _ = load_bundle(self.folder)
        new, samples = load_bundle(self.root / 'new')
        self.assertNotEqual(new['dataset_id'], old['dataset_id'])
        self.assertEqual((self.folder / 'split.json').read_bytes(), (self.root / 'new/split.json').read_bytes())
        self.assertTrue(all(r['review_status'] == 'confirmed' for r in load_jsonl(self.root / 'new/annotations.jsonl')))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.folder.iterdir()})
        self.assertEqual(samples[0]['input']['facts'], [])

    def make_new(self):
        folder = self.root / 'new'
        folder.mkdir()
        return folder
