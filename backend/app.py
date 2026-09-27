import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
import subprocess

try:
    import requests
except ImportError:
    requests = None
import tempfile
import json
import random
import base64
import re
import os
from cre_openreel_ui import render_pipeline, open_pool, render_template_library, bridge_for
from cre_format3 import FORMAT3_NAV, render_caption_word_templates, render_format3_page
from cre_caption_styles import render_caption_library
from cre_security import ALLOWED_EXTENSIONS, UploadRejected, secure_save_upload
from cre_legal import LEGAL_NAV, render_legal_page

# -------------------------------------------------
# Beat detection needs numpy + scipy (mechanical
# signal processing only — no AI/ML). Imported here
# as optional so the rest of the app still runs even
# if they aren't installed yet; the Beat Templates
# page just shows an error telling the user to
# `pip install numpy scipy` instead of crashing.
# -------------------------------------------------

try:
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import stft, find_peaks

    BEAT_DETECTION_AVAILABLE = True
except ImportError:
    BEAT_DETECTION_AVAILABLE = False

# -------------------------------------------------
# Auto Captions need real speech-to-text — there is
# no mechanical way to know the words/timing in an
# audio track. This is the one deliberate exception
# to CRE's "no AI" rule, and it's opt-in per render
# and runs fully locally (faster-whisper, CPU, no
# cloud calls). Guarded import so the rest of the
# app still runs if it isn't installed yet.
# -------------------------------------------------

try:
    from faster_whisper import WhisperModel

    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

# -------------------------------------------------
# Text-fit measurement for burned-in captions/text.
# PIL gives pixel-accurate glyph widths for the same
# .ttf files drawtext uses, so we can shrink/wrap text
# to actually fit the frame instead of guessing. Purely
# measurement — no AI, and the app still runs (falling
# back to a rough per-character estimate) if Pillow
# isn't installed.
# -------------------------------------------------

try:
    from PIL import ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

st.set_page_config(
    page_title="CRE — Clipping Revenue Engine",
    page_icon="🎬",
    layout="wide",
)

APP_DIR = Path(os.environ.get("CRE_DATA_DIR", str(Path(__file__).parent))).resolve()
ASSETS_DIR = APP_DIR / "assets"

# -----------------------------------------------------------------
# Remotion bridge (optional, experimental). Only used if the user
# flips the "Render engine" toggle on a render page AND has both
# `remotion_client.py` dropped next to this file AND the two local
# servers running (remotion-service on :3001, the asset file server
# on :8899 -- see serve_assets.py). Importing this never breaks
# ffmpeg rendering if it's missing or the servers aren't up; that
# path is only touched if the toggle is explicitly set to Remotion.
# -----------------------------------------------------------------

try:
    from remotion_client import render_local, RemotionBridgeError
    REMOTION_CLIENT_AVAILABLE = True
except ImportError:
    REMOTION_CLIENT_AVAILABLE = False

ASSET_SERVER_URL = "http://localhost:8899"


def asset_url(path) -> str:
    """Turn a local path under ASSETS_DIR into an http://localhost:8899/...
    URL the asset file server (serve_assets.py) can serve, so Remotion's
    renderer -- which fetches over HTTP, not raw filesystem paths -- can
    reach it. Raises if the path isn't under ASSETS_DIR."""
    rel = Path(path).resolve().relative_to(ASSETS_DIR.resolve())
    return f"{ASSET_SERVER_URL}/{rel.as_posix()}"


def is_default_layout(manual_layout, mode) -> bool:
    """True if this template's manual_layout is identical to CRE's own
    built-in defaults for this mode -- i.e. nothing was dragged or
    resized on the Crop Presets page. load_format1_templates() always
    backfills manual_layout to a full dict (never None), so we compare
    against _DEFAULT_MANUAL_LAYOUT directly. Only default layouts are
    safe to send to the Remotion scaffold, whose ClipTemplate.tsx has
    the same fixed positions hardcoded and nothing else (no arbitrary
    x/y/w/h support)."""
    default = _DEFAULT_MANUAL_LAYOUT[mode]
    for key, box in default.items():
        if key not in manual_layout:
            return False
        this_box = manual_layout[key]
        for field in ("x_pct", "y_pct", "w_pct", "h_pct"):
            if this_box.get(field) != box.get(field):
                return False
    return True
BEAT_TEMPLATES_DIR = APP_DIR / "beat_templates"
CAPTION_TEMPLATES_DIR = APP_DIR / "caption_templates"
FORMAT1_TEMPLATES_FILE = APP_DIR / "format1_templates.json"

# Trending-style caption fonts CRE will try, in order,
# falling back to the next if a font isn't installed.
# All three ship with Windows by default.
CAPTION_FONT_CANDIDATES = [
    "C:/Windows/Fonts/impact.ttf",       # bold condensed — "QUICK" look
    "C:/Windows/Fonts/arialbd.ttf",      # bold sans fallback
    "C:/Windows/Fonts/segoeuib.ttf",     # last-resort fallback
]

# -------------------------------------------------
# Format 2 asset pools (fixed — BMW / LiveMap / a
# shared music library). Format 1's clip pools are
# NOT here — they're dynamic, one per Template, and
# live under Format1_TEMPLATES_FILE instead. Its
# Music pool is still fixed, so it stays a bucket.
# -------------------------------------------------

BUCKETS = {
    "Format1Music": Path("Format_1") / "Music",
    "BMW": Path("Format_2") / "BMW",
    "LiveMap": Path("Format_2") / "LiveMap",
    "Format2Music": Path("Format_2") / "Music_Library",
}

VIDEO_TYPES = ["mp4", "mov", "mkv", "webm", "avi"]

AUDIO_TYPES = [
    "mp3",
    "wav",
    "m4a",
    "aac",
    "flac",
    "ogg",
]

for bucket in BUCKETS.values():
    (ASSETS_DIR / bucket).mkdir(parents=True, exist_ok=True)

BEAT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


# -------------------------------------------------
# Format 1 Templates.
#
# A "template" is just a named clip category (Movie
# Clip, Meme, Streamer Clip, or anything custom) —
# each gets its own footage pool under Format_1/. All
# templates pair against the same shared Music pool.
# "Streamer Clip + Song" reuses the original
# Format_1/Streamer folder so nothing already uploaded
# there gets orphaned. Purely organizational file I/O —
# no AI involved in what gets created or used.
# -------------------------------------------------


# Two render modes a template can be:
#   "stack"            — two videos, cropped to half-height each
#                         and vstack'd (e.g. Streamer Clip + Song,
#                         where "Song" is real footage of the song
#                         playing — a music video / visualizer).
#   "background_audio"  — one clip, fullscreen, with a Song AUDIO
#                         file playing underneath instead of the
#                         clip's own audio (e.g. Movie Clip + Song,
#                         Meme + Song).
# Each template gets its OWN Clip pool and its OWN Song pool —
# nothing is shared between templates anymore.

MODE_STACK = "stack"
MODE_BACKGROUND_AUDIO = "background_audio"

MODE_LABELS = {
    MODE_STACK: "Video footage + song videos",
    MODE_BACKGROUND_AUDIO: "Video footage + audio tracks",
}

DEFAULT_FORMAT1_TEMPLATES = [
    {"name": "Streamer Clip + Song", "slug": "Streamer", "mode": MODE_STACK},
    {"name": "Movie Clip + Song", "slug": "MovieClip", "mode": MODE_BACKGROUND_AUDIO},
    {"name": "Meme + Song", "slug": "Meme", "mode": MODE_BACKGROUND_AUDIO},
]

_LEGACY_BACKGROUND_AUDIO_NAMES = {"Movie Clip + Song", "Meme + Song"}

# -------------------------------------------------
# Manual layout (Crop Presets page).
#
# Replaces the old CapCut-style "crop box" (zoom/pan
# sliders) with real drag-and-resize placement, like
# CapCut's actual timeline canvas: each layer (a video
# clip, or the on-screen text box) is a rectangle on
# the 1080x1920 stage, defined as percentages of the
# canvas so it stays resolution-independent:
#   x_pct, y_pct — top-left corner of the box
#   w_pct, h_pct — box size
# Dragging moves x_pct/y_pct; resizing changes w_pct/
# h_pct (and x_pct/y_pct, if resized from a top/left
# handle). Because these are just rectangles the user
# positions freely, gaps and overlaps are both possible
# and both intentional — closing a gap just means
# dragging two edges together, exactly like CapCut.
# -------------------------------------------------

_DEFAULT_MANUAL_LAYOUT = {
    MODE_STACK: {
        "top_video":    {"x_pct": 0, "y_pct": 0, "w_pct": 100, "h_pct": 50},
        "bottom_video": {"x_pct": 0, "y_pct": 50, "w_pct": 100, "h_pct": 50},
        "text":         {"x_pct": 10, "y_pct": 4, "w_pct": 80, "h_pct": 10},
    },
    MODE_BACKGROUND_AUDIO: {
        "clip_video": {"x_pct": 0, "y_pct": 0, "w_pct": 100, "h_pct": 100},
        "text":       {"x_pct": 10, "y_pct": 4, "w_pct": 80, "h_pct": 10},
    },
}

# Per-template adjustable style — this is the "manual tool" for
# fixing text overflow / reframing without touching code: set once
# per template on the Templates / Crop Presets pages, and it
# applies to every clip rendered from that template from then on.
#   font_scale     — multiplies the auto-fit font size (1.0 = normal
#                    size the auto-fit picks; <1 smaller, >1 bigger).
#                    Text still never overflows the frame — the
#                    auto-fit re-checks/re-wraps AFTER this scale is
#                    applied, so this is a style knob, not an escape
#                    hatch for overflow.
#   manual_layout  — the drag/resize placement described above, set
#                    on the Crop Presets page. Keyed by mode: stack
#                    mode has top_video/bottom_video/text, background
#                    _audio mode has clip_video/text.
DEFAULT_TEMPLATE_STYLE = {
    "font_scale": 1.0,
    "manual_layout": None,
}


