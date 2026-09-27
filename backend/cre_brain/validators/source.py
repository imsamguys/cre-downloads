import math
import unicodedata
from .report import Issue


def token(text):
    # Only punctuation at token edges and case are normalized. Apostrophes inside
    # words, digits and negations remain significant.
    text = text.casefold()
    while text and unicodedata.category(text[0]).startswith('P'): text = text[1:]
    while text and unicodedata.category(text[-1]).startswith('P'): text = text[:-1]
    return text


def validate_source(plan, analysis):
    errors = []
    src = next(a for a in plan['render']['assets'] if a['id'] == plan['clip']['source_asset_id'])
    if not isinstance(analysis, dict) or analysis.get('version') != '1.0.0':
        return [Issue('SOURCE_ANALYSIS_REQUIRED', '/source_analysis', 'Provide trusted source analysis v1.')]
    for key, expected in [('source_asset_id', src['id']), ('source_sha256', src['sha256']),
                          ('fps', plan['render']['timebase']['fps']), ('duration_frames', src['duration_frames']),
                          ('verified', True)]:
        if analysis.get(key) != expected:
            errors.append(Issue('SOURCE_ANALYSIS_MISMATCH', '/source_analysis/' + key,
                                'Source analysis must bind this exact normalized source.', expected, analysis.get(key)))
    if not analysis.get('alignment_id'):
        errors.append(Issue('SOURCE_ANALYSIS_REQUIRED', '/source_analysis/alignment_id', 'Versioned alignment evidence is required.'))
    words = analysis.get('words')
    if not isinstance(words, list) or not words or len(words) > 20000:
        return errors + [Issue('SOURCE_ANALYSIS_INVALID', '/source_analysis/words', 'Expected a bounded complete word alignment.')]
    previous = 0
    for i, w in enumerate(words):
        if (not isinstance(w, dict) or not isinstance(w.get('text'), str) or not w['text']
                or type(w.get('source_start_frame')) is not int or type(w.get('source_end_frame')) is not int
                or not previous <= w['source_start_frame'] < w['source_end_frame'] <= src['duration_frames']):
            return errors + [Issue('SOURCE_ANALYSIS_INVALID', f'/source_analysis/words/{i}', 'Word bounds must be positive, ordered and nonoverlapping.')]
        previous = w['source_end_frame']
    segments = plan['render']['segments']; mapped = []
    for i, w in enumerate(words):
        lo, hi = w['source_start_frame'], w['source_end_frame']
        intersects = [s for s in segments if s['source_in_frame'] < hi and lo < s['source_out_frame']]
        contained = [s for s in intersects if s['source_in_frame'] <= lo and hi <= s['source_out_frame']]
        if intersects and not contained:
            errors.append(Issue('SOURCE_WORD_TRUNCATED', '/render/segments', 'A kept boundary truncates a source word.', w['text']))
        if not intersects and segments[0]['source_in_frame'] <= lo < segments[-1]['source_out_frame']:
            errors.append(Issue('SOURCE_WORD_REMOVED', '/render/segments', 'This profile only removes internal silence; an internal spoken word was removed.', w['text']))
        if contained:
            s = contained[0]; offset = s['output_start_frame'] - s['source_in_frame']
            mapped.append((token(w['text']), lo + offset, hi + offset))
    actual = [(token(w['text']), w['start_frame'], w['end_frame'], f'/render/captions/{ci}/words/{wi}')
              for ci, c in enumerate(plan['render']['captions']) for wi, w in enumerate(c['words'])]
    if len(mapped) != len(actual):
        errors.append(Issue('CAPTION_WORD_COUNT', '/render/captions', 'Captions must include every kept word exactly once.', len(mapped), len(actual)))
    for expected, word in zip(mapped, actual):
        if word[0] != expected[0]:
            errors.append(Issue('CAPTION_TRANSCRIPT_MISMATCH', word[3] + '/text', 'Caption changes the trusted spoken wording.', expected[0], word[0]))
        if max(abs(expected[1] - word[1]), abs(expected[2] - word[2])) > 2:
            errors.append(Issue('CAPTION_ALIGNMENT', word[3], 'Caption timing differs by more than two frames.', list(expected[1:]), list(word[1:3])))
    guard = math.ceil(60 * plan['render']['timebase']['fps'] / 1000)
    for si, s in enumerate(segments):
        kept = [w for w in words if s['source_in_frame'] <= w['source_start_frame'] and w['source_end_frame'] <= s['source_out_frame']]
        if not kept:
            errors.append(Issue('SOURCE_EMPTY_SEGMENT', f'/render/segments/{si}', 'A retained podcast segment must contain speech.'))
        elif kept[0]['source_start_frame'] - s['source_in_frame'] < guard or s['source_out_frame'] - kept[-1]['source_end_frame'] < guard:
            errors.append(Issue('CUT_GUARD', f'/render/segments/{si}', 'Retain at least 60 ms around boundary words after quantization.'))
    safe = analysis.get('safe_silence_ranges')
    if not isinstance(safe, list) or len(safe) > 20000:
        return errors + [Issue('SILENCE_EVIDENCE_REQUIRED', '/source_analysis/safe_silence_ranges', 'Supply trusted VAD/breath-reviewed silence ranges (empty if no internal cuts).')]
    for i, interval in enumerate(safe):
        if (not isinstance(interval, list) or len(interval) != 2 or any(type(x) is not int for x in interval)
                or not 0 <= interval[0] < interval[1] <= src['duration_frames']):
            return errors + [Issue('SOURCE_ANALYSIS_INVALID', f'/source_analysis/safe_silence_ranges/{i}', 'Invalid safe-silence interval.')]
    for si, (left, right) in enumerate(zip(segments, segments[1:])):
        a, b = left['source_out_frame'], right['source_in_frame']
        if not any(lo <= a < b <= hi and (hi-lo)*1000 >= 320*plan['render']['timebase']['fps'] for lo, hi in safe):
            errors.append(Issue('CUT_SILENCE_UNVERIFIED', f'/render/segments/{si+1}/source_in_frame', 'Internal omission is not inside a verified silence candidate of at least 320 ms.'))
    return errors
