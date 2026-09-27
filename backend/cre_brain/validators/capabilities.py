from .report import Issue


def required_operations(plan):
    r = plan['render']
    ops = {'hard_cuts_v1', 'static_crop_v1', 'dialogue_splice_fade_v1', 'captions_word_color_v1'}
    for field, name in [('punch_ins', 'center_zoom_linear_v1'), ('b_roll', 'b_roll_replace_v1'),
                        ('music', 'music_amplitude_envelope_v1'), ('sfx', 'sfx_linear_fades_v1'),
                        ('overlays', 'top_label_v1')]:
        if r[field]: ops.add(name)
    return sorted(ops)


def validate_capabilities(plan, capabilities, lock):
    errors = []
    if not isinstance(capabilities, dict) or capabilities.get('version') != '1.0.0':
        return [Issue('CAPABILITIES_REQUIRED', '/capabilities', 'A trusted renderer capability manifest v1 is required.')]
    profile_files = {
        'PODCAST_HIGH_RETENTION_V1': 'profile.json',
        'PODCAST_HIGH_RETENTION_FAST_SPEECH_V1': 'profile-fast-speech.json',
    }
    profile_file = profile_files.get(plan['profile']['id'])
    if profile_file is None:
        return [Issue('PROFILE_UNKNOWN', '/profile', 'Renderer cannot bind an unknown profile.')]
    for key, expected in [('schema_sha256', lock['schema.json']), ('profile_sha256', lock[profile_file])]:
        if capabilities.get(key) != expected:
            errors.append(Issue('CAPABILITY_CONTRACT_MISMATCH', '/capabilities/' + key, 'Renderer is not bound to this exact contract.', expected, capabilities.get(key)))
    for key in ['renderer_id', 'renderer_version', 'scaler_version', 'audio_engine_version', 'encoder_version']:
        if not isinstance(capabilities.get(key), str) or not capabilities[key].strip():
            errors.append(Issue('CAPABILITY_UNBOUND', '/capabilities/' + key, 'Pin an explicit implementation version.'))
    supported = capabilities.get('operations', [])
    for op in required_operations(plan):
        if op not in supported:
            errors.append(Issue('CAPABILITY_UNSUPPORTED', '/capabilities/operations', 'Requested operation is unsupported.', op))
    if plan['clip']['language'] not in capabilities.get('languages', []):
        errors.append(Issue('LANGUAGE_UNSUPPORTED', '/clip/language', 'Renderer has not declared support for this language.'))
    budget = capabilities.get('max_output_pixel_frames')
    out = plan['render']['output']; cost = out['width_px'] * out['height_px'] * out['duration_frames']
    if type(budget) is not int or cost > budget:
        errors.append(Issue('RESOURCE_BUDGET', '/render/output', 'Output exceeds the declared renderer pixel-frame budget.', budget, cost))
    if not isinstance(capabilities.get('font'), dict):
        errors.append(Issue('FONT_REQUIRED', '/capabilities/font', 'Supply the approved Inter Bold font path and hash.'))
    return errors