def _backfill_template_style(template):
    style = template.get("style", {})
    merged = {**DEFAULT_TEMPLATE_STYLE, **style}

    mode = template.get("mode", MODE_STACK)
    default_layout = _DEFAULT_MANUAL_LAYOUT[mode]
    existing_layout = style.get("manual_layout") or {}
    # Merge one level deeper so an old/partial saved layout doesn't
    # lose whichever keys it doesn't have (e.g. upgrading from an
    # older version of this style dict, or a mode switch).
    merged["manual_layout"] = {
        key: {**default_layout[key], **existing_layout.get(key, {})}
        for key in default_layout
    }

    template["style"] = merged
    return template


def _backfill_template_mode(template):
    if "mode" not in template:
        template["mode"] = (
            MODE_BACKGROUND_AUDIO
            if template["name"] in _LEGACY_BACKGROUND_AUDIO_NAMES
            else MODE_STACK
        )
    return template


def load_format1_templates():
    if not FORMAT1_TEMPLATES_FILE.exists():
        FORMAT1_TEMPLATES_FILE.write_text(
            json.dumps(DEFAULT_FORMAT1_TEMPLATES, indent=2)
        )
        return [
            _backfill_template_style(_backfill_template_mode(dict(t)))
            for t in DEFAULT_FORMAT1_TEMPLATES
        ]

    try:
        templates = json.loads(FORMAT1_TEMPLATES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return [
            _backfill_template_style(_backfill_template_mode(dict(t)))
            for t in DEFAULT_FORMAT1_TEMPLATES
        ]

    templates = [_backfill_template_mode(t) for t in templates]
    templates = [_backfill_template_style(t) for t in templates]
    save_format1_templates(templates)
    return templates


def save_format1_templates(templates):
    FORMAT1_TEMPLATES_FILE.write_text(json.dumps(templates, indent=2))


def format1_template_folder(slug):
    if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9]{1,100}", slug):
        raise ValueError("Invalid template folder name")
    folder = ASSETS_DIR / Path("Format_1") / slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def format1_template_song_folder(slug):
    folder = format1_template_folder(slug) / "Song"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# -------------------------------------------------
# On-Screen Text pools (Format 1, all templates).
#
# Each template gets its own list of caption strings,
# stored as plain JSON — not files-per-caption, since
# these are short text entries the user types/pastes,
# one per line. At render time they're assigned to the
# batch mechanically by index (pairing #1 -> text #1,
# #2 -> text #2 ...), wrapping back to the start once
# the pool is exhausted — the exact same "assign then
# loop" pattern already used for Format 2's background
# music. No AI selection involved.
# -------------------------------------------------

def format1_template_text_path(slug):
    return format1_template_folder(slug) / "on_screen_text.json"


def load_format1_template_texts(slug):
    path = format1_template_text_path(slug)

    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_format1_template_texts(slug, texts):
    format1_template_text_path(slug).write_text(json.dumps(texts, indent=2))


def migrate_legacy_shared_music():
    """
    Format 1 used to have ONE shared 'Music footage' pool for every
    template. Now each template has its own Song pool. This runs
    once at startup and moves anything still sitting in the old
    shared folder into Streamer Clip + Song's new Song folder (that
    was the only template actually using it) — nothing gets lost,
    and once moved the old folder just stays empty going forward.
    """
    old_folder = ASSETS_DIR / BUCKETS["Format1Music"]

    if not old_folder.exists():
        return

    leftover = list_video_files(old_folder)

    if not leftover:
        return

    destination = format1_template_song_folder("Streamer")

    for file in leftover:
        target = destination / file.name
        if not target.exists():
            file.rename(target)


def ensure_format1_dirs():
    for template in load_format1_templates():
        format1_template_folder(template["slug"])
        format1_template_song_folder(template["slug"])


def slugify_template_name(name, existing_slugs):
    base = re.sub(r"[^A-Za-z0-9]+", "", name.title()) or "Template"
    slug = base
    n = 2
    while slug in existing_slugs:
        slug = f"{base}{n}"
        n += 1
    return slug




# -------------------------------------------------
# File pool helpers.
#
# list_video_files() / list_audio_files() work on any
# folder — used both for the fixed BUCKETS and for
# Format 1's dynamic per-template folders.
# get_files() / get_audio_files() are the BUCKETS-only
# convenience wrappers everything else already used.
# -------------------------------------------------

def list_video_files(folder):
    return sorted(
        [
            file
            for file in folder.iterdir()
            if file.is_file()
            and file.suffix.lower().replace(".", "") in VIDEO_TYPES
        ],
        key=lambda x: x.name.lower(),
    )


def list_audio_files(folder):
    return sorted(
        [
            file
            for file in folder.iterdir()
            if file.is_file()
            and file.suffix.lower().replace(".", "") in AUDIO_TYPES
        ],
        key=lambda x: x.name.lower(),
    )


# list_video_files()/list_audio_files() now exist, so it's safe to
# create Format 1's folders and run the one-time legacy-Music
# migration (both depend on them).
ensure_format1_dirs()
migrate_legacy_shared_music()


def get_files(bucket):
    return list_video_files(ASSETS_DIR / BUCKETS[bucket])


def get_audio_files(bucket):
    return list_audio_files(ASSETS_DIR / BUCKETS[bucket])


def count_files(bucket):
    return len(get_files(bucket))


def save_files(files, bucket):
    kind = "audio" if "Music" in bucket else "video"
    save_files_to_folder(files, ASSETS_DIR / BUCKETS[bucket], kind)


def save_files_to_folder(files, folder, kind=None):
    for uploaded in files:
        upload_kind = kind
        if upload_kind is None:
            suffix = Path(uploaded.name).suffix.lower()
            upload_kind = "video" if suffix in ALLOWED_EXTENSIONS["video"] else "audio"
        secure_save_upload(uploaded, folder, upload_kind)


def show_pool(bucket):
    if bucket == "Format2Music":
        files = get_audio_files(bucket)
        label = "Music tracks"
    else:
        files = get_files(bucket)
        label = "Clips"

    st.metric(label, len(files))

    if files:
        with st.expander(f"View {len(files)} files"):
            for index, file in enumerate(files, 1):
                st.write(f"{index}. `{file.name}`")

    return files


def show_pool_folder(folder, label="Clips"):
    files = list_video_files(folder)

    st.metric(label, len(files))

    if files:
        with st.expander(f"View {len(files)} files"):
            for index, file in enumerate(files, 1):
                st.write(f"{index}. `{file.name}`")

    return files


def make_pairs(first_files, second_files):
    pair_count = min(len(first_files), len(second_files))
    return [
        (first_files[i], second_files[i])
        for i in range(pair_count)
    ]


# -------------------------------------------------
# Beat detection (Format 2 only).
#
# Mechanical beat/onset detection via spectral flux +
# peak picking. This is signal processing on the raw
# waveform — NOT AI/ML, no scene understanding, no
# semantic matching. It just finds moments where the
# audio energy changes sharply (drum hits, transients)
# and treats those as candidate beat points.
#
# "Sparse" selection: peaks must be at least
# `min_spacing` seconds apart, so it lands on strong
# beats only, rather than every single onset.
# -------------------------------------------------

def detect_beat_cuts(audio_path, min_spacing=3.0, height=0.15):
    with tempfile.NamedTemporaryFile(
        suffix=".wav", delete=False
    ) as tmp:
        tmp_wav_path = tmp.name

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "22050",
            tmp_wav_path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )

    sr, data = wavfile.read(tmp_wav_path)
    Path(tmp_wav_path).unlink(missing_ok=True)

    data = data.astype(np.float32)
    data /= (np.max(np.abs(data)) + 1e-9)

    n_fft, hop = 2048, 512
    f, t, Zxx = stft(
        data, fs=sr, nperseg=n_fft, noverlap=n_fft - hop
    )
    mag = np.abs(Zxx)

    flux = np.sum(np.maximum(0, np.diff(mag, axis=1)), axis=0)
    flux = flux / (np.max(flux) + 1e-9)

    hop_sec = t[1] - t[0]
    min_dist = max(1, int(min_spacing / hop_sec))

    peaks, _ = find_peaks(flux, height=height, distance=min_dist)

    return sorted(t[peaks].tolist())


# -------------------------------------------------
# Beat Templates (Format 2 only).
#
# A template is a saved, user-confirmed list of beat
# points for one specific song, keyed by filename. Once
# a song has a template, the Filters / Effects settings
# reuse it — deterministic and editable, rather than
# re-guessed. Purely mechanical file I/O.
# -------------------------------------------------

def template_path_for_song(song_path):
    return BEAT_TEMPLATES_DIR / f"{Path(song_path).name}.json"


def load_beat_template(song_path):
    path = template_path_for_song(song_path)

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_beat_template(song_path, beats, template_name):
    existing = load_beat_template(song_path)
    version = (existing["version"] + 1) if existing else 1

    payload = {
        "song": Path(song_path).name,
        "template_name": template_name,
        "version": version,
        "beats": sorted(beats),
    }

    template_path_for_song(song_path).write_text(
        json.dumps(payload, indent=2)
    )

    return payload



