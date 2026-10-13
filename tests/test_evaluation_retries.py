import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from evaluation.workflow import Runs


class RetryTests(unittest.TestCase):
    def execute(self, predictor, waiter=None):
        with TemporaryDirectory() as folder:
            runner = Runs(folder, lambda _: None)
            data = {'predictions': [], 'labels': {}, 'prompt': 'frozen', 'status': 'running'}
            async def run():
                with patch('evaluation.workflow.predict', predictor):
                    runner.retry_wait = waiter(runner) if waiter else AsyncMock()
                    await runner.execute('a'*32, data, [{'sample_id': 'one'}, {'sample_id': 'two'}], SimpleNamespace())
            asyncio.run(run())
            self.assertEqual(json.loads((Path(folder)/('ui-runs/'+'a'*32+'.json')).read_text(encoding='utf-8')), data)
            return data

    def test_retry_success_continues_next_sample_and_aggregates_usage(self):
        calls = []
        async def predict(sample, labels, prompt, client):
            calls.append(sample['sample_id'])
            self.assertEqual(prompt, 'frozen')
            return dict(sample_id=sample['sample_id'], status='error' if len(calls)<3 else 'ok',
                        raw_output=str(len(calls)), total_tokens=10, elapsed_ms=1)
        data = self.execute(predict)
        self.assertEqual(calls, ['one', 'one', 'one', 'two'])
        self.assertEqual(data['status'], 'completed')
        self.assertEqual(len(data['predictions']), 2)
        first = data['predictions'][0]
        self.assertEqual(first['attempt_count'], 3)
        self.assertEqual(first['total_tokens'], 30)
        self.assertEqual([a['raw_output'] for a in first['attempts']], ['1', '2', '3'])
        self.assertNotIn('current_attempt', data)

    def test_five_retries_then_stop_without_next_sample(self):
        calls = []
        async def predict(sample, *args):
            calls.append(sample['sample_id'])
            return dict(sample_id=sample['sample_id'], status='error', error='invalid')
        data = self.execute(predict)
        self.assertEqual(calls, ['one']*6)
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['predictions'][0]['attempt_count'], 6)

    def test_stop_during_backoff_prevents_retry(self):
        async def predict(sample, *args):
            return dict(sample_id=sample['sample_id'], status='error')
        def waiter(runner):
            async def stop(rid, delay):
                self.assertEqual(delay, 1)
                runner.stops.add(rid)
            return stop
        data = self.execute(predict, waiter)
        self.assertEqual(data['status'], 'cancelled')
        self.assertEqual(data['predictions'][0]['attempt_count'], 1)

    def test_shutdown_during_backoff_preserves_failed_attempt(self):
        async def predict(sample, *args):
            return dict(sample_id=sample['sample_id'], status='error', raw_output='partial')
        def waiter(runner):
            async def interrupt(rid, delay):
                raise asyncio.CancelledError()
            return interrupt
        data = self.execute(predict, waiter)
        self.assertEqual(data['status'], 'interrupted')
        self.assertEqual(data['predictions'][0]['attempts'][0]['raw_output'], 'partial')
