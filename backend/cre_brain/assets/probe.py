"""Read-only full-decode probe. No subprocess, encoder, or network requests."""
from fractions import Fraction
import time
import av
from PIL import Image


PROBE_VERSION = 'pyav-full-decode-v1'


def probe_media(path, kind, *, max_frames=108000, timeout_seconds=60):
    started = time.monotonic()
    if kind == 'image':
        with Image.open(path) as im:
            if getattr(im, 'n_frames', 1) != 1:
                raise ValueError('Animated images are unsupported.')
            im.load()
            return {'kind': 'image', 'width_px': im.width, 'height_px': im.height,
                    'duration_frames': None, 'has_audio': False}
    with av.open(str(path), mode='r', options={'protocol_whitelist': 'file'}) as container:
        videos = list(container.streams.video)
        audios = list(container.streams.audio)
        if len(videos) != (1 if kind == 'video' else 0) or len(audios) > 1:
            raise ValueError('Expected one video (for video kind) and at most one audio stream.')
        if kind == 'audio' and not audios:
            raise ValueError('Audio stream missing.')
        vs = videos[0] if videos else None
        aus = audios[0] if audios else None
        count = samples = 0
        video_start = audio_start = previous_video = None
        audio_end = None
        video_step = None
        cfr = audio_contiguous = True
        dims = None
        for frame in container.decode():
            if time.monotonic() - started > timeout_seconds:
                raise ValueError('Probe time budget exceeded.')
            if frame.pts is None or frame.time_base is None:
                raise ValueError('Missing decoded timestamps.')
            stamp = Fraction(frame.pts) * frame.time_base
            if isinstance(frame, av.VideoFrame):
                if any('DISPLAYMATRIX' in str(side.type).upper() for side in frame.side_data):
                    raise ValueError('Display rotation requires normalization before ingestion.')
                if count >= max_frames:
                    raise ValueError('Probe frame budget exceeded.')
                if dims is None: dims = (frame.width, frame.height)
                if dims != (frame.width, frame.height):
                    raise ValueError('Midstream dimensions changed.')
                if video_start is None: video_start = stamp
                if previous_video is not None:
                    step = stamp - previous_video
                    if video_step is None: video_step = step
                    if step <= 0 or step != video_step: cfr = False
                previous_video = stamp
                count += 1
            elif isinstance(frame, av.AudioFrame):
                if aus is None or frame.sample_rate != aus.codec_context.sample_rate or len(frame.layout.channels) != aus.codec_context.channels:
                    raise ValueError('Midstream audio format changed.')
                if audio_start is None: audio_start = stamp
                if audio_end is not None and abs(stamp - audio_end) > Fraction(1, frame.sample_rate):
                    audio_contiguous = False
                audio_end = stamp + Fraction(frame.samples, frame.sample_rate)
                samples += frame.samples
                if samples > 48000 * 3600 * 4:
                    raise ValueError('Probe sample budget exceeded.')
        if vs and (count < 2 or video_step is None or video_step <= 0):
            raise ValueError('Insufficient video frames to verify CFR.')
        rate = (1 / video_step) if vs else None
        sar = Fraction(vs.sample_aspect_ratio) if vs and vs.sample_aspect_ratio else None
        rotation = int(vs.metadata.get('rotate', '0')) if vs else 0
        if vs:
            for side in vs.side_data if hasattr(vs, 'side_data') else []:
                if 'DISPLAYMATRIX' in str(side.type).upper():
                    raise ValueError('Display matrix requires normalization before ingestion.')
        return {
            'kind': kind, 'width_px': dims[0] if dims else None, 'height_px': dims[1] if dims else None,
            'duration_frames': count if vs else None, 'has_audio': bool(aus),
            'fps_num': rate.numerator if rate else None, 'fps_den': rate.denominator if rate else None,
            'cfr': cfr if vs else None, 'video_start_zero': video_start == 0 if vs else None,
            'square_pixels': (sar == 1 if sar is not None else None) if vs else None, 'rotation_degrees': rotation,
            'color_primaries': int(vs.codec_context.color_primaries) if vs else None,
            'color_transfer': int(vs.codec_context.color_trc) if vs else None,
            'color_space': int(vs.codec_context.colorspace) if vs else None,
            'audio_sample_rate': aus.codec_context.sample_rate if aus else None,
            'audio_channels': aus.codec_context.channels if aus else None,
            'audio_samples': samples if aus else None,
            'audio_start_zero': audio_start == 0 if aus else None,
            'audio_contiguous': audio_contiguous if aus else None,
            'probe_version': PROBE_VERSION, 'pyav_version': av.__version__}
