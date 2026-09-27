"""Local, token-protected storage bridge between CRE and Open Reel."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, urlencode
import uuid
import time
from cre_security import ALLOWED_EXTENSIONS, SlidingWindowRateLimiter, UploadRejected, validate_upload_bytes, validate_upload_file


class ReelBridge:
    def __init__(self, app_dir, editor_url="http://localhost:5173", cre_url="http://localhost:8502"):
        self.root = Path(app_dir).resolve() / "openreel_pipeline"
        self.editor_url = editor_url.rstrip("/")
        parsed_cre = urlsplit(cre_url)
        self.cre_url = f"{parsed_cre.scheme}://{parsed_cre.netloc}"
        self.origin = "{0.scheme}://{0.netloc}".format(urlsplit(self.editor_url))
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.rate_limiter = SlidingWindowRateLimiter(limit=240, window_seconds=60)
        self.sessions = {}
        for name in ("media", "templates", "renders", "sessions"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for path in (self.root / "sessions").glob("*.json"):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
                for job in session["jobs"]:
                    if job["status"] == "running":
                        job.update(status="failed", error="CRE restarted before this job finished")
                self.sessions[session["id"]] = session
            except (ValueError, OSError, KeyError):
                continue
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def end_headers(self):
                if self.headers.get("Origin") == bridge.origin:
                    self.send_header("Access-Control-Allow-Origin", bridge.origin)
                    self.send_header("Vary", "Origin")
                self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                super().end_headers()

            def reply(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                if self.headers.get("Origin") != bridge.origin:
                    return self.reply({"error": "Origin not allowed"}, 403)
                self.send_response(204)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CRE-Token")
                self.end_headers()

            def dispatch(self):
                if not bridge.rate_limiter.allow(self.client_address[0]):
                    return self.reply({"error": "Too many requests"}, 429)
                if self.headers.get("Origin") not in (None, bridge.origin):
                    return self.reply({"error": "Origin not allowed"}, 403)
                if not secrets.compare_digest(self.headers.get("X-CRE-Token", ""), bridge.token):
                    return self.reply({"error": "Invalid token"}, 401)
                try:
                    parts = urlsplit(self.path).path.strip("/").split("/")
                    method = self.command
                    if method == "GET" and parts[0] == "media" and len(parts) == 2:
                        path = bridge.media_path(parts[1])
                        self.send_response(200)
                        self.send_header("Content-Type", MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"))
                        self.send_header("Content-Length", str(path.stat().st_size))
                        self.end_headers()
                        with path.open("rb") as source:
                            shutil.copyfileobj(source, self.wfile)
                        return
                    if method == "PUT" and parts == ["media"]:
                        name = parse_qs(urlsplit(self.path).query).get("name", ["asset.bin"])[0]
                        suffix = Path(name.replace("\\", "/")).suffix.lower()
                        if suffix not in MEDIA_EXTENSIONS:
                            raise UploadRejected("Unsupported media file")
                        media_id = uuid.uuid4().hex + suffix
                        path = bridge.root / "media" / media_id
                        self.receive_file(path, name=name, kind="media")
                        return self.reply({"id": media_id, "name": Path(name.replace("\\", "/")).name})
                    if method == "GET" and parts[0] == "sessions" and len(parts) == 2:
                        with bridge.lock:
                            value = copy.deepcopy(bridge.sessions[parts[1]])
                        return self.reply(value)
                    if method == "POST" and parts == ["templates"]:
                        value = self.read_json()
                        return self.reply(bridge.save_template(value))
                    if parts[0] == "sessions" and len(parts) == 4 and parts[2] == "jobs":
                        session_id, job_id = parts[1], parts[3]
                        if not UUID_HEX.fullmatch(session_id) or not UUID_HEX.fullmatch(job_id):
                            raise ValueError("Invalid job ID")
                        with bridge.lock:
                            session = bridge.sessions[session_id]
                            job = next(j for j in session["jobs"] if j["id"] == job_id)
                            if method == "POST":
                                change = self.read_json()
                                if change.get("status") == "running" and job["status"] != "pending":
                                    return self.reply({"error": "Job already claimed"}, 409)
                                if change.get("status") not in ("running", "failed", "cancelled"):
                                    raise ValueError("Invalid status")
                                job.update(status=change["status"], error=str(change.get("error", ""))[:2000])
                                bridge.persist_session(session)
                                return self.reply(job)
                        if method == "PUT":
                            if job["status"] != "running":
                                raise ValueError("Job is not running")
                            path = bridge.root / "renders" / f"{session_id}_{job_id}.mp4"
                            self.receive_file(path, name=path.name, kind="video")
                            with bridge.lock:
                                job.update(status="complete", output=str(path), error="")
                                bridge.persist_session(session)
                            return self.reply({"status": "complete"})
                    return self.reply({"error": "Not found"}, 404)
                except (KeyError, StopIteration, FileNotFoundError):
                    self.reply({"error": "Not found"}, 404)
                except (ValueError, TypeError, UploadRejected):
                    self.reply({"error": "Request validation failed"}, 400)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.reply({"error": "Internal server error"}, 500)

            def read_json(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32 * 1024 * 1024:
                    raise ValueError("Invalid JSON size")
                return json.loads(self.rfile.read(length))

            def receive_file(self, path, *, name, kind):
                remaining = int(self.headers.get("Content-Length", "0"))
                if not 0 < remaining <= 2 * 1024 ** 3:
                    raise ValueError("Invalid file size")
                temporary = path.with_suffix(path.suffix + ".partial")
                try:
                    with temporary.open("wb") as target:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("Upload interrupted")
                            target.write(chunk)
                            remaining -= len(chunk)
                    validate_upload_file(temporary, name, kind)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)

            do_GET = dispatch
            do_POST = dispatch
            do_PUT = dispatch

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def media_path(self, media_id):
        if not isinstance(media_id, str) or Path(media_id).name != media_id or "/" in media_id or "\\" in media_id:
            raise ValueError("Invalid media ID")
        path = (self.root / "media" / media_id).resolve()
        if path.parent != self.root / "media" or not path.is_file():
            raise FileNotFoundError(media_id)
        return path

    def register(self, path):
        path = Path(path).resolve()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        media_id = digest.hexdigest() + path.suffix.lower()
        destination = self.root / "media" / media_id
        with self.lock:
            if not destination.exists():
                shutil.copy2(path, destination)
        return {"id": media_id, "name": path.name}

    def templates(self, scope=None):
        with self.lock:
            values = self._numbered_templates()
            return [value for value in values if scope is None or value.get("scope") == scope]

    def _numbered_templates(self):
        values = []
        paths = sorted((self.root / "templates").glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                values.append(value)
            except (ValueError, OSError):
                continue
        next_number = max((t.get("number", 0) for t in values), default=0) + 1
        for value in values:
            if not value.get("number"):
                value["number"] = next_number
                next_number += 1
                self._write_template(value)
        return sorted(values, key=lambda t: t["number"])

    def _write_template(self, value):
        template_id = value["id"]
        if len(template_id) != 32 or any(c not in "0123456789abcdef" for c in template_id):
            raise ValueError("Invalid template ID")
        path = self.root / "templates" / (template_id + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def template(self, template_id):
        return next((t for t in self.templates() if t["id"] == template_id), None)

    def set_slots(self, template_id, slots):
        with self.lock:
            value = self.template(template_id)
            if value is None:
                raise ValueError("Template not found")
            ids = {m["id"] for m in value["project"]["mediaLibrary"]["items"]}
            if len(set(slots)) != len(slots) or not set(slots).issubset(ids):
                raise ValueError("Invalid replaceable media")
            value["slots"] = list(slots)
            self._write_template(value)

    def register_upload(self, data, name):
        safe_name = validate_upload_bytes(bytes(data), name, "media")
        suffix = Path(safe_name).suffix.lower()
        media_id = hashlib.sha256(data).hexdigest() + suffix
        with self.lock:
            path = self.root / "media" / media_id
            if not path.exists():
                path.write_bytes(data)
        return {"id": media_id, "name": Path(name.replace("\\", "/")).name}

    def save_template(self, value):
        if value.get("version") != 1 or not str(value.get("name", "")).strip():
            raise ValueError("Template needs a name and version 1")
        project = value["project"]
        if not project["timeline"]["tracks"]:
            raise ValueError("Add clips to the timeline first")
        items = project["mediaLibrary"]["items"]
        ids = {item["id"] for item in items}
        slots = value.get("slots", [])
        if len(set(slots)) != len(slots) or not set(slots).issubset(ids):
            raise ValueError("Invalid replaceable media")
        for item in items:
            self.media_path(value["assets"][item["id"]]["id"])
        value = copy.deepcopy(value)
        value["id"] = uuid.uuid4().hex
        value["name"] = str(value["name"]).strip()[:120]
        with self.lock:
            existing = self._numbered_templates()
            value["number"] = max((t["number"] for t in existing), default=0) + 1
            value["createdAt"] = time.time()
            self._write_template(value)
        return {"id": value["id"], "name": value["name"], "number": value["number"],
                "url": f"{self.cre_url}/?" + urlencode({"reel_template": value["id"]})}

    def launch(self, scope, assets=None, template=None, replacements=None):
        session_id = uuid.uuid4().hex
        session = {"id": session_id, "scope": scope, "assets": assets or [],
                   "template": copy.deepcopy(template), "jobs": [], "mode": "edit", "createdAt": time.time()}
        if replacements is not None:
            if not template or not replacements:
                raise ValueError("Choose a template and footage")
            session["mode"] = "batch"
            session["jobs"] = [{"id": uuid.uuid4().hex, "status": "pending", "replacements": row}
                               for row in replacements]
        with self.lock:
            self.sessions[session_id] = session
            self.persist_session(session)
        return session_id, self.session_url(session_id)

    def session_url(self, session_id):
        params = urlencode({"creBridge": self.url, "creToken": self.token, "creSession": session_id})
        return f"{self.editor_url}/?{params}#/editor"

    def persist_session(self, session):
        if not UUID_HEX.fullmatch(session["id"]):
            raise ValueError("Invalid session ID")
        path = self.root / "sessions" / (session["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def batches(self, scope, template_id=None):
        with self.lock:
            return sorted([copy.deepcopy(s) for s in self.sessions.values()
                    if s["scope"] == scope and s["mode"] == "batch"
                    and (template_id is None or s["template"]["id"] == template_id)],
                    key=lambda s: s.get("createdAt", 0))

    def jobs(self, session_id):
        with self.lock:
            return copy.deepcopy(self.sessions.get(session_id, {}).get("jobs", []))


MEDIA_EXTENSIONS = ALLOWED_EXTENSIONS["media"]
UUID_HEX = re.compile(r"[0-9a-f]{32}")
MEDIA_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