# -------------------------------------------------
# Auto Captions.
#
# Transcription (words + timing) is cached per audio
# file, same pattern as Beat Templates — transcribe
# once, re-render as many times as you want without
# re-running Whisper. Rendering itself (chunking words
# into on-screen lines, styling, burning them in with
# drawtext) is plain mechanical filter-building.
# -------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_whisper_model(model_size="base"):
    return WhisperModel(model_size, device="cpu", compute_type="int8")


def caption_template_path_for_clip(clip_path):
    return CAPTION_TEMPLATES_DIR / f"{Path(clip_path).name}.json"


def load_caption_template(clip_path):
    path = caption_template_path_for_clip(clip_path)

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_caption_template(clip_path, words):
    payload = {
        "clip": Path(clip_path).name,
        "words": words,
    }

    CAPTION_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    caption_template_path_for_clip(clip_path).write_text(
        json.dumps(payload, indent=2)
    )

    return payload


def transcribe_words(clip_path, model_size="base", force=False):
    """
    Returns a list of {"text", "start", "end"} dicts, one
    per spoken word, pulled from a cached .json template
    if one already exists for this exact clip filename.
    """

    if not force:
        cached = load_caption_template(clip_path)
        if cached is not None:
            return cached["words"]

    model = get_whisper_model(model_size)

    segments, _info = model.transcribe(
        str(clip_path),
        word_timestamps=True,
    )

    words = []

    for segment in segments:
        for word in segment.words:
            text = word.word.strip()
            if not text:
                continue
            words.append(
                {
                    "text": text,
                    "start": round(word.start, 3),
                    "end": round(word.end, 3),
                }
            )

    save_caption_template(clip_path, words)

    return words


def chunk_words_for_captions(words, max_words=3, max_gap=0.35):
    """
    Groups single words into short on-screen phrases —
    mechanical grouping, same idea as CapCut auto-captions:
    break the line on a natural pause or once it hits
    max_words, whichever comes first.
    """

    chunks = []
    current = []

    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            if gap > max_gap or len(current) >= max_words:
                chunks.append(current)
                current = []
        current.append(word)

    if current:
        chunks.append(current)

    return [
        {
            "text": " ".join(w["text"] for w in chunk).upper(),
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
        }
        for chunk in chunks
    ]


def resolve_caption_font():
    for candidate in CAPTION_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return CAPTION_FONT_CANDIDATES[-1]  # let ffmpeg raise a clear error


def escape_drawtext_font_path(path):
    # ffmpeg's filtergraph parser splits a filter's options on ':'.
    # For a Windows drive letter like "C:/...", backslash-escaping
    # the colon is NOT enough by itself — the value also has to be
    # wrapped in single quotes, or the parser still treats the ':'
    # as an option separator and dies with "No option name near ...".
    # The combo that actually parses cleanly (confirmed against a
    # live ffmpeg build, and matching the ffmpeg wiki's Drawtext
    # page): fontfile='C\:/Windows/Fonts/x.ttf' — quoted AND escaped.
    # This returns the quoted value; callers should NOT add their
    # own quotes around it.
    escaped = str(path).replace(":", "\\:")
    return f"'{escaped}'"


def escape_drawtext_text(text):
    # Order matters: backslash first, then the characters
    # drawtext treats specially inside its text= value.
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\u2019")   # swap to a safe apostrophe
    text = text.replace("%", "\\%")
    return text


# -------------------------------------------------
# Auto-fit for burned-in text.
#
# Previously every drawtext call used a fixed fontsize with no
# check against the frame width at all, so a long hook/caption
# just rendered straight off both edges of the screen. This
# shrinks the fontsize first (down to a floor), then — if it's
# STILL too wide even at the floor size — wraps onto extra lines,
# so text can no longer overflow the frame.
#
# When Pillow is installed, width is measured against the exact
# .ttf file drawtext will use, so the fit matches what actually
# gets burned in. Without Pillow, falls back to a rough
# per-character estimate — still enough to avoid the worst
# overflow, just less precise.
#
# max_width_px lets a caller (the manual on-screen text box, see
# build_onscreen_text_filter) fit against a specific box width
# instead of the whole frame margin — same shrink/wrap logic,
# just measured against a narrower target.
# -------------------------------------------------

TEXT_FIT_MARGIN_FRAC = 0.06     # ~6% margin either side of the frame
TEXT_FIT_MIN_FONTSIZE = 32
TEXT_FIT_MAX_LINES = 3
_FALLBACK_CHAR_WIDTH_FACTOR = 0.60  # rough avg glyph width / fontsize


def _measure_text_px(text, font_path, fontsize):
    if not text:
        return 0
    if PIL_AVAILABLE:
        try:
            # font_path here must be the RAW filesystem path (not
            # the ffmpeg-escaped/quoted value drawtext gets).
            font = ImageFont.truetype(font_path, fontsize)
            box = font.getbbox(text)
            return box[2] - box[0]
        except Exception:
            pass
    return len(text) * fontsize * _FALLBACK_CHAR_WIDTH_FACTOR


def _wrap_line_at_fontsize(text, font_path, fontsize, max_width_px):
    words = text.split()
    if not words:
        return [""]

    lines = []
    current = words[0]

    for word in words[1:]:
        candidate = f"{current} {word}"
        if _measure_text_px(candidate, font_path, fontsize) <= max_width_px:
            current = candidate
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def fit_drawtext(
    text,
    font_path,
    frame_w,
    base_fontsize,
    font_scale=1.0,
    min_fontsize=TEXT_FIT_MIN_FONTSIZE,
    max_lines=TEXT_FIT_MAX_LINES,
    max_width_px=None,
):
    """
    Returns (display_text, fontsize, line_count). display_text may
    contain a literal newline between wrapped lines — drawtext
    treats an embedded newline as a real line break, no escaping
    needed beyond the usual escape_drawtext_text() call.

    font_scale is the user's manual "Text size" adjustment (see the
    per-template style on the Templates page) — applied BEFORE the
    fit check, not after, so cranking it up can still trigger
    shrinking/wrapping rather than overflowing.

    max_width_px overrides the default frame-margin-based width —
    used when the text has a manually-placed box (see
    build_onscreen_text_filter) so the fit matches that box's
    actual width instead of the whole frame.
    """

    if max_width_px is None:
        max_width_px = frame_w * (1 - 2 * TEXT_FIT_MARGIN_FRAC)

    fontsize = max(min_fontsize, int(round(base_fontsize * font_scale)))

    # 1. Try shrinking a single line down to the floor size.
    size = fontsize
    while size > min_fontsize:
        if _measure_text_px(text, font_path, size) <= max_width_px:
            return text, size, 1
        size -= 2

    if _measure_text_px(text, font_path, min_fontsize) <= max_width_px:
        return text, min_fontsize, 1

    # 2. Still too wide even at the floor size -> wrap onto lines.
    lines = _wrap_line_at_fontsize(
        text, font_path, min_fontsize, max_width_px
    )

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "\u2026"

    return "\n".join(lines), min_fontsize, len(lines)


CAPTION_Y_FRACTIONS = {
    "stack": 0.40,      # lower third of the TOP half (streamer's face)
    "fullscreen": 0.82,  # standard bottom-of-screen captions
}


def build_caption_filter_chain(
    chunks,
    input_label,
    output_label,
    frame_h=1920,
    frame_w=1080,
    position="stack",
    font_scale=1.0,
):
    """
    Chains one drawtext filter per caption chunk onto
    input_label, each only visible during its own time
    window via enable='between(t,start,end)'. Style is
    the "Bold Pop" look: big bold white text, thick black
    outline, centered. `position` controls where vertically:
    "stack" sits in the lower third of the TOP half (for the
    Streamer Clip + Song stacked layout), "fullscreen" sits
    near the bottom of the whole frame (for Movie Clip / Meme's
    single fullscreen clip).

    Each chunk's text is auto-fit to frame_w (shrunk, then wrapped
    if needed) so it can't render past the edges of the frame —
    font_scale is the user's manual "Text size" adjustment from the
    template's style settings, applied within that same safety net.
    """

    raw_font_path = resolve_caption_font()
    font = escape_drawtext_font_path(raw_font_path)
    base_y = frame_h * CAPTION_Y_FRACTIONS[position]

    stages = []
    label = input_label

    for index, chunk in enumerate(chunks):
        display_text, fontsize, line_count = fit_drawtext(
            chunk["text"],
            raw_font_path,
            frame_w,
            base_fontsize=76,
            font_scale=font_scale,
        )
        text = escape_drawtext_text(display_text)

        line_height = fontsize * 1.2 + 4
        extra_lines = line_count - 1
        y_expr = f"{int(base_y - extra_lines * line_height / 2)}"

        next_label = f"[cap{index}]" if index < len(chunks) - 1 else output_label

        stages.append(
            f"{label}drawtext="
            f"fontfile={font}:"
            f"text='{text}':"
            f"fontsize={fontsize}:"
            f"fontcolor=white:"
            f"borderw=8:"
            f"bordercolor=black:"
            f"line_spacing=4:"
            f"x=(w-text_w)/2:"
            f"y={y_expr}:"
            f"enable='between(t\\,{chunk['start']}\\,{chunk['end']})'"
            f"{next_label}"
        )

        label = next_label

    return ";".join(stages)


ONSCREEN_TEXT_Y_FRACTIONS = {
    "stack": 0.08,       # near the top of the TOP half
    "fullscreen": 0.08,  # near the top of the whole frame
}


