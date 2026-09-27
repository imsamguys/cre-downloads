import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

import cre_local_api as api


class LocalApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(api.app)
        self.project = uuid4()
        self.user = str(uuid4())
        self.headers = {'Authorization': 'Bearer test-token', 'Origin': 'http://localhost:5173'}

    def test_rejects_foreign_origin_and_missing_auth(self):
        path = f'/api/projects/{self.project}/jobs'
        self.assertEqual(self.client.get(path, headers={'Origin': 'https://attacker.example'}).status_code, 403)
        self.assertEqual(self.client.get(path, headers={'Origin': 'http://localhost:5173'}).status_code, 401)

    def test_project_access_depends_on_rls_result(self):
        path = f'/api/projects/{self.project}/jobs'
        def remote(endpoint, token):
            return {'id': self.user} if endpoint == '/auth/v1/user' else []
        with patch.object(api, '_supabase_json', side_effect=remote):
            response = self.client.get(path, headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_upload_rejects_server_script_and_accepts_valid_signature(self):
        path = f'/api/projects/{self.project}/jobs'
        sample = b'\0\0\0\x18ftypisom' + b'0' * 20
        with patch.object(api, 'DATA_ROOT', Path(self.temp.name)), patch.object(api, '_identity', return_value=self.user), patch.object(api, '_prepare'):
            bad = self.client.post(path, headers=self.headers, data={'name': 'test'}, files={'source': ('video.php', sample, 'video/mp4')})
            self.assertEqual(bad.status_code, 400)
            good = self.client.post(path, headers=self.headers, data={'name': 'test'}, files={'source': ('video.mp4', sample, 'video/mp4')})
            self.assertEqual(good.status_code, 202)
            self.assertEqual(good.json()['state'], 'queued')
            self.assertNotIn('output', good.json())

    def test_preflight_private_network_requires_allowed_origin(self):
        response = self.client.options('/api/health', headers={
            'Origin': 'http://localhost:5173',
            'Access-Control-Request-Private-Network': 'true',
        })
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers.get('Access-Control-Allow-Private-Network'), 'true')


if __name__ == '__main__':
    unittest.main()
