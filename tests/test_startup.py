"""Checks that work on a clean clone without competition data or model access."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from app.main import create_app
from app.core.config import Settings


class StartupTests(unittest.TestCase):
    def test_clean_start_without_business_data(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(root / 'demo.sqlite3', root / 'missing.xlsx')
            with TestClient(app) as client:
                self.assertEqual(client.get('/actuator/health').json()['status'], 'UP')
                page = client.get('/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('知意', page.text)
                self.assertEqual(client.get('/student.html').status_code, 404)
                self.assertEqual(client.get('/admin.html').status_code, 404)
                self.assertIsNotNone(app.state.import_error)

    def test_legacy_env_keys_do_not_change_provider_settings(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('AI_PROVIDER=openai\nOPENAI_MODEL=test-model\n'
                            'FINETUNED_MODEL_DIR=models/old\nREDIS_URL=unused\n', encoding='utf-8')
            settings = Settings(_env_file=path)
            self.assertEqual(settings.ai_provider, 'openai')
            self.assertEqual(settings.openai_model, 'test-model')


if __name__ == '__main__':
    unittest.main()