def build_onscreen_text_filter(
    text,
    input_label,
    output_label,
    frame_h=1920,
    frame_w=1080,
    position="stack",
    font_scale=1.0,
    manual_box=None,
):
    """
    Burns in ONE static on-screen text overlay for the whole
    duration of the video — the user-supplied hook/caption text,
    not the auto-transcribed Whisper captions. Same "Bold Pop"
    drawtext styling as build_caption_filter_chain, but with no
    enable=between(...) window, since it's visible start to end.

    manual_box, when given, is the {"x_pct","y_pct","w_pct"} box the
    user dragged/resized on the Crop Presets page: the text is
    centered horizontally WITHIN that box (not the whole frame), fit
    against the box's width, and anchored to the box's top edge —
    this is what makes the on-screen text follow a manual drag/
    resize the same way the video layers do. Without manual_box,
    falls back to the old fixed top-of-frame/top-of-half position.

    Auto-fit to the target width the same way as captions — this is
    what stops a long hook like "WAIT FOR SPEED'S REACTION TO NEW
    KSI SONG" from rendering off the edges of its box.
    """

    raw_font_path = resolve_caption_font()
    font = escape_drawtext_font_path(raw_font_path)

    if manual_box:
        box_x_px = frame_w * manual_box.get("x_pct", 10) / 100
        box_w_px = frame_w * manual_box.get("w_pct", 80) / 100
        base_y = frame_h * manual_box.get("y_pct", 4) / 100
    else:
        box_x_px = None
        box_w_px = None
        base_y = frame_h * ONSCREEN_TEXT_Y_FRACTIONS[position]

    display_text, fontsize, line_count = fit_drawtext(
        text.strip().upper(),
        raw_font_path,
        frame_w,
        base_fontsize=72,
        font_scale=font_scale,
        max_width_px=box_w_px,
    )
    escaped = escape_drawtext_text(display_text)

    line_height = fontsize * 1.2 + 4
    extra_lines = line_count - 1
    y_expr = f"{int(base_y - extra_lines * line_height / 2)}"

    if box_x_px is not None:
        x_expr = f"({box_x_px:.1f})+(({box_w_px:.1f})-text_w)/2"
    else:
        x_expr = "(w-text_w)/2"

    return (
        f"{input_label}drawtext="
        f"fontfile={font}:"
        f"text='{escaped}':"
        f"fontsize={fontsize}:"
        f"fontcolor=white:"
        f"borderw=8:"
        f"bordercolor=black:"
        f"line_spacing=4:"
        f"x={x_expr}:"
        f"y={y_expr}"
        f"{output_label}"
    )


def parse_time_list(text):
    """
    Pulls every number out of pasted text (from the Listen & Tap
    box) and returns sorted, de-duplicated, 2-decimal seconds.
    Plain text parsing only — no AI involved.
    """
    if not text:
        return []

    numbers = re.findall(r"\d+\.?\d*", text)

    return sorted(
        {round(float(n), 2) for n in numbers}
    )


def parse_layout_paste(text):
    """
    Parses the JSON layout dict pasted from the Crop Presets drag/
    resize tool's output box — something like:
      {"top_video": {"x_pct":0,"y_pct":0,"w_pct":100,"h_pct":48.2}, ...}
    Returns a dict of {layer_key: {x_pct,y_pct,w_pct,h_pct}} for
    whichever layer keys parsed cleanly, or None if nothing did.
    Plain JSON parsing only — no AI involved.
    """
    if not text:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    cleaned = {}

    for key, box in data.items():
        if not isinstance(box, dict):
            continue
        try:
            cleaned[key] = {
                "x_pct": round(float(box["x_pct"]), 1),
                "y_pct": round(float(box["y_pct"]), 1),
                "w_pct": round(float(box["w_pct"]), 1),
                "h_pct": round(float(box["h_pct"]), 1),
            }
        except (KeyError, TypeError, ValueError):
            continue

    return cleaned or None


_AUDIO_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}

_VIDEO_MIME_TYPES = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
}

_TAP_TOOL_HTML = r"""
<div id="tapwrap" tabindex="0" style="outline:none; font-family:-apple-system,'Segoe UI',sans-serif; background:#111827; border-radius:12px; padding:16px; color:#e5e7eb;">
  <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap;">
    <button id="playBtn" style="background:#374151;color:#fff;border:none;border-radius:8px;padding:10px 16px;font-size:16px;cursor:pointer;">&#9654;&#65039; Play</button>
    <span id="timeLabel" style="font-variant-numeric:tabular-nums; font-size:14px; color:#9ca3af;">0:00.00 / 0:00.00</span>
    <span style="flex:1;"></span>
    <label style="font-size:13px;color:#9ca3af;">Speed
      <select id="rateSel" style="margin-left:6px;background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:6px;padding:4px;">
        <option value="0.5">0.5x</option>
        <option value="0.75">0.75x</option>
        <option value="1" selected>1x</option>
        <option value="1.25">1.25x</option>
      </select>
    </label>
  </div>

  <div id="timeline" style="position:relative; height:64px; background:#1f2937; border-radius:8px; cursor:pointer; overflow:hidden; margin-bottom:10px;">
    <div id="playhead" style="position:absolute; top:0; left:0; width:2px; height:100%; background:#f87171;"></div>
  </div>

  <div style="display:flex; gap:10px; margin-bottom:10px; flex-wrap:wrap;">
    <button id="tapBtn" style="flex:1; min-width:160px; background:#6366f1;color:#fff;border:none;border-radius:10px;padding:16px;font-size:18px;font-weight:700;cursor:pointer;">&#129345; TAP BEAT (Enter)</button>
    <button id="undoBtn" style="background:#374151;color:#fff;border:none;border-radius:8px;padding:10px 14px;cursor:pointer;">&#8617;&#65039; Undo</button>
    <button id="clearBtn" style="background:#374151;color:#fff;border:none;border-radius:8px;padding:10px 14px;cursor:pointer;">&#128465;&#65039; Clear</button>
  </div>

  <div style="font-size:13px;color:#9ca3af;margin-bottom:6px;">
    <span id="tapCount">0</span> beats tapped &mdash; click inside this box once, then Space = play/pause, Enter = tap. Click the timeline to seek.
  </div>

  <textarea id="tapOutput" readonly rows="2" style="width:100%; box-sizing:border-box; background:#1f2937; color:#e5e7eb; border:1px solid #374151; border-radius:8px; padding:8px; font-family:monospace; font-size:13px;" placeholder="Tapped times will appear here as you tap..."></textarea>
  <button id="selectBtn" style="margin-top:6px; background:#374151;color:#fff;border:none;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;">&#128269; Select All (then Ctrl/Cmd+C)</button>

  <audio id="player" src="__AUDIO_SRC__" preload="metadata"></audio>
</div>

<script>
(function() {
  var wrap = document.getElementById('tapwrap');
  var audio = document.getElementById('player');
  var playBtn = document.getElementById('playBtn');
  var tapBtn = document.getElementById('tapBtn');
  var undoBtn = document.getElementById('undoBtn');
  var clearBtn = document.getElementById('clearBtn');
  var selectBtn = document.getElementById('selectBtn');
  var timeline = document.getElementById('timeline');
  var playhead = document.getElementById('playhead');
  var timeLabel = document.getElementById('timeLabel');
  var tapCount = document.getElementById('tapCount');
  var tapOutput = document.getElementById('tapOutput');
  var rateSel = document.getElementById('rateSel');

  var taps = [];

  function fmt(t) {
    if (!isFinite(t)) return '0:00.00';
    var m = Math.floor(t / 60);
    var s = (t - m * 60).toFixed(2);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function renderTicks() {
    var existing = timeline.querySelectorAll('.tick');
    existing.forEach(function(el) { el.remove(); });
    var dur = audio.duration || 0;
    if (!dur) return;
    taps.forEach(function(t) {
      var tick = document.createElement('div');
      tick.className = 'tick';
      tick.style.position = 'absolute';
      tick.style.top = '0';
      tick.style.left = (t / dur * 100) + '%';
      tick.style.width = '3px';
      tick.style.height = '100%';
      tick.style.background = '#34d399';
      tick.style.boxShadow = '0 0 4px #34d399';
      timeline.appendChild(tick);
    });
  }

  function renderOutput() {
    var sorted = taps.slice().sort(function(a, b) { return a - b; });
    tapOutput.value = sorted.map(function(t) { return t.toFixed(2); }).join(', ');
    tapCount.textContent = taps.length;
    renderTicks();
  }

  function tapNow() {
    if (!isFinite(audio.currentTime)) return;
    taps.push(Math.round(audio.currentTime * 100) / 100);
    renderOutput();
  }

  function updatePlayhead() {
    var dur = audio.duration || 0;
    if (dur) {
      playhead.style.left = (audio.currentTime / dur * 100) + '%';
    }
    timeLabel.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
  }

  playBtn.addEventListener('click', function() {
    if (audio.paused) { audio.play(); } else { audio.pause(); }
    wrap.focus();
  });

  audio.addEventListener('play', function() { playBtn.innerHTML = '&#9208;&#65039; Pause'; });
  audio.addEventListener('pause', function() { playBtn.innerHTML = '&#9654;&#65039; Play'; });
  audio.addEventListener('timeupdate', updatePlayhead);
  audio.addEventListener('loadedmetadata', function() { updatePlayhead(); renderOutput(); });

  tapBtn.addEventListener('click', function() { tapNow(); wrap.focus(); });
  undoBtn.addEventListener('click', function() { taps.pop(); renderOutput(); wrap.focus(); });
  clearBtn.addEventListener('click', function() { taps = []; renderOutput(); wrap.focus(); });

  selectBtn.addEventListener('click', function() {
    tapOutput.focus();
    tapOutput.select();
  });

  timeline.addEventListener('click', function(e) {
    var dur = audio.duration || 0;
    if (!dur) return;
    var rect = timeline.getBoundingClientRect();
    var ratio = (e.clientX - rect.left) / rect.width;
    audio.currentTime = Math.max(0, Math.min(dur, ratio * dur));
  });

  rateSel.addEventListener('change', function() {
    audio.playbackRate = parseFloat(rateSel.value);
  });

  wrap.addEventListener('keydown', function(e) {
    if (e.code === 'Space') {
      e.preventDefault();
      if (audio.paused) { audio.play(); } else { audio.pause(); }
    } else if (e.code === 'Enter') {
      e.preventDefault();
      tapNow();
    } else if (e.code === 'Backspace' || e.code === 'Delete') {
      e.preventDefault();
      taps.pop();
      renderOutput();
    }
  });

  wrap.tabIndex = 0;
  setTimeout(function() { wrap.focus(); }, 300);
})();
</script>
"""


