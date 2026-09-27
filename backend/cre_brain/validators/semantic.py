"""Offline v1 semantic/profile checks. Schema validation must run first."""
from .report import Issue

def validate_semantics(plan, profile):
    errors = []
    locations = {}
    for group, items in plan['render'].items():
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict) and 'id' in item:
                    locations[item['id']] = f'/render/{group}/{index}'
    locations.update({plan['render']['music']['id']: '/render/music'} if plan['render']['music'] else {})

    def check(condition, message, code, path):
        if not condition:
            errors.append(Issue(code, path, message))
    r = plan['render']
    h = profile['hard_limits']
    out = r['output']
    fps = r['timebase']['fps']
    total = out['duration_frames']
    check(fps == h['fps'], 'profile: fps must be 30', 'PROFILE_LIMIT', '/render/timebase/fps')
    check((out['width_px'], out['height_px']) == (1080, 1920), 'profile: output must be 1080x1920', 'PROFILE_LIMIT', '/render/output')
    check(h['min_duration_frames'] <= total <= h['max_duration_frames'], 'profile: output must be 20–30 seconds', 'PROFILE_LIMIT', '/render/output')
    ids = set()
    groups = ['assets', 'segments', 'reframes', 'punch_ins', 'captions', 'b_roll', 'sfx', 'transitions', 'overlays']
    for item in [x for group in groups for x in r[group]] + ([r['music']] if r['music'] else []):
        check(item['id'] not in ids, f"duplicate ID: {item['id']}", 'DUPLICATE_ID', locations.get(item['id'], '/render') + '/id')
        ids.add(item['id'])
    assets = {a['id']: a for a in r['assets']}
    source = assets.get(plan['clip']['source_asset_id'])
    check(source is not None and source['kind'] == 'video' and source['has_audio'], 'source must resolve to video with dialogue audio', 'ASSET_METADATA', '/render/assets')
    for a in assets.values():
        if a['kind'] == 'audio':
            check(a['width_px'] is None and a['height_px'] is None and (a['duration_frames'] is not None) and a['has_audio'], f"audio metadata: {a['id']}", 'ASSET_METADATA', locations.get(a['id'], '/render/assets') + '')
        elif a['kind'] == 'image':
            check(a['width_px'] is not None and a['height_px'] is not None and (a['duration_frames'] is None) and (not a['has_audio']), f"image metadata: {a['id']}", 'ASSET_METADATA', locations.get(a['id'], '/render/assets') + '')
        else:
            check(a['width_px'] is not None and a['height_px'] is not None and (a['duration_frames'] is not None), f"video metadata: {a['id']}", 'ASSET_METADATA', locations.get(a['id'], '/render/assets') + '')
    if source is None or source['duration_frames'] is None or source['width_px'] is None or (source['height_px'] is None):
        return errors
    cursor = 0
    previous_source_end = None
    check(len(r['segments']) <= h['max_segments'], 'too many segments', 'PROFILE_LIMIT', '/render/segments')
    for s in r['segments']:
        a, b = (s['source_in_frame'], s['source_out_frame'])
        length = b - a
        check(0 <= a < b <= source['duration_frames'], f"invalid source interval: {s['id']}", 'SOURCE_RANGE', locations.get(s['id'], '/render/segments') + '/source_out_frame')
        check(length >= h['min_segment_frames'], f"segment too short: {s['id']}", 'PROFILE_LIMIT', locations.get(s['id'], '/render/segments') + '')
        check(s['output_start_frame'] == cursor, f"gap/overlap or unordered cut map: {s['id']}", 'TIMELINE_GAP', locations.get(s['id'], '/render/segments') + '/output_start_frame')
        if previous_source_end is not None:
            check(a > previous_source_end, f"source reorder/overlap or unconsolidated contiguous segments: {s['id']}", 'SOURCE_ORDER', locations.get(s['id'], '/render/segments') + '/source_in_frame')
        previous_source_end = b
        cursor += length
    check(cursor == total, 'segment durations must sum to output duration', 'DURATION_MISMATCH', '/render/output/duration_frames')
    segments = {s['id']: s for s in r['segments']}
    seen = set()
    for f in r['reframes']:
        check(f['segment_id'] in segments and f['segment_id'] not in seen, f"invalid/duplicate reframe mapping: {f['id']}", 'FRAMING_INVALID', locations.get(f['id'], '/render/reframes') + '')
        seen.add(f['segment_id'])
        c = f['crop']
        check(c['x_px'] + c['width_px'] <= source['width_px'] and c['y_px'] + c['height_px'] <= source['height_px'], f"crop out of source: {f['id']}", 'FRAMING_INVALID', locations.get(f['id'], '/render/reframes') + '')
        check(c['width_px'] * out['height_px'] == c['height_px'] * out['width_px'], f"crop aspect mismatch: {f['id']}", 'FRAMING_INVALID', locations.get(f['id'], '/render/reframes') + '')
        check(all((c[k] % 2 == 0 for k in c)), f"crop must be even for chroma alignment: {f['id']}", 'FRAMING_INVALID', locations.get(f['id'], '/render/reframes') + '')
    check(seen == set(segments), 'exactly one reframe required per segment', 'FRAMING_INVALID', '/render/reframes')
    check([f['segment_id'] for f in r['reframes']] == [s['id'] for s in r['segments']], 'reframes must follow segment order', 'FRAMING_INVALID', '/render/reframes')

    def inside_segment(start, end):
        return any((s['output_start_frame'] <= start < end <= s['output_start_frame'] + s['source_out_frame'] - s['source_in_frame'] for s in r['segments']))

    def intervals(items, label, allow_overlap=False):
        prev_start = -1
        prev_end = -1
        for e in items:
            a, b = (e['start_frame'], e['end_frame'])
            check(0 <= a < b <= total, f"invalid output interval: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/output') + '')
            check(a >= prev_start, f'unordered {label}', 'PROFILE_LIMIT', '/render')
            if not allow_overlap:
                check(a >= prev_end, f'overlapping {label}', 'EFFECT_CONFLICT', '/render')
            prev_start = a
            prev_end = b
    for name in ['punch_ins', 'captions', 'b_roll', 'sfx', 'transitions']:
        intervals(r[name], name)
    intervals(r['overlays'], 'overlays', allow_overlap=True)
    if r['music']:
        intervals([r['music']], 'music')

    def spacing(items, minimum, label):
        check(all((b['start_frame'] - a['start_frame'] >= minimum for a, b in zip(items, items[1:]))), f'{label} starts too close', 'PROFILE_LIMIT', '/render')

    def envelope(e, field, value):
        keys = e[field]
        check(keys[0]['frame'] == e['start_frame'] and keys[-1]['frame'] == e['end_frame'], f"envelope must cover event boundaries: {e['id']}", 'ENVELOPE_INVALID', locations.get(e['id'], '/render') + '')
        check(all((e['start_frame'] <= k['frame'] <= e['end_frame'] for k in keys)), f"key outside event: {e['id']}", 'ENVELOPE_INVALID', locations.get(e['id'], '/render') + '')
        check(all((a['frame'] < b['frame'] for a, b in zip(keys, keys[1:]))), f"keys not strictly increasing: {e['id']}", 'ENVELOPE_INVALID', locations.get(e['id'], '/render') + '')
    check(len(r['punch_ins']) <= h['max_punch_ins'], 'too many punch-ins', 'PROFILE_LIMIT', '/render/punch_ins')
    spacing(r['punch_ins'], h['min_punch_start_spacing_frames'], 'punch-in')
    for e in r['punch_ins']:
        check(h['min_punch_frames'] <= e['end_frame'] - e['start_frame'] <= h['max_punch_frames'], f"punch duration: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
        check(inside_segment(e['start_frame'], e['end_frame']), f"punch crosses a cut: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
        envelope(e, 'keyframes', 'scale_milli')
        keys = e['keyframes']
        check(keys[0]['scale_milli'] == keys[-1]['scale_milli'] == 1000, f"punch must return to neutral: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
        check(max((k['scale_milli'] for k in keys)) <= h['max_zoom_milli'], f"zoom too large: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
        check(max((k['scale_milli'] for k in keys)) > 1000, f"no-op zoom: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
        peak = max((k['scale_milli'] for k in keys))
        falling = False
        for a, b in zip(keys, keys[1:]):
            if b['scale_milli'] < a['scale_milli']:
                falling = True
            check(not (falling and b['scale_milli'] > a['scale_milli']), f"oscillating zoom: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
            if a['scale_milli'] != b['scale_milli']:
                check(b['frame'] - a['frame'] >= h['min_zoom_ramp_frames'], f"zoom ramp too short: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
        check(any((a['scale_milli'] == b['scale_milli'] == peak and b['frame'] > a['frame'] for a, b in zip(keys, keys[1:]))), f"zoom needs a hold at peak: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/punch_ins') + '')
    word_count = 0
    emphasized = 0
    check(bool(r['captions']), 'profile requires captions', 'CAPTION_INVALID', '/render/captions')
    for e in r['captions']:
        duration = e['end_frame'] - e['start_frame']
        words = e['words']
        min_caption_frames = 1 if len(words) <= 3 else h['min_caption_frames']
        check(min_caption_frames <= duration <= h['max_caption_frames'], f"caption duration: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
        check(len(words) <= h['max_words_per_caption'], f"caption word count: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
        chars = len(' '.join((w['text'] for w in words)))
        check(chars <= h['max_caption_characters'], f"caption too long: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
        caption_cps = max(120, h['max_caption_characters_per_second']) if len(words) <= 3 else h['max_caption_characters_per_second']
        check(chars * fps <= caption_cps * duration, f"caption reading speed: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
        previous = e['start_frame']
        for w in words:
            check(previous <= w['start_frame'] < w['end_frame'] <= e['end_frame'], f"caption word bounds/order: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
            check(not any((c.isspace() or ord(c) < 32 for c in w['text'])), f"word must be one token: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
            check(inside_segment(w['start_frame'], w['end_frame']), f"word crosses cut: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
            previous = w['end_frame']
        check(e['start_frame'] == words[0]['start_frame'] and e['end_frame'] == words[-1]['end_frame'], f"cue must match first/last word bounds: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
        n = sum((w['emphasis'] for w in words))
        word_count += len(words)
        emphasized += n
        check(n <= h['max_emphasized_words_per_caption'], f"excessive cue emphasis: {e['id']}", 'CAPTION_INVALID', locations.get(e['id'], '/render/captions') + '')
    check(emphasized * 1000 <= word_count * h['max_emphasized_word_ratio_milli'], 'excessive overall emphasis', 'CAPTION_INVALID', '/render/captions')
    used = {plan['clip']['source_asset_id']}

    def asset_window(e, kinds):
        asset = assets.get(e['asset_id'])
        used.add(e['asset_id'])
        check(asset is not None and asset['kind'] in kinds, f"unresolved or wrong asset type: {e['id']}", 'ASSET_REFERENCE', locations.get(e['id'], '/render/assets') + '/asset_id')
        if asset and asset['kind'] in kinds and (asset['kind'] != 'image'):
            check(asset['duration_frames'] is not None and e['source_in_frame'] + (e['end_frame'] - e['start_frame']) <= asset['duration_frames'], f"asset exhausted: {e['id']}", 'ASSET_WINDOW', locations.get(e['id'], '/render/assets') + '/source_in_frame')
        return asset
    check(len(r['b_roll']) <= h['max_b_roll_events'], 'too much B-roll', 'PROFILE_LIMIT', '/render/b_roll')
    spacing(r['b_roll'], h['min_b_roll_start_spacing_frames'], 'B-roll')
    check(sum((e['end_frame'] - e['start_frame'] for e in r['b_roll'])) * 1000 <= total * h['max_b_roll_coverage_milli'], 'B-roll coverage too high', 'PROFILE_LIMIT', '/render/b_roll')
    for e in r['b_roll']:
        a = asset_window(e, {'video', 'image'})
        check(h['min_b_roll_frames'] <= e['end_frame'] - e['start_frame'] <= h['max_b_roll_frames'], f"B-roll duration: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/b_roll') + '')
        check(e['start_frame'] >= 45 and e['end_frame'] <= total - 30, f"B-roll covers hook/payoff: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/b_roll') + '')
        if a:
            check(a['kind'] == 'image' and e['playback'] == 'still' and (e['source_in_frame'] == 0) or (a['kind'] == 'video' and e['playback'] == 'normal'), f"B-roll playback mode: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/b_roll') + '')
    if r['music']:
        e = r['music']
        asset_window(e, {'audio'})
        envelope(e, 'gain_keyframes', 'gain_db')
        keys = e['gain_keyframes']
        check(e['start_frame'] == 0 and e['end_frame'] == total, 'music must cover full clip', 'PROFILE_LIMIT', '/render/music')
        check(len(keys) >= 4, 'music must have fade-in, hold and fade-out keys', 'ENVELOPE_INVALID', '/render')
        check(keys[0]['gain_db'] == keys[-1]['gain_db'] == -80, 'music endpoints must mute', 'PROFILE_LIMIT', '/render/music')
        check(all((k['gain_db'] <= h['max_music_gain_db'] for k in keys)), 'music too loud', 'PROFILE_LIMIT', '/render/music')
        check(all((k['gain_db'] > -80 for k in keys[1:-1])), 'interior music keys must be audible', 'ENVELOPE_INVALID', '/render')
        check(h['min_music_fade_frames'] <= keys[1]['frame'] <= h['max_music_fade_frames'], 'music fade-in duration', 'PROFILE_LIMIT', '/render/music')
        check(h['min_music_fade_frames'] <= total - keys[-2]['frame'] <= h['max_music_fade_frames'], 'music fade-out duration', 'PROFILE_LIMIT', '/render/music')
    check(len(r['sfx']) <= h['max_sfx_events'], 'too many SFX', 'PROFILE_LIMIT', '/render/sfx')
    spacing(r['sfx'], h['min_sfx_start_spacing_frames'], 'SFX')
    for e in r['sfx']:
        asset_window(e, {'audio'})
        duration = e['end_frame'] - e['start_frame']
        check(duration <= h['max_sfx_duration_frames'] and e['gain_db'] <= h['max_sfx_gain_db'], f"SFX duration/gain: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/sfx') + '')
        check(e['fade_in_frames'] >= h['min_sfx_fade_frames'] and e['fade_out_frames'] >= h['min_sfx_fade_frames'] and (e['fade_in_frames'] + e['fade_out_frames'] <= duration), f"SFX fades: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/sfx') + '')
    check(len(r['transitions']) <= h['max_transitions'], 'podcast profile prohibits transitions', 'CAPABILITY_UNSUPPORTED', '/render/transitions')
    check(len(r['overlays']) <= h['max_overlays'], 'too many overlays', 'PROFILE_LIMIT', '/render/overlays')
    for e in r['overlays']:
        check(h['min_overlay_frames'] <= e['end_frame'] - e['start_frame'] <= h['max_overlay_frames'] and len(e['text']) <= h['max_overlay_characters'], f"overlay duration/text: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/overlays') + '')
        check(not any((ord(c) < 32 for c in e['text'])), f"overlay control character: {e['id']}", 'PROFILE_LIMIT', locations.get(e['id'], '/render/overlays') + '')
    # Top hook and bottom CTA are persistent framing elements. They may coexist
    # with inserts; timed visual edits still may not overlap one another.
    visual = sorted(r['punch_ins'] + r['b_roll'], key=lambda e: e['start_frame'])
    intervals(visual, 'visual decorations')
    decorations = sorted(visual + r['sfx'], key=lambda e: e['start_frame'])
    check(len(decorations) <= h['max_decoration_events'], 'too many decorations overall', 'PROFILE_LIMIT', '/render')
    broll_starts = {event['start_frame'] for event in r['b_roll']}
    spacing([event for event in decorations
             if not (event in r['sfx'] and event['start_frame'] in broll_starts)],
            h['min_decoration_start_spacing_frames'], 'decoration')
    check(used == set(assets), 'asset manifest must contain exactly referenced assets', 'ASSET_METADATA', '/render/assets')
    return errors
