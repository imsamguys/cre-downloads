"""Authenticated loopback companion for CRE Studio's Format 3 review workflow.

The public site owns sign-in and private project records. This process keeps
media and renders on the user's computer and never accepts unauthenticated work.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from cre_security import MAX_BYTES, SlidingWindowRateLimiter, UploadRejected, validate_upload_file


DATA_ROOT = Path(os.environ.get("CRE_DATA_DIR", Path(__file__).parent / "data")).resolve()
SUPABASE_URL = os.environ.get("CRE_SUPABASE_URL", "").rstrip("/")
PUBLISHABLE_KEY = os.environ.get("CRE_SUPABASE_PUBLISHABLE_KEY", "")
ALLOWED_ORIGINS = {origin.strip().rstrip("/") for origin in os.environ.get(
    "CRE_WEB_ORIGINS", "https://imsamguys.github.io,https://cre-cre2.vercel.app,https://cre-git-stage-cre2.vercel.app,http://127.0.0.1:5173,http://localhost:5173"
).split(",") if origin.strip()}
INSTANCE_ID = uuid4().hex
REQUEST_LIMIT = SlidingWindowRateLimiter(120, 60)
UPLOAD_LIMIT = SlidingWindowRateLimiter(10, 3600)
STATE_LOCK = threading.RLock()

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def _reply_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.middleware("http")
async def protect(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return _reply_error(403, "Origin not allowed")
    if not REQUEST_LIMIT.allow(request.client.host if request.client else "unknown"):
        return _reply_error(429, "Too many requests")
    if request.method == "OPTIONS":
        if not origin:
            return _reply_error(403, "Origin required")
        response = Response(status_code=204)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
    else:
        if request.url.path.endswith("/jobs") and request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdecimal():
                return _reply_error(411, "Content length required")
            if int(length) > MAX_BYTES["video"] + 1024 * 1024:
                return _reply_error(413, "Upload too large")
        response = await call_next(request)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    return response


def _supabase_json(path: str, token: str):
    if not SUPABASE_URL.startswith("https://") or not PUBLISHABLE_KEY.startswith("sb_publishable_"):
        raise HTTPException(503, "Local engine is not configured")
    request = UrlRequest(
        SUPABASE_URL + path,
        headers={"apikey": PUBLISHABLE_KEY, "Authorization": "Bearer " + token,
                 "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise HTTPException(401, "Sign in again") from None
        raise HTTPException(503, "Account service unavailable") from None
    except (URLError, TimeoutError, ValueError):
        raise HTTPException(503, "Account service unavailable") from None


def _identity(request: Request, project_id: UUID) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) > 8192:
        raise HTTPException(401, "Sign in required")
    token = header[7:]
    if not token or any(ch.isspace() for ch in token):
        raise HTTPException(401, "Sign in required")
    user = _supabase_json("/auth/v1/user", token)
    try:
        user_id = str(UUID(user["id"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(401, "Sign in again") from None
    query = urlencode({"id": "eq." + str(project_id), "select": "id,format"})
    projects = _supabase_json("/rest/v1/studio_projects?" + query, token)
    if not isinstance(projects, list) or len(projects) != 1 or projects[0].get("format") != "Format 3":
        raise HTTPException(404, "Project not found")
    return user_id


def _project_dir(user_id: str, project_id: UUID) -> Path:
    path = DATA_ROOT / user_id / str(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(project_dir: Path, job_id: UUID) -> Path:
    folder = project_dir / "api-jobs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (str(job_id) + ".json")


def _read_state(project_dir: Path, job_id: UUID) -> dict:
    try:
        state = json.loads(_state_path(project_dir, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "Job not found") from None
    if state.get("instance") != INSTANCE_ID and state.get("state") in {"preparing", "rendering"}:
        state["state"] = "failed"
        state["message"] = "The local engine stopped. Start a new job or retry the render."
        _write_state(project_dir, job_id, state)
    return state


def _write_state(project_dir: Path, job_id: UUID, state: dict) -> None:
    path = _state_path(project_dir, job_id)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _public_state(state: dict) -> dict:
    return {key: state[key] for key in ("id", "name", "state", "message", "updated_at", "summary") if key in state}


def _job_dir(project_dir: Path, job_id: UUID) -> Path:
    return project_dir / "format3" / "jobs" / str(job_id)


def _prepare(project_dir: Path, job_id: UUID, source: Path):
    import cre_format3 as f3

    with STATE_LOCK:
        state = _read_state(project_dir, job_id)
        state["state"] = "preparing"
        _write_state(project_dir, job_id, state)
    try:
        if not f3.FORMAT3_BRAIN_AVAILABLE:
            raise RuntimeError("Format 3 dependencies are unavailable")
        job, report = f3._prepare_new_job(project_dir, source, str(job_id))
        plan = f3._read_json(job / "edit-plan.json")
        summary = {
            "duration_seconds": round(plan["render"]["output"]["duration_frames"] / 30, 2),
            "segments": len(plan["render"]["segments"]),
            "captions": len(plan["render"]["captions"]),
            "warnings": [str(item)[:300] for item in plan.get("editorial", {}).get("warnings", [])[:10]],
            "validation": report.get("status", "unknown"),
        }
        with STATE_LOCK:
            state = _read_state(project_dir, job_id)
            state.update(state="review_ready", summary=summary,
                         message="Review the plan, then approve this exact version.")
            _write_state(project_dir, job_id, state)
    except Exception as exc:
        print("CRE preparation failed:", type(exc).__name__)
        with STATE_LOCK:
            state = _read_state(project_dir, job_id)
            state.update(state="failed", message="Could not prepare this source. Check the local engine log.")
            _write_state(project_dir, job_id, state)


def _render(project_dir: Path, job_id: UUID):
    import cre_format3 as f3
    from cre_plan_review import approved_snapshot

    try:
        job = _job_dir(project_dir, job_id)
        snapshot, _ = approved_snapshot(job)
        snapshot_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        approval = {"version": "1.0.0", "snapshot_sha256": snapshot_hash,
                    "approved_for_review_render": True,
                    "approval_id": "api-" + uuid4().hex,
                    "scope": "Local Format 3 review render only; no publishing."}
        approval_path = job / "review-render-approval.json"
        f3._write_json(approval_path, approval)
        snapshot_data = f3._read_json(snapshot)
        font_path = snapshot_data["execution"]["renderer_binding"]["caption_preset"]["font_binding"]["path"]
        output = f3._next_output(project_dir, job)
        report = f3.render_review(snapshot, approval_path, output, font_path=font_path)
        f3._write_json(job / "last-render-report.json", report)
        with STATE_LOCK:
            state = _read_state(project_dir, job_id)
            state.update(state="complete", output=str(output), message="Review render is ready.")
            _write_state(project_dir, job_id, state)
    except Exception as exc:
        print("CRE render failed:", type(exc).__name__)
        with STATE_LOCK:
            state = _read_state(project_dir, job_id)
            state.update(state="failed", message="Render failed. Check the local engine log.")
            _write_state(project_dir, job_id, state)


@app.get("/api/health")
def health():
    return {"ok": True, "configured": bool(SUPABASE_URL and PUBLISHABLE_KEY)}


@app.get("/api/projects/{project_id}/jobs")
def list_jobs(project_id: UUID, request: Request):
    user_id = _identity(request, project_id)
    project_dir = _project_dir(user_id, project_id)
    result = []
    for path in sorted((project_dir / "api-jobs").glob("*.json"), reverse=True) if (project_dir / "api-jobs").is_dir() else []:
        try:
            result.append(_public_state(_read_state(project_dir, UUID(path.stem))))
        except (HTTPException, ValueError):
            continue
    return {"jobs": result[:100]}


@app.post("/api/projects/{project_id}/jobs", status_code=202)
async def create_job(project_id: UUID, request: Request, tasks: BackgroundTasks,
                     source: UploadFile = File(), name: str = Form("New review")):
    user_id = _identity(request, project_id)
    if not UPLOAD_LIMIT.allow(user_id):
        raise HTTPException(429, "Please wait before uploading again")
    name = name.strip()
    if not 1 <= len(name) <= 100:
        raise HTTPException(400, "Use a name between 1 and 100 characters")
    project_dir = _project_dir(user_id, project_id)
    job_id = uuid4()
    source_dir = project_dir / "format3" / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    quarantine = project_dir / ".quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    temporary = quarantine / (str(job_id) + ".partial")
    size = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as target:
            while chunk := await source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES["video"]:
                    raise UploadRejected("Upload too large")
                digest.update(chunk)
                target.write(chunk)
        safe_name = validate_upload_file(temporary, source.filename or "", "video")
        destination = source_dir / (digest.hexdigest() + Path(safe_name).suffix)
        temporary.replace(destination)
    except UploadRejected:
        raise HTTPException(400, "Video upload failed validation") from None
    finally:
        temporary.unlink(missing_ok=True)
        await source.close()
    state = {"id": str(job_id), "name": name, "state": "queued",
             "instance": INSTANCE_ID, "message": "Preparing source for review."}
    with STATE_LOCK:
        _write_state(project_dir, job_id, state)
    tasks.add_task(_prepare, project_dir, job_id, destination)
    return _public_state(state)


@app.get("/api/projects/{project_id}/jobs/{job_id}")
def get_job(project_id: UUID, job_id: UUID, request: Request):
    user_id = _identity(request, project_id)
    return _public_state(_read_state(_project_dir(user_id, project_id), job_id))


@app.post("/api/projects/{project_id}/jobs/{job_id}/approve")
def approve_job(project_id: UUID, job_id: UUID, request: Request):
    user_id = _identity(request, project_id)
    project_dir = _project_dir(user_id, project_id)
    import cre_format3 as f3
    from cre_plan_review import approve, read

    with STATE_LOCK:
        state = _read_state(project_dir, job_id)
        if state["state"] != "review_ready":
            raise HTTPException(409, "Job is not ready for review")
        job = _job_dir(project_dir, job_id)
        try:
            plan = read(job / "edit-plan.json")
            caps = f3._capabilities_for_job(job)
            baseline = hashlib.sha256((job / "edit-plan.json").read_bytes()).hexdigest()
            approve(job, plan, caps, baseline)
        except (OSError, ValueError, KeyError):
            raise HTTPException(409, "Plan validation failed") from None
        state.update(state="approved", message="Approved for a local review render.")
        _write_state(project_dir, job_id, state)
        return _public_state(state)


@app.post("/api/projects/{project_id}/jobs/{job_id}/render", status_code=202)
def render_job(project_id: UUID, job_id: UUID, request: Request, tasks: BackgroundTasks):
    user_id = _identity(request, project_id)
    project_dir = _project_dir(user_id, project_id)
    with STATE_LOCK:
        state = _read_state(project_dir, job_id)
        if state["state"] != "approved":
            raise HTTPException(409, "Approve the review before rendering")
        state.update(state="rendering", message="Rendering on this computer.")
        _write_state(project_dir, job_id, state)
    tasks.add_task(_render, project_dir, job_id)
    return _public_state(state)


@app.get("/api/projects/{project_id}/jobs/{job_id}/download")
def download_job(project_id: UUID, job_id: UUID, request: Request):
    user_id = _identity(request, project_id)
    project_dir = _project_dir(user_id, project_id)
    state = _read_state(project_dir, job_id)
    if state["state"] != "complete":
        raise HTTPException(409, "Render is not ready")
    output = Path(state.get("output", "")).resolve()
    if not output.is_file() or project_dir.resolve() not in output.parents:
        raise HTTPException(404, "Render not found")
    return FileResponse(output, media_type="video/mp4", filename=f"cre-{job_id}.mp4")