def render_tap_tool(song_path):
    """
    Embeds a CapCut-style "listen and tap" beat marker: real audio
    playback in-browser (JS), a big tap button + Enter/Space keys,
    live tick marks on a timeline. Purely a listening/tapping UI —
    the tapped times are read back into Streamlit by pasting the
    box's contents into the text area below it (Streamlit's HTML
    component can't push values back to Python directly).
    """
    suffix = song_path.suffix.lower().replace(".", "")
    mime = _AUDIO_MIME_TYPES.get(suffix, "audio/mpeg")
    encoded = base64.b64encode(song_path.read_bytes()).decode("ascii")
    audio_src = f"data:{mime};base64,{encoded}"

    html = _TAP_TOOL_HTML.replace("__AUDIO_SRC__", audio_src)

    components.html(html, height=470, scrolling=False)


# -------------------------------------------------
# Manual layout tool (Format 1, Crop Presets page).
#
# Real drag-to-move / drag-handle-to-resize placement of every
# layer (video clips + the on-screen text box) directly on one
# 1080x1920 canvas at once — the actual CapCut-editor interaction
# the crop-box sliders were standing in for, powered by Moveable.js
# (MIT-licensed, CDN-loaded, no build step). Each layer gets its
# own independent Moveable instance, so multiple layers can each
# show their own drag/resize handles on the same stage.
#
# Like the tap/crop tools above, Streamlit's plain HTML component
# can't push values back to Python directly, so the live layout is
# read back the same way: a JSON readout box the user selects,
# copies, and pastes into a text_area next to the tool.
# -------------------------------------------------

_LAYOUT_TOOL_HTML = r"""
<div id="layoutwrap" tabindex="0" style="outline:none; font-family:-apple-system,'Segoe UI',sans-serif; background:#111827; border-radius:12px; padding:16px; color:#e5e7eb;">
  <div id="stage" style="position:relative; width:100%; max-width:340px; aspect-ratio:9/16; margin:0 auto; background:#000; overflow:hidden; border-radius:6px; box-shadow:0 0 0 1px #374151;">
    __LAYERS__
  </div>

  <div style="font-size:13px;color:#9ca3af;margin:10px 0;">
    Drag any clip or the text box to move it. Drag a corner or edge
    handle to resize &mdash; width and height move independently,
    so you can close gaps or overlap layers exactly like in CapCut.
  </div>

  <div style="display:flex; gap:8px; margin-bottom:8px;">
    <button id="resetBtn" style="background:#374151;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;">&#8634; Reset to Default</button>
  </div>

  <div style="display:flex; gap:8px;">
    <textarea id="output" readonly rows="2" style="flex:1; font-family:monospace; font-size:12px; background:#1f2937; color:#e5e7eb; border:1px solid #374151; border-radius:8px; padding:8px; resize:none;"></textarea>
    <button id="selectBtn" style="background:#374151;color:#fff;border:none;border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer; white-space:nowrap;">&#128269; Select</button>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/moveable@0.53.0/dist/moveable.min.js"></script>
<script>
(function() {
  var LAYERS = __LAYERS_JSON__;
  var stage = document.getElementById('stage');
  var output = document.getElementById('output');
  var selectBtn = document.getElementById('selectBtn');
  var resetBtn = document.getElementById('resetBtn');
  var moveables = [];

  function round1(n) { return Math.round(n * 10) / 10; }

  function pctToPx(layout) {
    var sw = stage.clientWidth, sh = stage.clientHeight;
    return {
      left: layout.x_pct / 100 * sw,
      top: layout.y_pct / 100 * sh,
      width: layout.w_pct / 100 * sw,
      height: layout.h_pct / 100 * sh
    };
  }

  function updateOutput() {
    var sw = stage.clientWidth, sh = stage.clientHeight;
    var result = {};
    LAYERS.forEach(function(layer) {
      var el = document.getElementById('layer_' + layer.key);
      result[layer.key] = {
        x_pct: round1(parseFloat(el.style.left) / sw * 100),
        y_pct: round1(parseFloat(el.style.top) / sh * 100),
        w_pct: round1(parseFloat(el.style.width) / sw * 100),
        h_pct: round1(parseFloat(el.style.height) / sh * 100)
      };
    });
    output.value = JSON.stringify(result);
  }

  function applyLayout(layer) {
    var el = document.getElementById('layer_' + layer.key);
    var box = pctToPx(layer.layout);
    el.style.left = box.left + 'px';
    el.style.top = box.top + 'px';
    el.style.width = box.width + 'px';
    el.style.height = box.height + 'px';
  }

  function init() {
    LAYERS.forEach(function(layer) {
      applyLayout(layer);
      var el = document.getElementById('layer_' + layer.key);

      var mv = new Moveable(stage, {
        target: el,
        draggable: true,
        resizable: true,
        keepRatio: false,
        edge: false,
        origin: false,
        throttleDrag: 0,
        throttleResize: 0
      });

      mv.on('drag', function(e) {
        e.target.style.left = e.left + 'px';
        e.target.style.top = e.top + 'px';
      });
      mv.on('resize', function(e) {
        e.target.style.width = e.width + 'px';
        e.target.style.height = e.height + 'px';
        e.target.style.left = e.drag.left + 'px';
        e.target.style.top = e.drag.top + 'px';
      });
      mv.on('dragEnd', updateOutput);
      mv.on('resizeEnd', updateOutput);

      moveables.push(mv);
    });

    // Seek every clip roughly to its midpoint so the preview isn't
    // sitting on a possibly-blank/black first frame.
    var videos = stage.querySelectorAll('video');
    videos.forEach(function(v) {
      v.addEventListener('loadedmetadata', function() {
        if (v.duration && isFinite(v.duration)) {
          v.currentTime = Math.min(
            Math.max(0.5, v.duration / 2),
            Math.max(0, v.duration - 0.2)
          );
        }
      });
    });

    updateOutput();
  }

  resetBtn.addEventListener('click', function() {
    LAYERS.forEach(applyLayout);
    moveables.forEach(function(mv) { mv.updateRect(); });
    updateOutput();
  });

  selectBtn.addEventListener('click', function() {
    output.focus();
    output.select();
  });

  if (document.readyState === 'complete') {
    setTimeout(init, 50);
  } else {
    window.addEventListener('load', function() { setTimeout(init, 50); });
  }
})();
</script>
"""


def render_manual_layout_tool(layers):
    """
    layers: list of dicts, each either
      {"key": str, "kind": "video", "video_path": Path, "label": str, "layout": {...pct...}}
    or
      {"key": str, "kind": "text", "text_content": str, "layout": {...pct...}}

    Renders every layer as its own absolutely-positioned box on one
    1080x1920-proportioned stage, each with its own Moveable drag/
    resize handles active at the same time — this is the single
    combined canvas (all clips + the text box together) rather than
    editing one half blind to the other, matching how CapCut shows
    the whole composite while you drag one piece of it.
    """
    layer_divs = []
    layers_json = []

    for layer in layers:
        layout = layer["layout"]
        layers_json.append({"key": layer["key"], "layout": layout})

        if layer["kind"] == "video":
            suffix = layer["video_path"].suffix.lower().replace(".", "")
            mime = _VIDEO_MIME_TYPES.get(suffix, "video/mp4")
            encoded = base64.b64encode(
                layer["video_path"].read_bytes()
            ).decode("ascii")
            src = f"data:{mime};base64,{encoded}"

            layer_divs.append(
                f'<div id="layer_{layer["key"]}" class="layer" '
                'style="position:absolute; overflow:hidden; '
                'border:2px solid #34d399; box-sizing:border-box; cursor:move;">'
                f'<video src="{src}" muted preload="auto" '
                'style="width:100%; height:100%; object-fit:cover; '
                'pointer-events:none; display:block;"></video>'
                '<div style="position:absolute; top:2px; left:4px; '
                'font-size:11px; color:#34d399; background:rgba(0,0,0,0.5); '
                'padding:1px 4px; border-radius:3px; pointer-events:none;">'
                f'{layer.get("label", layer["key"])}</div>'
                '</div>'
            )
        else:
            text_content = (layer.get("text_content") or "YOUR ON-SCREEN TEXT").upper()
            layer_divs.append(
                f'<div id="layer_{layer["key"]}" class="layer" '
                'style="position:absolute; box-sizing:border-box; cursor:move; '
                'border:2px dashed #f59e0b; display:flex; align-items:center; '
                'justify-content:center; text-align:center; padding:4px; overflow:hidden;">'
                '<span style="color:#fff; font-weight:800; font-size:5.5vw; '
                'line-height:1.15; text-shadow:-2px -2px 0 #000,2px -2px 0 #000,'
                '-2px 2px 0 #000,2px 2px 0 #000; word-break:break-word;">'
                f'{text_content}</span>'
                '</div>'
            )

    html = (
        _LAYOUT_TOOL_HTML
        .replace("__LAYERS__", "".join(layer_divs))
        .replace("__LAYERS_JSON__", json.dumps(layers_json))
    )

    components.html(html, height=760, scrolling=False)


