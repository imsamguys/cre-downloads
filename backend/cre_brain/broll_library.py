"""Local, resumable movie B-roll catalog and semantic search."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable

import numpy as np
from PIL import Image


FFMPEG = Path(os.environ.get("CRE_FFMPEG", "ffmpeg"))
FFPROBE = Path(os.environ.get("CRE_FFPROBE", "ffprobe"))
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MODEL_NAME = "ViT-B-32"
MODEL_WEIGHTS = "laion2b_s34b_b79k"
BROLL_PLAYBACK_SECONDS = 4.5
_MODEL_CACHE = None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", shell=False)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return value[:72] or "movie"


def _file_id(path: Path) -> str:
    stat = path.stat()
    token = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return _slug(path.stem)[:48] + "-" + hashlib.sha1(token).hexdigest()[:12]


def _probe(path: Path) -> dict:
    path = Path(path).resolve()
    if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
        raise ValueError("Choose an existing local movie clip.")
    data = json.loads(_run([str(FFPROBE), "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(path)]).stdout)
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    duration = float(video.get("duration") or data["format"].get("duration") or 0)
    return {"duration_seconds": duration, "width_px": int(video["width"]),
            "height_px": int(video["height"]), "size_bytes": path.stat().st_size}


def library_paths(app_dir: str | Path) -> dict[str, Path]:
    root = Path(app_dir) / "format3" / "broll-library"
    result = {"root": root, "catalog": root / "catalog", "index": root / "index.json",
              "vectors": root / "embeddings.npy", "config": root / "config.json"}
    result["catalog"].mkdir(parents=True, exist_ok=True)
    return result


def read_index(app_dir: str | Path) -> dict:
    path = library_paths(app_dir)["index"]
    if not path.is_file():
        return {"version": "1.0.0", "model": None, "sources": [], "scenes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def scan_folder(folder: str | Path) -> list[Path]:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Choose an existing folder containing movie clips.")
    return sorted((path for path in folder.rglob("*")
                   if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES),
                  key=lambda path: str(path).casefold())


def _make_proxy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file(): return
    temporary = target.with_suffix(".partial.mp4")
    _run([str(FFMPEG), "-y", "-i", str(source), "-map", "0:v:0", "-an",
          "-vf", "scale='min(960,iw)':-2:flags=lanczos,fps=12,setsar=1",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
          "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)])
    temporary.replace(target)


def _detect_scenes(proxy: Path, duration: float) -> list[tuple[float, float]]:
    from scenedetect import AdaptiveDetector, detect
    detected = detect(str(proxy), AdaptiveDetector(min_scene_len=12), show_progress=False)
    shots = [(max(0.0, start.get_seconds()), min(duration, end.get_seconds()))
             for start, end in detected]
    return shots or [(0.0, duration)]


def _candidate_windows(shots: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    windows = []
    for start, end in shots:
        center = (start + end) / 2
        candidate_start = max(0.0, min(max(0.0, duration - 3.0), center - 1.5))
        candidate_end = min(duration, candidate_start + 3.0)
        if candidate_end - candidate_start < 2.5: continue
        if not windows or abs(candidate_start - windows[-1][0]) >= 1.0:
            windows.append((candidate_start, candidate_end))
    return windows


def _thumbnail(proxy: Path, at_seconds: float, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file(): return
    _run([str(FFMPEG), "-y", "-ss", f"{at_seconds:.3f}", "-i", str(proxy),
          "-frames:v", "1", "-vf", "scale=480:-2:flags=lanczos", "-q:v", "3",
          "-update", "1", str(target)])


def ingest_folder(app_dir: str | Path, folder: str | Path,
                  progress: Callable[[str, float], None] | None = None) -> dict:
    progress = progress or (lambda message, fraction: None)
    paths = library_paths(app_dir); index = read_index(app_dir)
    known = {item["id"]: item for item in index["sources"]}
    files = scan_folder(folder)
    if not files: raise ValueError("No supported movie clips were found in that folder.")
    for number, source in enumerate(files, 1):
        source_id = _file_id(source)
        if source_id in known:
            progress(f"Already indexed: {source.name}", number / len(files)); continue
        progress(f"Creating proxy: {source.name}", (number - 1) / len(files))
        metadata = _probe(source)
        item_dir = paths["catalog"] / source_id
        proxy = item_dir / "proxy.mp4"
        _make_proxy(source, proxy)
        shots = _detect_scenes(proxy, metadata["duration_seconds"])
        scenes = []
        for scene_number, (start, end) in enumerate(_candidate_windows(shots, metadata["duration_seconds"]), 1):
            scene_id = f"{source_id}-scene-{scene_number:05d}"
            thumb = item_dir / "thumbs" / f"scene-{scene_number:05d}.jpg"
            _thumbnail(proxy, (start + end) / 2, thumb)
            scenes.append({"id": scene_id, "source_id": source_id, "source_path": str(source),
                           "source_name": source.name, "start_seconds": round(start, 3),
                           "end_seconds": round(end, 3), "duration_seconds": round(end-start, 3),
                           "thumbnail_path": str(thumb.resolve()), "proxy_path": str(proxy.resolve())})
        source_record = {"id": source_id, "path": str(source), "name": source.name,
                         "proxy_path": str(proxy.resolve()), "scene_count": len(scenes), **metadata}
        index["sources"].append(source_record); index["scenes"].extend(scenes)
        paths["index"].write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        progress(f"Indexed {source.name}: {len(scenes)} candidate scenes", number / len(files))
    return index


def _load_model():
    global _MODEL_CACHE
    if _MODEL_CACHE is not None: return _MODEL_CACHE
    import open_clip
    import torch
    model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=MODEL_WEIGHTS)
    model.eval()
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    _MODEL_CACHE=(model, preprocess, tokenizer, torch)
    return _MODEL_CACHE


def build_embeddings(app_dir: str | Path,
                     progress: Callable[[str, float], None] | None = None,
                     batch_size: int = 16) -> dict:
    progress = progress or (lambda message, fraction: None)
    paths = library_paths(app_dir); index = read_index(app_dir); scenes = index["scenes"]
    if not scenes: raise ValueError("Index at least one movie folder before building semantic search.")
    progress("Loading the local visual model (the first run downloads its free weights)...", 0.0)
    model, preprocess, _, torch = _load_model()
    batches=[]
    with torch.inference_mode():
        for offset in range(0, len(scenes), batch_size):
            current=scenes[offset:offset+batch_size]
            images=torch.stack([preprocess(Image.open(scene["thumbnail_path"]).convert("RGB")) for scene in current])
            vectors=model.encode_image(images)
            vectors=vectors/vectors.norm(dim=-1,keepdim=True)
            batches.append(vectors.cpu().numpy().astype("float32"))
            progress(f"Embedded {min(offset+len(current),len(scenes))} of {len(scenes)} scenes",
                     min(1.0,(offset+len(current))/len(scenes)))
    np.save(paths["vectors"],np.concatenate(batches,axis=0),allow_pickle=False)
    index["model"]={"name":MODEL_NAME,"weights":MODEL_WEIGHTS,"scene_count":len(scenes)}
    paths["index"].write_text(json.dumps(index,indent=2)+"\n",encoding="utf-8")
    return index


def search(app_dir: str | Path, query: str, limit: int = 12) -> list[dict]:
    paths=library_paths(app_dir); index=read_index(app_dir)
    if not paths["vectors"].is_file() or not index.get("model"):
        raise ValueError("Build the local semantic index first.")
    vectors=np.load(paths["vectors"],allow_pickle=False)
    if len(vectors)!=len(index["scenes"]):
        raise ValueError("The catalog changed. Rebuild semantic embeddings.")
    model, _, tokenizer, torch=_load_model()
    with torch.inference_mode():
        text_vector=model.encode_text(tokenizer([query]))
        text_vector=text_vector/text_vector.norm(dim=-1,keepdim=True)
    scores=vectors @ text_vector.cpu().numpy().astype("float32")[0]
    order=np.argsort(-scores)[:max(1,min(limit,len(scores)))]
    return [{**index["scenes"][int(i)],"score":round(float(scores[int(i)]),5)} for i in order]


def _expanded_start(scene: dict, media_path: str | Path,
                    duration: float = BROLL_PLAYBACK_SECONDS) -> float:
    media_duration=_probe(Path(media_path))["duration_seconds"]
    centered=float(scene["start_seconds"])-(duration-float(scene["duration_seconds"]))/2
    return max(0.0,min(centered,max(0.0,media_duration-duration)))


def motion_score(scene: dict, duration: float = BROLL_PLAYBACK_SECONDS) -> float:
    """Estimate visible motion across the expanded candidate window."""
    import cv2
    path=scene["proxy_path"]; start=_expanded_start(scene,path,duration)
    capture=cv2.VideoCapture(str(path)); previous=None; differences=[]
    for sample in range(10):
        at=start+duration*sample/9
        capture.set(cv2.CAP_PROP_POS_MSEC,at*1000)
        ok,frame=capture.read()
        if not ok: continue
        gray=cv2.resize(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),(160,90))
        if previous is not None:
            differences.append(float(cv2.absdiff(previous,gray).mean()))
        previous=gray
    capture.release()
    return round(sum(differences)/len(differences),3) if differences else 0.0


def export_scene(app_dir: str | Path, scene: dict, label: str = "selected") -> Path:
    target_dir=Path(app_dir)/"format3"/"media"/"broll"; target_dir.mkdir(parents=True,exist_ok=True)
    start=_expanded_start(scene,scene["source_path"])
    name=f"{_slug(label)[:32]}--{_slug(Path(scene['source_name']).stem)[:28]}--{start:.2f}s--4p5s-v2.mp4"
    target=target_dir/name
    if target.is_file(): return target
    _run([str(FFMPEG),"-y","-ss",f"{start:.3f}","-i",scene["source_path"],
          "-t",f"{BROLL_PLAYBACK_SECONDS:.3f}","-an","-vf","fps=30,setsar=1,format=yuv420p",
          "-c:v","libx264","-preset","medium","-crf","18","-color_primaries","bt709",
          "-color_trc","bt709","-colorspace","bt709","-x264-params",
          "colorprim=bt709:transfer=bt709:colormatrix=bt709","-movflags","+faststart",str(target)])
    return target


def preview_scene(app_dir: str | Path, scene: dict) -> Path:
    """Create a tiny cached candidate clip for one Gemini reranking request."""
    folder=library_paths(app_dir)["root"]/"rerank-cache"; folder.mkdir(parents=True,exist_ok=True)
    target=folder/(scene["id"]+"-4p5s-v2.mp4")
    if target.is_file(): return target
    start=_expanded_start(scene,scene["proxy_path"])
    _run([str(FFMPEG),"-y","-ss",f"{start:.3f}","-i",scene["proxy_path"],
          "-t",f"{BROLL_PLAYBACK_SECONDS:.3f}","-an","-vf","fps=8,scale=640:-2:flags=lanczos",
          "-c:v","libx264","-preset","veryfast","-crf","32","-pix_fmt","yuv420p",
          "-movflags","+faststart",str(target)])
    return target
