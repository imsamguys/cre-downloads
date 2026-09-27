"""Security primitives shared by CRE's Streamlit UI and local editor bridge."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time


DANGEROUS_EXTENSIONS = {
    ".php", ".php3", ".php4", ".php5", ".phtml", ".jsp", ".jspx", ".asp", ".aspx",
    ".cgi", ".pl", ".py", ".js", ".mjs", ".html", ".htm", ".svg", ".exe", ".dll",
    ".com", ".bat", ".cmd", ".ps1", ".sh", ".jar", ".war", ".msi", ".scr",
}
ALLOWED_EXTENSIONS = {
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"},
    "lut": {".cube"},
    "json": {".json"},
}
ALLOWED_EXTENSIONS["media"] = ALLOWED_EXTENSIONS["video"] | ALLOWED_EXTENSIONS["audio"] | ALLOWED_EXTENSIONS["image"]
MAX_BYTES = {
    "video": 2 * 1024**3,
    "audio": 512 * 1024**2,
    "image": 64 * 1024**2,
    "media": 2 * 1024**3,
    "lut": 8 * 1024**2,
    "json": 16 * 1024**2,
}


class UploadRejected(ValueError):
    """Raised when an uploaded file fails server-side validation."""


def _safe_basename(name: str) -> str:
    raw = str(name or "").replace("\\", "/").split("/")[-1]
    if not raw or raw in {".", ".."} or "\x00" in raw:
        raise UploadRejected("The uploaded file has an invalid name.")
    suffixes = {suffix.lower() for suffix in Path(raw).suffixes}
    if suffixes & DANGEROUS_EXTENSIONS:
        raise UploadRejected("Executable and server-script uploads are blocked.")
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", Path(raw).stem).strip(" .-")[:100] or "upload"
    suffix = Path(raw).suffix.lower()
    return stem + suffix


def _matches_signature(data: bytes, suffix: str) -> bool:
    head = data[:64]
    if suffix in {".mp4", ".mov", ".m4a"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if suffix in {".mkv", ".webm"}:
        return head.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".avi":
        return head.startswith(b"RIFF") and head[8:12] == b"AVI "
    if suffix == ".wav":
        return head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if suffix == ".aac":
        return len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0
    if suffix == ".flac":
        return head.startswith(b"fLaC")
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    return True


def validate_upload_bytes(data: bytes, name: str, kind: str = "media") -> str:
    """Validate bytes independently of the browser-supplied MIME type."""
    if kind not in ALLOWED_EXTENSIONS:
        raise ValueError("Unknown upload category")
    safe_name = _safe_basename(name)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS[kind]:
        raise UploadRejected(f"Files ending in {suffix or '(none)'} are not allowed here.")
    if not data:
        raise UploadRejected("Empty files are not allowed.")
    if len(data) > MAX_BYTES[kind]:
        raise UploadRejected(f"The file exceeds the {MAX_BYTES[kind] // 1024**2} MB limit.")
    if suffix == ".json":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadRejected("The file is not valid UTF-8 JSON.") from exc
        if not isinstance(value, (dict, list)):
            raise UploadRejected("JSON uploads must contain an object or array.")
    elif suffix == ".cube":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadRejected("The LUT must be UTF-8 text.") from exc
        if "\x00" in text or not re.search(r"(?m)^\s*LUT_(?:1D|3D)_SIZE\s+\d+\s*$", text):
            raise UploadRejected("The file is not a valid .cube LUT.")
    elif not _matches_signature(data, suffix):
        raise UploadRejected("The file contents do not match its extension.")
    return safe_name


def validate_upload_file(path: Path, name: str, kind: str = "media") -> str:
    """Validate a streamed file without loading large media files into memory."""
    path = Path(path)
    safe_name = _safe_basename(name)
    suffix = Path(safe_name).suffix.lower()
    if kind not in ALLOWED_EXTENSIONS or suffix not in ALLOWED_EXTENSIONS[kind]:
        raise UploadRejected("Unsupported upload type.")
    size = path.stat().st_size
    if not 0 < size <= MAX_BYTES[kind]:
        raise UploadRejected("Invalid file size.")
    if kind in {"json", "lut"}:
        return validate_upload_bytes(path.read_bytes(), name, kind)
    with path.open("rb") as source:
        head = source.read(64)
    if not _matches_signature(head, suffix):
        raise UploadRejected("The file contents do not match its extension.")
    return safe_name


def secure_save_upload(upload, destination_dir: Path, kind: str = "media", *, forced_name: str | None = None) -> Path:
    data = bytes(upload.getbuffer() if hasattr(upload, "getbuffer") else upload)
    source_name = forced_name or getattr(upload, "name", "upload")
    safe_name = validate_upload_bytes(data, source_name, kind)
    destination_dir = Path(destination_dir).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    quarantine = destination_dir.parent / ".quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    suffix = Path(safe_name).suffix.lower()
    digest = hashlib.sha256(data).hexdigest()[:12]
    final_name = safe_name if forced_name else f"{Path(safe_name).stem}-{digest}{suffix}"
    target = (destination_dir / final_name).resolve()
    if destination_dir not in target.parents:
        raise UploadRejected("Invalid upload destination.")
    fd, temporary_name = tempfile.mkstemp(prefix="upload-", suffix=".quarantine", dir=quarantine)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(target)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return target


def verify_stripe_signature(payload: bytes, signature_header: str, secret: str, tolerance_seconds: int = 300, now: int | None = None) -> bool:
    """Verify Stripe-style t=...,v1=... signatures. Fail closed on malformed input."""
    if not payload or not signature_header or not secret:
        return False
    fields: dict[str, list[str]] = defaultdict(list)
    for part in signature_header.split(","):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip()].append(value.strip())
    try:
        timestamp = int(fields["t"][0])
    except (KeyError, IndexError, ValueError):
        return False
    now = int(time.time()) if now is None else int(now)
    if abs(now - timestamp) > tolerance_seconds:
        return False
    expected = hmac.new(secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in fields.get("v1", []))


def safe_update(payload: dict, allowed_fields: set[str]) -> dict:
    """Prevent mass assignment by rejecting every unexpected field."""
    unexpected = set(payload) - set(allowed_fields)
    if unexpected:
        raise ValueError("Unexpected fields: " + ", ".join(sorted(unexpected)))
    return {key: payload[key] for key in allowed_fields if key in payload}


def require_record_owner(record_owner_id: str, authenticated_user_id: str) -> None:
    if not authenticated_user_id or not secrets.compare_digest(str(record_owner_id), str(authenticated_user_id)):
        raise PermissionError("Record access denied")


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Passwords must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            events = self._events[key]
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True