def _manual_video_layer_filter(
    input_label, trim_duration, layout, out_label, canvas_w=1080, canvas_h=1920
):
    """
    Scales+crops a video input to exactly fill the size of its
    manually-placed box (from the Crop Presets drag/resize tool),
    crop-to-fill so it never stretches/squishes — then hands back
    the filter chain plus that box's pixel position on the canvas,
    ready for an overlay= call. x_pct/y_pct/w_pct/h_pct come
    straight from what the user dragged/resized.
    """
    box_w = max(2, round(canvas_w * layout.get("w_pct", 100) / 100))
    box_h = max(2, round(canvas_h * layout.get("h_pct", 100) / 100))
    box_x = round(canvas_w * layout.get("x_pct", 0) / 100)
    box_y = round(canvas_h * layout.get("y_pct", 0) / 100)

    chain = (
        f"[{input_label}]"
        f"trim=start=0:duration={trim_duration},setpts=PTS-STARTPTS,"
        f"scale={box_w}:{box_h}:force_original_aspect_ratio=increase,"
        f"crop={box_w}:{box_h},setsar=1,fps=30,settb=1/30"
        f"{out_label}"
    )
    return chain, box_x, box_y


def build_clear2_filter(input_label, output_label):
    """
    "Clear 2" preset — a fixed adjustment recipe applied once to the
    whole finished video:
      Brightness -3, Contrast +7, Saturation +20, Sharpen +20,
      Clarity +11, Temperature -3, Highlights -4, Shadows -2,
      blended back over the untouched original at 89% strength.

    Deterministic ffmpeg filter math (eq / unsharp / colorbalance /
    curves / blend) — no AI, no per-frame analysis. This is the
    ONLY filter preset; nothing else ships alongside it.
    """
    return (
        f"[{input_label}]split=2[c2base][c2fx];"
        "[c2fx]"
        "eq=brightness=-0.010:contrast=1.035:saturation=1.20,"
        "unsharp=5:5:0.40:5:5:0.00,"
        "unsharp=13:13:0.17:13:13:0.00,"
        "colorbalance="
        "rs=0.00:gs=0.00:bs=0.01:"
        "rm=-0.01:gm=0.00:bm=0.01:"
        "rh=-0.01:gh=0.00:bh=0.01,"
        "curves=all='0/0 0.25/0.247 0.75/0.744 1/1'"
        "[c2adj];"
        f"[c2base][c2adj]blend=all_mode=normal:all_opacity=0.89[{output_label}]"
    )


def goto_page(page_name):
    """Queue a navigation change before the next sidebar render."""
    # Can't write st.session_state["nav_page"] directly here — the
    # nav_page radio widget was already instantiated earlier in this
    # script run, and Streamlit forbids mutating a widget's key after
    # it's been created. Instead, stash the target page and apply it
    # on the next run, before the radio widget is (re)created.
    st.session_state["_pending_nav_page"] = page_name
    st.rerun()


def queue_for_auto_post(format_label, output_path):
    """
    Auto Post is shared across both formats. Preview pages call this
    when the user confirms a rendered clip is good to post. No actual
    posting automation yet — just a tracked queue.
    """
    queue = st.session_state.setdefault("auto_post_queue", [])
    if any(item["path"] == str(output_path) for item in queue):
        return
    queue.append(
        {
            "format": format_label,
            "name": Path(output_path).name,
            "path": str(output_path),
        }
    )


# ─────────────────────────────────────────────
# NAVIGATION
#
# The site opens on a Dashboard that asks which format
# to use. Once picked, the sidebar swaps to a
# nav list scoped to that format. Both use saved Open Reel
# templates and share the Auto Post queue.
# -------------------------------------------------

st.session_state.setdefault("active_format", None)

FORMAT2_NAV = [
    "Asset Pools",
    "Assets",
    "Caption Library",
    "Editor",
    "Templates",
    "Auto Post",
]

FORMAT1_NAV = [
    "Asset Pools",
    "Assets",
    "Caption Library",
    "Editor",
    "Templates",
    "Auto Post",
]

st.sidebar.title("🎬 CRE")

if "reel_template" in st.query_params:
    linked_template = bridge_for(APP_DIR).template(st.query_params["reel_template"])
    if linked_template:
        st.session_state["active_format"] = linked_template["scope"].split("/")[0]
        st.session_state["reel_focus_template"] = linked_template["id"]
        st.session_state["_pending_nav_page"] = "Templates"
    del st.query_params["reel_template"]

active_format = st.session_state["active_format"]

if active_format:

    st.sidebar.markdown(f"### {active_format}")

    if active_format == "Format 3":
        nav_list = FORMAT3_NAV + LEGAL_NAV
    elif active_format == "Format 2":
        nav_list = FORMAT2_NAV + LEGAL_NAV
    else:
        nav_list = FORMAT1_NAV + LEGAL_NAV

    # A button elsewhere in the app can jump to another
    # page via goto_page(), which stashes the target in
    # "_pending_nav_page" and reruns (see goto_page() above — it
    # can't set "nav_page" directly since the radio widget below
    # already owns that key for the run it's called in). Apply
    # any pending jump now, before the radio widget is created.
    if "_pending_nav_page" in st.session_state:
        st.session_state["nav_page"] = st.session_state.pop("_pending_nav_page")
    if active_format != "Format 3" and st.session_state.get("nav_page") in ("Render Queue", "Actually Rendering", "Preview"):
        st.session_state["nav_page"] = "Templates"

    # Guard against a leftover value from the other format's nav
    # list (e.g. right after Switch Format).
    if st.session_state.get("nav_page") not in nav_list:
        st.session_state["nav_page"] = nav_list[0]

    page = st.sidebar.radio("Navigate", nav_list, key="nav_page")

    st.sidebar.divider()

    if st.sidebar.button("⬅️ Switch Format", use_container_width=True):
        st.session_state["active_format"] = None
        st.rerun()

else:

    page = "Dashboard"


# ─────────────────────────────────────────────
# DASHBOARD (landing — format picker)
# ─────────────────────────────────────────────

if page == "Dashboard":

    st.title("Clipping Revenue Engine")

    st.caption("Choose an editing workflow. Format 3 generates validated AI Edit Plans with Gemini and renders them locally.")

    st.divider()

    format1_asset_count = sum(
        len(list_video_files(format1_template_folder(t["slug"])))
        + (
            len(list_video_files(format1_template_song_folder(t["slug"])))
            if t["mode"] == MODE_STACK
            else len(list_audio_files(format1_template_song_folder(t["slug"])))
        )
        for t in load_format1_templates()
    )

    format2_asset_count = (
        count_files("BMW")
        + count_files("LiveMap")
        + len(get_audio_files("Format2Music"))
    )

    with st.container(border=True):
        st.subheader("🧠 Format 3 — AI Edit Plans")
        st.write("Contract-first podcast editing: validated cuts, reframing, punch-ins, dynamic captions, evidence-bound assets, render queue, and review preview.")
        st.caption("Gemini planning is connected; every plan must pass CRE's deterministic validation before local FFmpeg rendering.")
        if st.button("Open Format 3", type="primary", use_container_width=True, key="open_format3"):
            st.session_state["active_format"] = "Format 3"
            st.session_state["nav_page"] = FORMAT3_NAV[0]
            st.rerun()

    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        with st.container(border=True):
            st.subheader("🎥 Format 1")
            st.write(
                "Clip + Song templates — Movie Clip, Meme, Streamer "
                "Clip, or any custom type you add."
            )
            st.metric("Assets", format1_asset_count)
            if st.button(
                "Open Format 1",
                type="primary",
                use_container_width=True,
                key="open_format1",
            ):
                st.session_state["active_format"] = "Format 1"
                st.rerun()

    with col2:
        with st.container(border=True):
            st.subheader("🚗 Format 2")
            st.write(
                "BMW + LiveMap asset pools"
            )
            st.metric("Assets", format2_asset_count)
            if st.button(
                "Open Format 2",
                type="primary",
                use_container_width=True,
                key="open_format2",
            ):
                st.session_state["active_format"] = "Format 2"
                st.rerun()


# ─────────────────────────────────────────────
# FORMAT 3 — VALIDATED AI EDIT PLANS
# ─────────────────────────────────────────────

elif page == "Caption Library" and active_format in ("Format 1", "Format 2", "Format 3"):
    render_caption_library(APP_DIR, active_format)
    if active_format == "Format 3":
        render_caption_word_templates(APP_DIR)

elif page in LEGAL_NAV:
    render_legal_page(page)

elif active_format == "Format 3" and page in FORMAT3_NAV:
    render_format3_page(page, APP_DIR, goto_page, queue_for_auto_post)


# ─────────────────────────────────────────────
# FORMAT 2 — ASSETS
# ─────────────────────────────────────────────

elif page == "Asset Pools" and active_format == "Format 2":
    st.title("Format 2 — Asset Pools")
    st.subheader("BMW + LiveMap")
    st.caption(
        "BMW footage, LiveMap footage, and background music."
    )
    if st.button("Open template assets", type="primary", key="open_f2_assets"):
        goto_page("Assets")

