from pathlib import Path
from .probe import probe_media
from ..compiler.snapshot import file_hash
from ..validators.report import Issue


def normalization_issues(actual, path, fps=30):
    errors = []
    def check(field, expected, code):
        if actual.get(field) != expected:
            errors.append(Issue(code, path + '/' + field, 'Media requires normalization.', expected, actual.get(field)))
    if actual.get('kind') == 'video':
        for key, value in [('fps_num', fps), ('fps_den', 1), ('cfr', True), ('video_start_zero', True),
                           ('square_pixels', True), ('rotation_degrees', 0), ('color_primaries', 1),
                           ('color_transfer', 1), ('color_space', 1)]:
            check(key, value, 'MEDIA_TIMEBASE' if key in ('fps_num', 'fps_den', 'cfr') else 'MEDIA_FORMAT')
    if actual.get('has_audio'):
        for key, value in [('audio_sample_rate', 48000), ('audio_channels', 2),
                           ('audio_start_zero', True), ('audio_contiguous', True)]:
            check(key, value, 'AUDIO_FORMAT')
    return errors


def validate_assets(plan, registry, *, probe=probe_media):
    errors = []; resolved = []
    if not isinstance(registry, dict) or registry.get('version') != '1.0.0' or not isinstance(registry.get('assets'), dict):
        return [Issue('REGISTRY_INVALID', '/registry', 'Expected trusted registry v1 with assets map.')], []
    fps = plan['render']['timebase']['fps']
    for index, expected in enumerate(plan['render']['assets']):
        ptr = f'/render/assets/{index}'; entry = registry['assets'].get(expected['id'])
        if not isinstance(entry, dict):
            errors.append(Issue('ASSET_UNRESOLVED', ptr + '/id', 'Asset is not in the trusted registry.', expected['id']))
            continue
        if entry.get('approved') is not True:
            errors.append(Issue('ASSET_NOT_APPROVED', ptr, 'Registry has no asset-use approval.'))
        if entry.get('sha256') != expected['sha256']:
            errors.append(Issue('ASSET_HASH_MISMATCH', ptr + '/sha256', 'Plan and registry hashes differ.', entry.get('sha256'), expected['sha256']))
            continue
        try:
            path = Path(entry.get('path', ''))
            if not path.is_absolute() or not path.is_file():
                raise FileNotFoundError('Registry path must resolve to an existing absolute file.')
            path = path.resolve(strict=True)
            before = file_hash(path)
            if before != expected['sha256']:
                errors.append(Issue('ASSET_HASH_MISMATCH', ptr + '/sha256', 'Media bytes differ from the approved hash.', expected['sha256'], before))
                continue
            actual = probe(path, expected['kind'])
            if file_hash(path) != before:
                errors.append(Issue('ASSET_CHANGED_DURING_CHECK', ptr, 'Media changed during probing.'))
                continue
            errors.extend(normalization_issues(actual, ptr, fps))
            for key in ['kind', 'width_px', 'height_px', 'has_audio']:
                if actual.get(key) != expected[key]:
                    errors.append(Issue('MEDIA_METADATA', ptr + '/' + key, 'Decoded media disagrees with plan.', expected[key], actual.get(key)))
            duration = actual.get('duration_frames')
            if expected['kind'] == 'audio':
                rate = actual.get('audio_sample_rate') or 1
                duration = actual.get('audio_samples', 0) * fps // rate
            if duration != expected['duration_frames']:
                errors.append(Issue('MEDIA_METADATA', ptr + '/duration_frames', 'Usable media duration differs.', expected['duration_frames'], duration))
            if expected['id'] == plan['clip']['source_asset_id']:
                required = max(s['source_out_frame'] for s in plan['render']['segments']) * (48000 // fps)
                if actual.get('audio_samples', 0) < required:
                    errors.append(Issue('AUDIO_EXHAUSTED', ptr, 'Source dialogue samples do not cover the kept video.'))
            if expected['has_audio'] and (expected['id'] == plan['clip']['source_asset_id'] or expected['kind'] == 'audio'):
                evidence = entry.get('normalization', {})
                if (evidence.get('recipe') != 'cre_audio_minus16_v1' or evidence.get('sha256') != before
                        or type(evidence.get('integrated_lufs_milli')) is not int
                        or abs(evidence['integrated_lufs_milli'] + 16000) > 250
                        or type(evidence.get('true_peak_mdbtp')) is not int or evidence['true_peak_mdbtp'] > -1000
                        or not evidence.get('evidence_id')):
                    errors.append(Issue('NORMALIZATION_EVIDENCE_REQUIRED', ptr, 'Trusted hash-bound loudness evidence is missing or incompatible.'))
            resolved.append({'id': expected['id'], 'path': str(path), 'sha256': before,
                             'metadata': actual, 'normalization': entry.get('normalization'),
                             'approval_id': entry.get('approval_id')})
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(Issue('MEDIA_PROBE_FAILED', ptr, str(exc)))
    return errors, resolved
