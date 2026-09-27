import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cre_brain.source_prep import _local_video, normalize_source


class NormalizationTests(unittest.TestCase):
    def run_case(self, measurement):
        first = json.dumps(dict(input_i='-20', input_tp='-6', input_lra='3',
                                input_thresh='-30', target_offset='0.1'))
        probe = json.dumps({'streams': [
            {'codec_type': 'video', 'width': 848, 'height': 480, 'duration': '30'},
            {'codec_type': 'audio', 'sample_rate': '48000'}]})
        outputs = [SimpleNamespace(stderr=first), SimpleNamespace(),
                   SimpleNamespace(stdout=probe), SimpleNamespace(stderr=measurement)]
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'input.mp4'
            source.write_bytes(b'local test file')
            with patch('cre_brain.source_prep._run', side_effect=outputs) as run, \
                 patch('cre_brain.source_prep._sha256', return_value='a' * 64):
                result = normalize_source(source, Path(directory) / 'out.mp4')
                self.assertIn('measured_I=-20', run.call_args_list[1].args[0][
                    run.call_args_list[1].args[0].index('-af') + 1])
                return result

    def test_source_must_be_an_existing_local_video(self):
        with TemporaryDirectory() as directory:
            text_file = Path(directory) / 'notes.txt'
            text_file.write_text('not video', encoding='utf-8')
            with self.assertRaises(ValueError):
                _local_video(text_file)
            with self.assertRaises(ValueError):
                _local_video('https://example.com/video.mp4')

    def test_measured_pass_and_delivery_evidence(self):
        result = self.run_case('{"input_i":"-16.15","input_tp":"-1.48"}')
        self.assertEqual(-16150, result['integrated_lufs_milli'])
        self.assertEqual(-1480, result['true_peak_mdbtp'])

    def test_missing_or_invalid_evidence_rejected(self):
        for data in ('', '{"input_i":"-16.55","input_tp":"-1.18"}',
                     '{"input_i":"-16","input_tp":"-0.5"}',
                     '{"input_i":"-inf","input_tp":"-inf"}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.run_case(data)