elif page == "Assets" and active_format == "Format 2":

    st.title("📁 Format 2 Assets")

    st.write(
        "Upload the raw clips CRE should use for Format 2 — BMW "
        "footage, LiveMap footage, and background music."
    )

    st.write(
        "CRE does not analyze, rank, or choose clips."
    )

    st.divider()

    col1, col2 = st.columns(2)

    with col1:

        st.markdown("### 🚗 BMW")

        show_pool("BMW")

        files = st.file_uploader(
            "Upload BMW clips",
            type=VIDEO_TYPES,
            accept_multiple_files=True,
            key="bmw_upload",
        )

        if files and st.button(
            "Save BMW Clips",
            key="save_bmw",
        ):
            save_files(files, "BMW")
            st.success(f"Saved {len(files)} clip(s).")
            st.rerun()

    with col2:

        st.markdown("### 🗺️ LiveMap")

        show_pool("LiveMap")

        files = st.file_uploader(
            "Upload LiveMap clips",
            type=VIDEO_TYPES,
            accept_multiple_files=True,
            key="livemap_upload",
        )

        if files and st.button(
            "Save LiveMap Clips",
            key="save_livemap",
        ):
            save_files(files, "LiveMap")
            st.success(f"Saved {len(files)} clip(s).")
            st.rerun()

    st.divider()

    st.subheader("🎵 Background Music Library")

    st.write(
        "Background music assets for Format 2."
    )

    show_pool("Format2Music")

    files = st.file_uploader(
        "Upload Format 2 music",
        type=AUDIO_TYPES,
        accept_multiple_files=True,
        key="format2_music_upload",
    )

    if files and st.button(
        "Save Format 2 Music",
        key="save_format2_music",
    ):
        save_files(files, "Format2Music")
        st.success(f"Saved {len(files)} music file(s).")
        st.rerun()

    has_format2_assets = any(
        get_files(bucket) or get_audio_files(bucket)
        for bucket in ("BMW", "LiveMap", "Format2Music")
    )
    if has_format2_assets and st.button(
        "Open Editor with these assets",
        type="primary",
        use_container_width=True,
        key="f2_goto_editor",
    ):
        with st.spinner("Preparing editor media..."):
            open_pool(APP_DIR, "Format 2", [ASSETS_DIR / BUCKETS[key]
                for key in ("BMW", "LiveMap", "Format2Music")])
        goto_page("Editor")

    if st.button("Replicate from saved template", key="f2_assets_continue"):
        goto_page("Templates")


# ─────────────────────────────────────────────
# FORMAT 2 — BEAT TEMPLATES
# ─────────────────────────────────────────────

elif page == "Beat Templates" and active_format == "Format 2":

    st.title("🥁 Beat Templates")

    st.caption(
        "Identify beat points for a song once, edit and confirm "
        "them, and save a reusable template. Mechanical signal "
        "processing only (spectral flux onset detection) — no AI, "
        "no scene understanding."
    )

    st.divider()

    if not BEAT_DETECTION_AVAILABLE:

        st.error(
            "Beat detection needs `numpy` and `scipy`, which aren't "
            "installed. Run `pip install numpy scipy` and relaunch "
            "the app."
        )

    else:

        music_files = get_audio_files("Format2Music")

        if not music_files:

            st.warning(
                "No Format 2 music tracks found. Upload some on the "
                "Assets page first."
            )

        else:

            # -------------------------------------------------
            # Song picker. "Random Song" mimics the mechanical
            # random-pick step in the actual render pipeline, so
            # you can pre-build a template for whatever comes up.
            # -------------------------------------------------

            if (
                "beat_tpl_select" not in st.session_state
                or st.session_state["beat_tpl_select"]
                not in [f.name for f in music_files]
            ):
                st.session_state["beat_tpl_select"] = music_files[0].name

            col1, col2 = st.columns([3, 1])

            # NOTE: the "Random Song" button's logic is written first
            # (even though it renders on the right, in col2) so that
            # if it fires, it updates st.session_state["beat_tpl_select"]
            # BEFORE the st.selectbox below is instantiated with that
            # same key. Streamlit forbids writing to a widget's session
            # state key after that widget has been created in the same
            # run — writing first and rerunning avoids that.

            with col2:
                st.write("")
                st.write("")
                if st.button("🎲 Random Song", use_container_width=True):
                    st.session_state["beat_tpl_select"] = random.choice(
                        music_files
                    ).name
                    st.rerun()

            with col1:
                st.selectbox(
                    "Choose a song",
                    [f.name for f in music_files],
                    key="beat_tpl_select",
                )

            song_path = next(
                f
                for f in music_files
                if f.name == st.session_state["beat_tpl_select"]
            )

            st.divider()

            existing_template = load_beat_template(song_path)
            draft_key = f"beat_draft_{song_path.name}"
            redetect_flag = (
                st.session_state.get("beat_tpl_force_redetect")
                == song_path.name
            )

            show_saved = (
                existing_template is not None
                and draft_key not in st.session_state
                and not redetect_flag
            )

            # ---------------------------------------------
            # Template already exists for this song -> use it.
            # ---------------------------------------------

            if show_saved:

                st.success(
                    f"✅ **{existing_template['template_name']}** found "
                    f"for `{song_path.name}` "
                    f"({len(existing_template['beats'])} beat points, "
                    f"v{existing_template['version']})."
                )

                st.write("Saved beat points (seconds):")
                st.code(
                    ", ".join(
                        f"{b:.2f}" for b in existing_template["beats"]
                    )
                )

                col_a, col_b = st.columns(2)

                with col_a:
                    if st.button("✏️ Edit This Template"):
                        st.session_state[draft_key] = [
                            {"Time (s)": b, "Keep": True}
                            for b in existing_template["beats"]
                        ]
                        st.rerun()

                with col_b:
                    if st.button("🔁 Re-Detect From Scratch"):
                        st.session_state["beat_tpl_force_redetect"] = (
                            song_path.name
                        )
                        st.rerun()

            # ---------------------------------------------
            # No template yet (or user chose to edit/re-detect):
            # identify beats, show them, let the user rearrange
            # and confirm which points are real beats.
            # ---------------------------------------------

            else:

                st.session_state.pop("beat_tpl_force_redetect", None)

                st.subheader("🎧 Listen & Tap")

                st.caption(
                    "Play the song and tap the button in time with the "
                    "beat — like tapping along in CapCut. Space plays/"
                    "pauses, Enter taps. Every tap is read straight off "
                    "the audio's playback position."
                )

                render_tap_tool(song_path)

                tap_paste_key = f"tap_paste_{song_path.name}"

                tap_paste = st.text_area(
                    "Paste the tapped times from the box above here",
                    key=tap_paste_key,
                    height=80,
                    placeholder=(
                        "Click '🔎 Select All' above, copy (Ctrl/Cmd+C), "
                        "then paste here."
                    ),
                )

                col_load, col_auto = st.columns(2)

                with col_load:
                    if st.button(
                        "⬇️ Load Tapped Times Into Editor",
                        type="primary",
                        use_container_width=True,
                    ):
                        parsed = parse_time_list(tap_paste)

                        if not parsed:
                            st.error(
                                "No numbers found — tap some beats "
                                "above, select all, copy, and paste "
                                "them into the box first."
                            )
                        else:
                            st.session_state[draft_key] = [
                                {"Time (s)": t, "Keep": True}
                                for t in parsed
                            ]
                            st.rerun()

                with col_auto:
                    with st.popover(
                        "🤖 Auto-Detect Instead", use_container_width=True
                    ):
                        st.caption(
                            "Mechanical fallback (spectral flux onset "
                            "detection) if you'd rather not tap by hand."
                        )
                        redetect_spacing = st.number_input(
                            "Target seconds between points",
                            min_value=1.0,
                            max_value=10.0,
                            value=3.0,
                            step=0.5,
                            key=f"beat_tpl_spacing_{song_path.name}",
                        )
                        if st.button(
                            "Run Auto-Detection",
                            key=f"autodetect_{song_path.name}",
                        ):
                            with st.spinner("Identifying beats..."):
                                detected = detect_beat_cuts(
                                    song_path,
                                    min_spacing=redetect_spacing,
                                )
                            st.session_state[draft_key] = [
                                {"Time (s)": round(b, 2), "Keep": True}
                                for b in detected
                            ]
                            st.rerun()

                st.divider()

                if draft_key not in st.session_state:
                    st.info(
                        f"No beats loaded yet for `{song_path.name}`. "
                        "Tap along above (or use Auto-Detect) and load "
                        "them in to start editing."
                    )
                    st.stop()

                st.info(
                    f"Editing beats for `{song_path.name}` — untick "
                    "**Beat?** for anything that isn't a real beat, "
                    "use the blank row at the bottom to add a missed "
                    "one, or the trash icon to delete a row."
                )

                edited = st.data_editor(
                    st.session_state[draft_key],
                    num_rows="dynamic",
                    use_container_width=True,
                    column_config={
                        "Time (s)": st.column_config.NumberColumn(
                            "Time (s)",
                            min_value=0.0,
                            step=0.1,
                            format="%.2f",
                        ),
                        "Keep": st.column_config.CheckboxColumn(
                            "Beat?",
                            default=True,
                        ),
                    },
                    key=f"editor_{draft_key}",
                )

                st.session_state[draft_key] = edited

                template_name = st.text_input(
                    "Template name",
                    value=(
                        existing_template["template_name"]
                        if existing_template
                        else "Beat Template 1"
                    ),
                    key=f"tplname_{song_path.name}",
                )

                col_save, col_cancel = st.columns(2)

                with col_save:
                    if st.button(
                        "💾 Confirm & Save Beat Template",
                        type="primary",
                        use_container_width=True,
                    ):
                        confirmed_times = sorted(
                            {
                                round(float(row["Time (s)"]), 2)
                                for row in edited
                                if row.get("Keep")
                                and row.get("Time (s)") is not None
                            }
                        )

                        if not confirmed_times:
                            st.error(
                                "No beats confirmed — tick at least "
                                "one row's **Beat?** box before saving."
                            )
                        else:
                            saved = save_beat_template(
                                song_path,
                                confirmed_times,
                                template_name or "Beat Template 1",
                            )
                            st.session_state.pop(draft_key, None)
                            st.session_state.pop(
                                "beat_tpl_force_redetect", None
                            )
                            st.success(
                                f"Saved **{saved['template_name']}** "
                                f"({len(saved['beats'])} beats) for "
                                f"`{song_path.name}`."
                            )
                            st.rerun()

                with col_cancel:
                    if st.button(
                        "✖️ Discard Draft", use_container_width=True
                    ):
                        st.session_state.pop(draft_key, None)
                        st.session_state.pop(
                            "beat_tpl_force_redetect", None
                        )
                        st.rerun()


