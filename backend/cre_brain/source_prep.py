"""Local source preparation for Format 3. No cloud service is used here."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from faster_whisper import WhisperModel
from cre_security import ALLOWED_EXTENSIONS


FFMPEG = Path(os.environ.get("CRE_FFMPEG", "ffmpeg"))
FFPROBE = Path(os.environ.get("CRE_FFPROBE", "ffprobe"))


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, encoding="utf-8", errors="replace", shell=False)


def _local_video(path: str | Path) -> Path:
    """Pass FFmpeg an existing absolute filename, never an option or protocol."""
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in ALLOWED_EXTENSIONS["video"] or not source.is_file():
        raise ValueError("Choose an existing local video file.")
    return source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_source(source_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    source_path, output_path = _local_video(source_path), Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Measure the actual source before applying gain; a fixed target offset is
    # not transferable between clips. Fail closed if measurement is unavailable.
    first_pass = _run([str(FFMPEG), "-hide_banner", "-i", str(source_path), "-af",
                       "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]).stderr
    measurements = re.findall(r"\{\s*\"input_i\".*?\}", first_pass, flags=re.S)
    if not measurements:
        raise ValueError("Source loudness measurement was unavailable.")
    measured_input = json.loads(measurements[-1])
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if not math.isfinite(float(measured_input[key])):
            raise ValueError("Source audio has no finite loudness measurement.")
    audio_filter = ("loudnorm=I=-16:TP=-1.5:LRA=11:linear=true"
                    f":measured_I={measured_input['input_i']}:measured_TP={measured_input['input_tp']}"
                    f":measured_LRA={measured_input['input_lra']}:measured_thresh={measured_input['input_thresh']}"
                    f":offset={measured_input['target_offset']},aresample=48000:async=1:first_pts=0")
    _run([str(FFMPEG), "-y", "-i", str(source_path), "-vf", "setpts=PTS-STARTPTS,fps=30,setsar=1",
          # Leave encoder headroom so decoded AAC reliably meets CRE's <= -1 dBTP contract.
          "-af", audio_filter, "-c:v", "libx264", "-preset", "medium",
          "-crf", "18", "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
          "-colorspace", "bt709", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
          "-ac", "2", "-movflags", "+faststart", str(output_path)])
    probe = json.loads(_run([str(FFPROBE), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output_path)]).stdout)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    duration_frames = round(float(video.get("duration") or probe["format"]["duration"]) * 30)
    measured = _run([str(FFMPEG), "-hide_banner", "-i", str(output_path), "-af",
                     "loudnorm=I=-16:TP=-1:LRA=11:print_format=json", "-f", "null", "-"]).stderr
    blocks = re.findall(r"\{\s*\"input_i\".*?\}", measured, flags=re.S)
    if not blocks:
        raise ValueError("Encoded audio loudness could not be verified.")
    loudness = json.loads(blocks[-1])
    integrated = float(loudness["input_i"])
    peak = float(loudness["input_tp"])
    if not math.isfinite(integrated) or not math.isfinite(peak) or abs(integrated + 16) > 0.25 or peak > -1:
        raise ValueError(f"Encoded audio is outside the delivery limits: {integrated:.2f} LUFS, {peak:.2f} dBTP.")
    return {"id": "source", "kind": "video", "sha256": _sha256(output_path),
            "duration_frames": duration_frames, "width_px": int(video["width"]),
            "height_px": int(video["height"]), "has_audio": True,
            "audio_sample_rate": int(audio["sample_rate"]), "fps": 30,
            "integrated_lufs_milli": round(integrated * 1000),
            "true_peak_mdbtp": round(peak * 1000)}


def transcribe_source(source_path: str | Path, model_size: str = "base") -> list[dict[str, Any]]:
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(source_path), word_timestamps=True, vad_filter=True)
    words = []
    for segment in segments:
        for word in segment.words or []:
            text = word.word.strip()
            if not text:
                continue
            start = max(0, math.floor(float(word.start) * 30))
            end = max(start + 1, math.ceil(float(word.end) * 30))
            if words and start < words[-1]["source_end_frame"]:
                start = words[-1]["source_end_frame"]
                end = max(start + 1, end)
            words.append({"text": text, "source_start_frame": start, "source_end_frame": end})
    return words


def detect_safe_silence(source_path: str | Path, words: list[dict[str, Any]], duration_frames: int) -> list[list[int]]:
    result = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(source_path),
                             "-af", "silencedetect=noise=-42dB:d=0.32", "-f", "null", "-"],
                            text=True, capture_output=True, encoding="utf-8", errors="replace")
    starts = [float(v) for v in re.findall(r"silence_start:\s*([0-9.]+)", result.stderr)]
    ends = [float(v) for v in re.findall(r"silence_end:\s*([0-9.]+)", result.stderr)]
    if len(ends) < len(starts):
        ends.append(duration_frames / 30)
    candidates = []
    for start_s, end_s in zip(starts, ends):
        lo, hi = max(0, math.floor(start_s * 30)), min(duration_frames, math.ceil(end_s * 30))
        if hi - lo < 10:
            continue
        intersects_word = any(lo < w["source_end_frame"] and w["source_start_frame"] < hi for w in words)
        if not intersects_word:
            candidates.append([lo, hi])
    return candidates


def prepare_source(source_path: str | Path, normalized_path: str | Path, model_size: str = "base") -> tuple[dict, dict]:
    normalized_path = Path(normalized_path)
    asset = normalize_source(source_path, normalized_path)
    words = transcribe_source(normalized_path, model_size=model_size)
    if not words:
        raise ValueError("Local transcription returned no words.")
    words[-1]["source_end_frame"] = min(words[-1]["source_end_frame"], asset["duration_frames"])
    safe = detect_safe_silence(normalized_path, words, asset["duration_frames"])
    analysis = {"version": "1.0.0", "source_asset_id": "source", "source_sha256": asset["sha256"],
                "fps": 30, "duration_frames": asset["duration_frames"], "verified": True,
                "alignment_id": f"faster-whisper-{model_size}-cpu-int8-v1",
                "words": words, "safe_silence_ranges": safe}
    registry = {"version": "1.0.0", "assets": {"source": {
        "path": str(normalized_path.resolve()), "sha256": asset["sha256"], "approved": True,
        "approval_id": "user-provided-local-source-v1",
        "normalization": {"recipe": "cre_audio_minus16_v1", "sha256": asset["sha256"],
                          "integrated_lufs_milli": asset["integrated_lufs_milli"],
                          "true_peak_mdbtp": asset["true_peak_mdbtp"],
                          "evidence_id": "ffmpeg-loudnorm-local-v1"}}}}
    asset.pop("integrated_lufs_milli", None); asset.pop("true_peak_mdbtp", None)
    return {"asset": asset, "analysis": analysis}, registry
