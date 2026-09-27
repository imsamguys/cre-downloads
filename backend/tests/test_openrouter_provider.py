import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from cre_brain.providers.base import ProviderError
from cre_brain.providers.openrouter import OpenRouterProvider, FREE_MODELS


class OpenRouterTests(unittest.TestCase):
    def provider(self):
        p = OpenRouterProvider('test-key', list(FREE_MODELS)[1])
        p._checked = True
        return p

    def run_proposal(self, provider):
        return provider.run_specialist(bot_id='test', instructions='Return a proposal', input_data={},
            response_schema={'type': 'object', 'properties': {'ok': {'const': True}},
                             'required': ['ok'], 'additionalProperties': False})

    def response(self, content='{"ok": true}', finish='stop'):
        return Mock(status_code=200, json=Mock(return_value={
            'choices': [{'finish_reason': finish, 'message': {'content': content}}]}))

    @patch('cre_brain.providers.openrouter.requests.request')
    def test_valid_json_and_free_price_guards(self, request):
        request.return_value = self.response()
        result = self.run_proposal(self.provider())
        self.assertEqual(result.output, {'ok': True})
        body = request.call_args.kwargs['json']
        self.assertTrue(body['model'].endswith(':free'))
        self.assertEqual(set(body['provider']['max_price'].values()), {0})
        self.assertNotIn('models', body)
        self.assertIn('text only', body['messages'][0]['content'])

    def test_paid_model_rejected(self):
        with self.assertRaises(ProviderError):
            OpenRouterProvider('test-key', 'qwen/paid-model')

    @patch('cre_brain.providers.openrouter.requests.request')
    def test_invalid_truncated_and_schema_invalid_responses_rejected(self, request):
        for content, finish in [('not json', 'stop'), ('{"ok": false}', 'stop'),
                                ('{"ok": true}', 'length'), ('[]', 'stop')]:
            request.return_value = self.response(content, finish)
            with self.assertRaises(ProviderError):
                self.run_proposal(self.provider())

    @patch('cre_brain.providers.openrouter.requests.request')
    def test_quota_error_is_safe_and_not_retried(self, request):
        request.return_value = Mock(status_code=429)
        with self.assertRaisesRegex(ProviderError, 'quota'):
            self.run_proposal(self.provider())
        self.assertEqual(request.call_count, 1)

    @patch('cre_brain.providers.openrouter.requests.request')
    def test_delisted_or_paid_model_fails_preflight(self, request):
        for models in [[], [{'id': list(FREE_MODELS)[1], 'pricing': {'prompt': '1', 'completion': '0'}}]]:
            request.side_effect = [Mock(status_code=200, json=lambda: {'data': {}}),
                                   Mock(status_code=200, json=lambda: {'data': models})]
            with self.assertRaises(ProviderError):
                self.provider().check_connection()

    @patch('cre_brain.providers.openrouter.requests.request')
    def test_catalog_controls_visual_support(self, request):
        for modalities in [['text'], ['text', 'image']]:
            request.side_effect = [Mock(status_code=200, json=lambda: {'data': {}}),
                Mock(status_code=200, json=lambda: {'data': [{'id': list(FREE_MODELS)[1],
                    'pricing': {'prompt': '0', 'completion': '0'},
                    'architecture': {'input_modalities': modalities}}]})]
            provider = self.provider()
            provider.check_connection()
            self.assertEqual(provider.supports_visual_input, 'image' in modalities)

    def test_glm_does_not_read_or_upload_media(self):
        provider = self.provider()
        provider.set_source_video('does-not-exist.mp4')
        provider.add_broll_video('does-not-exist.mp4', 'broll')
        self.assertEqual(provider._media, [])

    def test_visual_preview_sampling_has_labels_and_bounded_images(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'preview.avi'
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 64))
            self.assertTrue(writer.isOpened())
            for i in range(20):
                writer.write(np.full((64, 64, 3), i * 10, dtype=np.uint8))
            writer.release()
            provider = self.provider()
            provider.supports_visual_input = True
            provider.set_source_video(path)
            self.assertEqual(len(provider._media), 12)
            self.assertIn('SOURCE', provider._media[0]['text'])
            self.assertTrue(provider._media[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            provider.add_broll_video(path, 'candidate-1')
            self.assertEqual(len(provider._media), 16)
            self.assertIn('candidate-1', provider._media[12]['text'])


if __name__ == '__main__':
    unittest.main()