# ─────────────────────────────────────────────
# FORMAT 2 — FILTERS / EFFECTS
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# FORMAT 1 — ASSET POOLS
# ─────────────────────────────────────────────

elif page == "Asset Pools" and active_format == "Format 1":

    st.title("🗂️ Format 1 — Asset Pools")

    st.caption(
        "Clip and song libraries grouped by category."
    )

    st.divider()

    templates = load_format1_templates()

    for template in templates:

        clip_folder = format1_template_folder(template["slug"])
        song_folder = format1_template_song_folder(template["slug"])
        clip_count = len(list_video_files(clip_folder))

        if template["mode"] == MODE_STACK:
            song_count = len(list_video_files(song_folder))
            song_label = "Song clips (video)"
        else:
            song_count = len(list_audio_files(song_folder))
            song_label = "Song tracks (audio)"

        with st.container(border=True):
            col1, col2, col3 = st.columns([4, 1, 1])
            col1.write(f"**{template['name']}**")
            col1.caption(MODE_LABELS[template["mode"]])
            col2.metric("Clips", clip_count)
            col3.metric(song_label, song_count)


            if st.button(
                f"Choose {template['name']} and add assets",
                key=f"open_template_assets_{template['slug']}",
                type="primary",
            ):
                st.session_state["selected_f1_slug"] = template["slug"]
                st.session_state.pop("current_pairs_f1", None)
                goto_page("Assets")

    st.divider()

    st.subheader("➕ Add an Asset Pool")

    new_name = st.text_input(
        "Pool name",
        key="new_f1_template_name",
    )

    new_mode = st.radio(
        "Song media type",
        [MODE_STACK, MODE_BACKGROUND_AUDIO],
        format_func=lambda m: MODE_LABELS[m],
        key="new_f1_template_mode",
    )

    if st.button(
        "Add Asset Pool",
        type="primary",
        key="add_f1_template",
    ):
        if not new_name.strip():
            st.error("Give the template a name first.")
        elif any(
            t["name"].lower() == new_name.strip().lower()
            for t in templates
        ):
            st.error("A template with that name already exists.")
        else:
            existing_slugs = {t["slug"] for t in templates}
            slug = slugify_template_name(new_name.strip(), existing_slugs)
            new_template = _backfill_template_style(
                {
                    "name": new_name.strip(),
                    "slug": slug,
                    "mode": new_mode,
                }
            )
            templates.append(new_template)
            save_format1_templates(templates)
            format1_template_folder(slug)
            format1_template_song_folder(slug)
            st.success(f"Added **{new_name.strip()}**.")
            st.rerun()


# ─────────────────────────────────────────────
# FORMAT 1 — CROP PRESETS (manual drag/resize layout)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# FORMAT 1 — ASSETS
# ─────────────────────────────────────────────

elif page == "Assets" and active_format == "Format 1":

    st.title("📁 Format 1 Assets")

    st.write(
        "Add videos and songs to the selected template, then save each slot. "
        "Only this template\'s assets are shown."
    )

    st.write("CRE does not analyze, rank, or choose clips.")

    st.divider()

    templates = load_format1_templates()
    template_slugs = {template["slug"] for template in templates}

    if st.session_state.get("selected_f1_slug") not in template_slugs:
        st.info("Choose the format/template you want to edit before adding assets.")
        if st.button("Choose a format to edit", type="primary"):
            goto_page("Asset Pools")
        st.stop()

    any_assets_uploaded = False

    for template in templates:
        if template["slug"] != st.session_state.get("selected_f1_slug"):
            continue

        clip_folder = format1_template_folder(template["slug"])
        song_folder = format1_template_song_folder(template["slug"])
        stack_mode = template["mode"] == MODE_STACK

        st.subheader(f"🎬 {template['name']}")
        st.caption(MODE_LABELS[template["mode"]])

        col1, col2 = st.columns(2)

        with col1:
            st.write("1. " + template["name"].split(" + ")[0])
            show_pool_folder(clip_folder, label="Clips")

            clip_uploads = st.file_uploader(
                f"Upload clips for {template['name']}",
                type=VIDEO_TYPES,
                accept_multiple_files=True,
                key=f"f1_upload_clip_{template['slug']}",
            )

            if clip_uploads and st.button(
                f"Save Clips — {template['name']}",
                key=f"f1_save_clip_{template['slug']}",
            ):
                save_files_to_folder(clip_uploads, clip_folder)
                st.success(f"Saved {len(clip_uploads)} clip(s).")
                st.rerun()

        with col2:
            if stack_mode:
                st.markdown("**2. Song (actual video of the song)**")
                show_pool_folder(song_folder, label="Song clips")
                song_uploads = st.file_uploader(
                    f"Upload song footage for {template['name']}",
                    type=VIDEO_TYPES,
                    accept_multiple_files=True,
                    key=f"f1_upload_song_{template['slug']}",
                )
            else:
                st.markdown("**2. Song (background audio)**")
                st.metric(
                    "Song tracks",
                    len(list_audio_files(song_folder)),
                )
                if list_audio_files(song_folder):
                    with st.expander(
                        f"View {len(list_audio_files(song_folder))} files"
                    ):
                        for i, f in enumerate(
                            list_audio_files(song_folder), 1
                        ):
                            st.write(f"{i}. `{f.name}`")
                song_uploads = st.file_uploader(
                    f"Upload background songs for {template['name']}",
                    type=AUDIO_TYPES,
                    accept_multiple_files=True,
                    key=f"f1_upload_song_{template['slug']}",
                )

            if song_uploads and st.button(
                f"Save Song — {template['name']}",
                key=f"f1_save_song_{template['slug']}",
            ):
                save_files_to_folder(song_uploads, song_folder)
                st.success(f"Saved {len(song_uploads)} file(s).")
                st.rerun()


        if list_video_files(clip_folder) or (
            list_video_files(song_folder)
            if stack_mode
            else list_audio_files(song_folder)
        ):
            any_assets_uploaded = True

        st.divider()

    if any_assets_uploaded:
        if st.button(
            "Open Editor with these assets",
            type="primary",
            use_container_width=True,
            key="f1_goto_editor",
        ):
            with st.spinner("Preparing editor media..."):
                selected_slug = st.session_state["selected_f1_slug"]
                open_pool(APP_DIR, "Format 1/" + selected_slug,
                          [format1_template_folder(selected_slug)])
            goto_page("Editor")

        if st.button(
            "Replicate from saved template",
            use_container_width=True,
            key="f1_goto_render_queue",
        ):
            goto_page("Templates")


# ─────────────────────────────────────────────
# OPEN REEL EDITOR AND SAVED TEMPLATES
# ─────────────────────────────────────────────

elif page == "Editor":
    if st.sidebar.button("← Back to assets", use_container_width=True):
        goto_page("Assets")
    if active_format == "Format 1":
        reel_templates = load_format1_templates()
        reel_slugs = [t["slug"] for t in reel_templates]
        current_slug = st.session_state.get("selected_f1_slug")
        reel_slug = st.selectbox("Asset pool", reel_slugs,
            index=reel_slugs.index(current_slug) if current_slug in reel_slugs else 0,
            format_func=lambda slug: next(t["name"] for t in reel_templates if t["slug"] == slug))
        reel_folders = [format1_template_folder(reel_slug)]
        reel_scope = "Format 1/" + reel_slug
    else:
        reel_folders = [ASSETS_DIR / BUCKETS[key] for key in ("BMW", "LiveMap", "Format2Music")]
        reel_scope = "Format 2"
    render_pipeline(APP_DIR, reel_scope, reel_folders, goto_page)

elif page == "Templates":
    def reel_folders_for_scope(scope):
        if scope.startswith("Format 1/"):
            slug = scope.split("/", 1)[1]
            if slug not in {t["slug"] for t in load_format1_templates()}:
                return []
            return [format1_template_folder(slug)]
        return [ASSETS_DIR / BUCKETS[key] for key in ("BMW", "LiveMap", "Format2Music")]

    render_template_library(APP_DIR, active_format, reel_folders_for_scope,
                            lambda path: queue_for_auto_post(active_format, path), goto_page)


# ─────────────────────────────────────────────
# AUTO POST (shared across both formats)
# ─────────────────────────────────────────────

elif page == "Auto Post":

    st.title("📤 Auto Post")

    st.caption(
        "Connected to all formats — clips sent here from any "
        "Preview or template Results tab land in the same queue. Posting "
        "automation isn't built yet; this just tracks what's ready."
    )

    st.divider()

    queue = st.session_state.get("auto_post_queue", [])

    if not queue:

        st.info(
            "Nothing queued yet. Send clips here from either "
            "template's Results tab."
        )

    else:

        st.metric("Queued for posting", len(queue))

        for item in queue:

            with st.container(border=True):
                st.write(f"**{item['name']}**")
                st.caption(item["format"])
