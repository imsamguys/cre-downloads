"""Format 3 UI: AI planning, validation, queued review rendering, and preview."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import streamlit as st
from cre_caption_styles import resolved_caption_preset
from cre_plan_review import write as _versioned_write, approved_snapshot, render_plan_review, stamp
from cre_security import UploadRejected, secure_save_upload

try:
    from cre_brain import accept_plan
    from cre_brain.contract import load_contract, load_profile
    from cre_brain.coordinator import BotCoordinator, BotTeamError, roster as bot_roster
    from cre_brain.compiler.editorial import planning_context, compile_intent, resolve_selection, CompilationError
    from cre_brain.adapters.ffmpeg_v1 import ADAPTER_ID, ADAPTER_VERSION, CAPTION_LAYOUT_VERSION, SCALER_VERSION, render_review
    from cre_brain.providers import GeminiProvider, ProviderError, SpecialistResult
    from cre_brain.providers.openrouter import OpenRouterProvider, FREE_MODELS
    from cre_brain.source_prep import prepare_source
    from cre_brain.broll_library import (library_paths as broll_library_paths,
                                         read_index as read_broll_index,
                                         scan_folder as scan_broll_folder,
                                         ingest_folder as ingest_broll_folder,
                                         build_embeddings as build_broll_embeddings,
                                         search as search_broll_library,
                                         motion_score as broll_motion_score,
                                         export_scene as export_broll_scene,
                                         preview_scene as preview_broll_scene)
    from cre_brain.assets.probe import probe_media
    from cre_brain.compiler.snapshot import file_hash
    from cre_brain.face_placement import (detect_frame_face, choose_below_cheek_placement)
    FORMAT3_BRAIN_AVAILABLE = True
    FORMAT3_IMPORT_ERROR = None
except Exception as exc:
    FORMAT3_BRAIN_AVAILABLE = False
    FORMAT3_IMPORT_ERROR = str(exc)


FORMAT3_NAV = ["Sources", "B-Roll Library", "B-Rolls", "Music", "Sound Effects", "Captions", "Caption Library", "Filters", "Brain Plans", "Plan Review", "Render Queue", "Actually Rendering", "Preview", "Auto Post"]
BROLL_PLAYBACK_FRAMES = 135
REQUIRED_JOB_FILES = ("edit-plan.json", "asset-registry.json", "source-analysis.json", "renderer-capabilities.json")


def _dirs(app_dir):
    root = Path(app_dir) / "format3"
    paths = {"root": root, "sources": root / "sources", "jobs": root / "jobs", "renders": root / "renders",
             "broll": root / "media" / "broll", "music": root / "media" / "music",
             "sfx": root / "media" / "sfx", "luts": root / "media" / "luts",
             "prepared": root / "media" / "prepared"}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _slug(value):
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return value[:64] or "edit-plan-job"


def _contract_id(value):
    value = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
    if not value or not value[0].isalpha(): value = "id_" + value
    return value[:64]


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    _versioned_write(path, value)


def _media_settings(app_dir):
    path = _dirs(app_dir)["root"] / "media-settings.json"
    defaults = {"version":"1.1.0", "b_roll":{"enabled":True,"selected":[]},
                 "music":{"enabled":True,"selected":None,"gain_db":-10},
                 "sfx":{"enabled":True,"asset":None,"gain_db":10},
                 "text":{"enabled":True,"hook_text":"","cta_text":"follow for more"},
                 "captions":{"enabled":True,"mode":"normal","words_per_caption":1,
                             "adjust_all":False,"x_offset":0,"y_offset":0},
                 "filters":{"enabled":False,"selected":None}}
    if path.is_file():
        saved=_read_json(path)
        for key,value in defaults.items():
            if isinstance(value,dict): value.update(saved.get(key,{}) or {})
            elif key in saved: defaults[key]=saved[key]
        # Migrate the former start/end setup to the single B-roll-start effect.
        if not defaults["sfx"].get("asset"):
            defaults["sfx"]["asset"]=(defaults["sfx"].get("start_asset")
                                         or defaults["sfx"].get("end_asset"))
    return path,defaults


def _save_uploads(folder, uploads, kind):
    count=0
    for upload in uploads or []:
        secure_save_upload(upload, folder, kind); count+=1
    return count


def _media_files(folder, suffixes):
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in suffixes), key=lambda p:p.name.casefold())


def _jobs(app_dir):
    return sorted((p for p in _dirs(app_dir)["jobs"].iterdir() if p.is_dir()), key=lambda p: p.name.casefold())


def _job_ready(job):
    return all((job / name).is_file() for name in REQUIRED_JOB_FILES)


def _validate_job(job, write_snapshot=True):
    missing = [name for name in REQUIRED_JOB_FILES if not (job / name).is_file()]
    if missing:
        return None, {"status": "rejected", "render_authorized": False,
                      "issues": [{"code": "JOB_FILE_MISSING", "path": "/job", "message": ", ".join(missing)}]}
    capabilities = _capabilities_for_job(job)
    result = accept_plan((job / "edit-plan.json").read_bytes(), _read_json(job / "asset-registry.json"),
                         _read_json(job / "source-analysis.json"), capabilities)
    report = result.report()
    if result.accepted and write_snapshot:
        snapshot = job / "review-snapshot.json"
        snapshot.write_bytes(result.snapshot.json_bytes)
        report["snapshot_path"] = str(snapshot)
    _write_json(job / "validation-report.json", report)
    return result, report


def _capabilities_for_job(job, profile_id="PODCAST_HIGH_RETENTION_V1", profile_version="1.0.0"):
    capabilities = _read_json(job / "renderer-capabilities.json")
    if (job / "edit-plan.json").is_file():
        plan_profile = _read_json(job / "edit-plan.json").get("profile", {})
        profile_id = plan_profile.get("id", profile_id)
        profile_version = plan_profile.get("version", profile_version)
    _, profile_hash = load_profile(profile_id, profile_version)
    _, _, lock = load_contract()
    capabilities["schema_sha256"] = lock["schema.json"]
    capabilities["profile_sha256"] = profile_hash
    capabilities["renderer_id"] = ADAPTER_ID
    capabilities["renderer_version"] = ADAPTER_VERSION
    capabilities["scaler_version"] = SCALER_VERSION
    capabilities["layout_engine"] = CAPTION_LAYOUT_VERSION
    if "wide_frame_letterbox_v1" not in capabilities["operations"]:
        capabilities["operations"].append("wide_frame_letterbox_v1")
    for operation in ("caption_safe_zones_v1", "speaker_palette_v1", "caption_smooth_fade_v1",
                      "caption_face_anchor_v1", "caption_one_shot_face_stack_v1",
                      "b_roll_replace_v1", "music_amplitude_envelope_v1",
                      "sfx_linear_fades_v1", "top_label_v1"):
        if operation not in capabilities["operations"]:
            capabilities["operations"].append(operation)
    app_dir = job.parents[2]
    _, media_settings = _media_settings(app_dir)
    caption_settings = media_settings["captions"]
    style_id = "cheek_stack" if caption_settings.get("mode") == "stacked" else None
    preset = resolved_caption_preset(app_dir, "Format 3", style_id=style_id)
    if caption_settings.get("mode") != "stacked" and preset.get("layout_mode") == "one_shot_face_stack":
        preset = resolved_caption_preset(app_dir, "Format 3", style_id="black_shadow")
    font_path = Path(preset["font_binding"]["path"])
    preset["font_binding"]["sha256"] = hashlib.sha256(font_path.read_bytes()).hexdigest()
    for weight in preset.get("weight_fonts", {}).values():
        if isinstance(weight, dict) and weight.get("path"):
            weight_path = Path(weight["path"])
            weight["sha256"] = hashlib.sha256(weight_path.read_bytes()).hexdigest()
    plain_font = preset.get("plain_caption_preset", {}).get("font_binding")
    if plain_font:
        plain_font["sha256"] = hashlib.sha256(Path(plain_font["path"]).read_bytes()).hexdigest()
    capabilities["caption_preset"] = preset
    capabilities["font"] = {"asset_id": preset["font_binding"]["asset_id"],
                            "path": str(font_path), "sha256": preset["font_binding"]["sha256"]}
    plan = _read_json(job / "edit-plan.json") if (job / "edit-plan.json").is_file() else None
    capabilities["caption_runtime"] = {
        "enabled": bool(caption_settings.get("enabled", True)),
        "mode": caption_settings.get("mode", "normal"),
        "words_per_caption": int(caption_settings.get("words_per_caption", 1)),
        "speaker_roles": _speaker_roles_for_plan(job, plan) if plan else {},
    }
    filter_settings = media_settings["filters"]
    selected_lut = filter_settings.get("selected")
    lut_root = _dirs(app_dir)["luts"].resolve()
    lut_path = (lut_root / selected_lut).resolve() if isinstance(selected_lut, str) else None
    if filter_settings.get("enabled"):
        if not lut_path or lut_path.parent != lut_root or lut_path.suffix.lower() != ".cube" or not lut_path.is_file():
            raise ValueError("Filters are enabled, but the selected LUT is missing.")
        capabilities["filter_preset"] = {"enabled": True, "name": lut_path.name,
                                         "path": str(lut_path.resolve()),
                                         "sha256": file_hash(lut_path)}
    else:
        capabilities["filter_preset"] = {"enabled": False}
    return capabilities


def _gemini_key():
    return _provider_key("GEMINI_API_KEY")


def _provider_key(name):
    environment_value = os.environ.get(name, "").strip()
    if environment_value:
        return environment_value
    try:
        return str(st.secrets.get(name, "")).strip()
    except Exception:
        return ""


def _speaker_changes(job):
    """Return trusted transcript frame anchors proposed by Gemini's speaker director."""
    team_path = job / "bot-team-latest.json"
    source_path = job / "source-analysis.json"
    if not team_path.is_file() or not source_path.is_file():
        return []
    team = _read_json(team_path)
    run = next((item for item in team.get("runs", []) if item.get("bot_id") == "speaker_director"), None)
    ids = (run or {}).get("output", {}).get("decisions", {}).get("change_word_ids", [])
    words = _read_json(source_path).get("words", [])
    frames = []
    for word_id in ids:
        try:
            frames.append(words[int(word_id[2:])]["source_start_frame"])
        except (ValueError, IndexError, KeyError, TypeError):
            continue
    return sorted(set(frames))


def _caption_source_frame(render, caption):
    segment = next(s for s in render["segments"]
                   if s["output_start_frame"] <= caption["start_frame"]
                   and caption["end_frame"] <= s["output_start_frame"] + s["source_out_frame"] - s["source_in_frame"])
    return caption["start_frame"] + segment["source_in_frame"] - segment["output_start_frame"]


def _speaker_roles_for_plan(job, plan):
    if not plan:
        return {}
    changes = _speaker_changes(job)
    roles = {}
    for caption in plan.get("render", {}).get("captions", []):
        source_frame = _caption_source_frame(plan["render"], caption)
        speaker_index = sum(frame <= source_frame for frame in changes) % 2
        roles[caption["id"]] = f"speaker_{speaker_index + 1}"
    return roles


def _regroup_captions(render, words_per_caption, change_source_frames):
    """Create exact one/two-word cues without crossing a cut or speaker change."""
    words_per_caption = 1 if int(words_per_caption) == 1 else 2
    regrouped = []
    for segment in render["segments"]:
        segment_end = segment["output_start_frame"] + segment["source_out_frame"] - segment["source_in_frame"]
        words = [dict(word) for cue in render.get("captions", [])
                 if segment["output_start_frame"] <= cue["start_frame"] and cue["end_frame"] <= segment_end
                 for word in cue["words"]]
        cursor = 0
        while cursor < len(words):
            first = words[cursor]
            first_source = first["start_frame"] + segment["source_in_frame"] - segment["output_start_frame"]
            role = sum(frame <= first_source for frame in change_source_frames) % 2
            chunk = [first]
            while len(chunk) < words_per_caption and cursor + len(chunk) < len(words):
                candidate = words[cursor + len(chunk)]
                candidate_source = candidate["start_frame"] + segment["source_in_frame"] - segment["output_start_frame"]
                if sum(frame <= candidate_source for frame in change_source_frames) % 2 != role:
                    break
                chunk.append(candidate)
            regrouped.append({"start_frame": chunk[0]["start_frame"],
                              "end_frame": chunk[-1]["end_frame"], "words": chunk})
            cursor += len(chunk)
    if len(regrouped) > 120:
        raise ValueError("This excerpt contains more than 120 words; one-word captions exceed the validated render limit.")
    for index, caption in enumerate(regrouped, 1):
        caption["id"] = f"caption_{index}"
    render["captions"] = regrouped


def _apply_face_side_placements(job, plan, capabilities, words_per_caption_override=None):
    """Place all captions below the active speaker using one sampled frame per speaker role."""
    _, media_settings = _media_settings(job.parents[2])
    settings = media_settings["captions"]
    render = plan["render"]
    changes = _speaker_changes(job)
    words_per_caption = words_per_caption_override or int(settings.get("words_per_caption", 1))
    _regroup_captions(render, words_per_caption, changes)
    roles = _speaker_roles_for_plan(job, plan)
    capabilities["caption_runtime"].update({
        "enabled": bool(settings.get("enabled", True)),
        "mode": settings.get("mode", "normal"),
        "words_per_caption": words_per_caption,
        "speaker_roles": roles,
    })
    registry = _read_json(job / "asset-registry.json")
    source_id = plan["clip"]["source_asset_id"]
    source_path = Path(registry["assets"][source_id]["path"])
    model = job.parents[2] / "assets/models/face_detection_yunet_2023mar.onnx"
    preset = capabilities["caption_preset"]
    font_path = preset["font_binding"]["path"]
    font_size = preset["font_size_px"]
    line_height = preset["line_height_px"]
    x_offset = int(settings.get("x_offset", 0)) if settings.get("adjust_all") else 0
    y_offset = int(settings.get("y_offset", 0)) if settings.get("adjust_all") else 0
    faces = {}
    for caption in render["captions"]:
        role = roles.get(caption["id"], "speaker_1")
        if role not in faces:
            source_frame = _caption_source_frame(render, caption)
            faces[role] = detect_frame_face(source_path, source_frame / 30, model) if model.is_file() else None
        text = " ".join(word["text"] for word in caption["words"])
        placement = choose_below_cheek_placement(faces[role], text, font_path, font_size,
                                                 line_height, x_offset, y_offset)
        caption["caption_placement"] = placement.placement
    return plan


def _compile_candidate(plan, source, trusted_source_asset, profile, fallback_plan):
    """Compile model editorial choices into exact renderer timeline arithmetic."""
    plan = json.loads(json.dumps(plan))
    render = plan["render"]
    source_id = plan["clip"]["source_asset_id"]
    render["assets"] = [json.loads(json.dumps(trusted_source_asset))]
    render["assets"][0]["id"] = source_id

    cursor = 0
    for segment in render["segments"]:
        segment["output_start_frame"] = cursor
        cursor += segment["source_out_frame"] - segment["source_in_frame"]
    render["output"]["duration_frames"] = cursor

    src_w, src_h = trusted_source_asset["width_px"], trusted_source_asset["height_px"]
    crop_h = src_h - (src_h % 2)
    while crop_h > 0 and (crop_h * 9) % 16:
        crop_h -= 2
    crop_w = crop_h * 9 // 16
    if crop_w > src_w:
        crop_w = src_w - (src_w % 2)
        while crop_w > 0 and (crop_w * 16) % 9:
            crop_w -= 2
        crop_h = crop_w * 16 // 9
    prior_crops = {r.get("segment_id"): r.get("crop", {}) for r in render.get("reframes", [])}
    reframes = []
    for index, segment in enumerate(render["segments"], 1):
        prior = prior_crops.get(segment["id"], {})
        if prior.get("width_px", 0) < 32 or prior.get("height_px", 0) < 32:
            center_x, center_y = src_w // 2, src_h // 2
        else:
            center_x = prior.get("x_px", (src_w - crop_w) // 2) + prior.get("width_px", crop_w) // 2
            center_y = prior.get("y_px", (src_h - crop_h) // 2) + prior.get("height_px", crop_h) // 2
        x = max(0, min(src_w - crop_w, center_x - crop_w // 2)); x -= x % 2
        y = max(0, min(src_h - crop_h, center_y - crop_h // 2)); y -= y % 2
        reframes.append({"id": f"reframe_{index}", "segment_id": segment["id"],
                         "crop": {"x_px": x, "y_px": y, "width_px": crop_w, "height_px": crop_h}})
    render["reframes"] = reframes

    limits = profile["hard_limits"]
    emphasis_tokens = []
    for cue in render.get("captions", []):
        for word in cue.get("words", []):
            if word.get("emphasis"):
                token = word.get("text", "")
                start, end = 0, len(token)
                while start < end and not (token[start].isalnum() or token[start] == "_"):
                    start += 1
                while end > start and not (token[end - 1].isalnum() or token[end - 1] == "_"):
                    end -= 1
                emphasis_tokens.append(token[start:end].casefold())
    mapped_by_segment = []
    for segment in render["segments"]:
        offset = segment["output_start_frame"] - segment["source_in_frame"]
        mapped_by_segment.append([
            {"text": word["text"], "start_frame": word["source_start_frame"] + offset,
             "end_frame": word["source_end_frame"] + offset, "emphasis": False}
            for word in source["words"]
            if segment["source_in_frame"] <= word["source_start_frame"]
            and word["source_end_frame"] <= segment["source_out_frame"]
        ])

    def valid_chunk(words):
        duration = words[-1]["end_frame"] - words[0]["start_frame"]
        chars = len(" ".join(w["text"] for w in words))
        return (limits["min_caption_frames"] <= duration <= limits["max_caption_frames"]
                and len(words) <= limits["max_words_per_caption"]
                and chars <= limits["max_caption_characters"]
                and chars * 30 <= limits["max_caption_characters_per_second"] * duration)

    def partition(words):
        memo = {}
        def solve(index):
            if index == len(words): return []
            if index in memo: return memo[index]
            for count in range(min(limits["max_words_per_caption"], len(words) - index), 0, -1):
                chunk = words[index:index + count]
                if valid_chunk(chunk):
                    rest = solve(index + count)
                    if rest is not None:
                        memo[index] = [chunk] + rest; return memo[index]
            memo[index] = None; return None
        return solve(0)

    chunks = []
    for words in mapped_by_segment:
        grouped = partition(words)
        if grouped is None:
            scaffold = json.loads(json.dumps(fallback_plan))
            if isinstance(plan.get("editorial"), dict):
                scaffold["editorial"] = plan["editorial"]
            return scaffold, "trusted_execution_scaffold"
        chunks.extend(grouped)
    emphasis_budget = len([w for chunk in chunks for w in chunk]) * limits["max_emphasized_word_ratio_milli"] // 1000
    emphasized = 0
    captions = []
    for index, chunk in enumerate(chunks, 1):
        for word in chunk:
            token = re.sub(r"^\W+|\W+$", "", word["text"]).casefold()
            if emphasized < emphasis_budget and token in emphasis_tokens:
                word["emphasis"] = True; emphasized += 1; break
        captions.append({"id": f"caption_{index}", "start_frame": chunk[0]["start_frame"],
                         "end_frame": chunk[-1]["end_frame"], "words": chunk})
    render["captions"] = captions
    return plan, "compiled_ai_timeline"


def _generate_gemini_plan(job, model):
    schema = _read_json(Path(__file__).parent / "cre_brain" / "data" / "schema.json")
    source = _read_json(job / "source-analysis.json")
    registry = _read_json(job / "asset-registry.json")
    capabilities = _capabilities_for_job(job)
    planning_capabilities = json.loads(json.dumps(capabilities))
    existing_plan = _read_json(job / "edit-plan.json")
    profile, _ = load_profile(existing_plan["profile"]["id"], existing_plan["profile"]["version"])
    source_asset_id = existing_plan.get("clip", {}).get("source_asset_id")
    source_asset = next((a for a in existing_plan.get("render", {}).get("assets", [])
                         if a.get("id") == source_asset_id), None)
    planning_capabilities["planning_context"] = {"source_asset_metadata": source_asset,
                                                  "editorial_intent": planning_context(source, profile)}
    context_name = "editorial-context-" + stamp() + ".json"
    _write_json(job / context_name, planning_capabilities["planning_context"]["editorial_intent"])
    provider = GeminiProvider(_gemini_key(), model=model)
    source_entry=registry.get("assets",{}).get(source_asset_id,{})
    if source_entry.get("path"):
        provider.set_source_video(source_entry["path"])
    attempts = []
    correction = None
    accepted = None
    result = None
    for attempt_number in (1, 2, 3):
        generated = provider.generate_plan(schema=schema, profile=profile, source_analysis=source,
                                           asset_registry=registry, renderer_capabilities=planning_capabilities,
                                           correction=correction)
        original_name = "gemini-original-" + stamp() + ".json"
        _write_json(job / original_name, generated.plan)
        compiler_report = {"compiler_mode": "compiled_editorial_intent_v1", "fallback_used": False}
        try:
            resolved_intent = resolve_selection(generated.plan, planning_capabilities["planning_context"]["editorial_intent"])
            compiled_plan, compiler_report = compile_intent(resolved_intent, source, source_asset, profile, existing_plan)
            _apply_face_side_placements(job, compiled_plan, capabilities)
            compiler_report["window_id"] = generated.plan["window_id"]
            candidate_bytes = (json.dumps(compiled_plan, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            accepted = accept_plan(candidate_bytes, registry, source, capabilities)
            report = accepted.report()
        except CompilationError as exc:
            accepted = None
            report = {"status": "rejected", "render_authorized": False, "issues": exc.issues}
        compiler_mode = compiler_report["compiler_mode"]
        attempts.append({"attempt": attempt_number, "original_file": original_name, "latency_ms": generated.latency_ms,
                         "usage": generated.usage, "response_id": generated.response_id,
                         "compiler_mode": compiler_mode, "compiler_report": compiler_report,
                         "validation_status": report.get("status"), "issues": report.get("issues", [])})
        result = generated
        if accepted and accepted.accepted:
            result = type(generated)(plan=compiled_plan, provider=generated.provider, model=generated.model,
                                     latency_ms=generated.latency_ms, usage=generated.usage,
                                     response_id=generated.response_id)
            break
        correction = {"candidate": generated.plan, "issues": report.get("issues", [])}
    generation = {"version": "1.0.0", "provider": result.provider, "model": result.model,
                  "generated_at": datetime.now(timezone.utc).isoformat(), "attempts": attempts,
                  "planning_context_file": context_name,
                  "accepted": bool(accepted and accepted.accepted)}
    _write_json(job / ("ai-generation.json" if generation["accepted"] else "ai-generation-rejected-" + stamp() + ".json"), generation)
    if not accepted or not accepted.accepted:
        _write_json(job / "ai-candidate.rejected.json", result.plan)
        return None, report, generation
    old_plan = job / "edit-plan.json"
    if old_plan.exists():
        backup = job / ("edit-plan.backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json")
        shutil.copy2(old_plan, backup)
    _write_json(old_plan, result.plan)
    snapshot = job / "review-snapshot.json"
    snapshot.write_bytes(accepted.snapshot.json_bytes)
    report = accepted.report(); report["snapshot_path"] = str(snapshot)
    _write_json(job / "validation-report.json", report)
    return result.plan, report, generation


def _generate_gemini_bot_plan(job, model, on_event=None, provider_name="Gemini"):
    """Run specialist proposals, then compile and validate one safe edit plan."""
    app_dir = job.parents[2]
    source = _read_json(job / "source-analysis.json")
    registry = _read_json(job / "asset-registry.json")
    capabilities = _capabilities_for_job(job)
    existing_plan = _read_json(job / "edit-plan.json")
    profile, _ = load_profile(existing_plan["profile"]["id"], existing_plan["profile"]["version"])
    source_asset_id = existing_plan.get("clip", {}).get("source_asset_id")
    source_asset = next((asset for asset in existing_plan.get("render", {}).get("assets", [])
                         if asset.get("id") == source_asset_id), None)
    if source_asset is None:
        raise BotTeamError("The prepared source asset is missing from the current plan.")
    context = planning_context(source, profile)
    _,media_settings=_media_settings(app_dir); media_paths=_dirs(app_dir)
    context["render_controls"] = {
        "b_roll_enabled": bool(media_settings["b_roll"].get("enabled", True)),
        "captions_enabled": bool(media_settings["captions"].get("enabled", True)),
        "caption_mode": media_settings["captions"].get("mode", "normal"),
        "words_per_caption": int(media_settings["captions"].get("words_per_caption", 1)),
    }
    local_broll_ready=bool(read_broll_index(app_dir).get("model"))
    context["asset_catalog"]={
        # When the local movie index is ready, let the director describe the
        # transcript literally instead of anchoring it to an old selection.
        "b_roll":[] if local_broll_ready else
                 [{"id":name,"filename":name} for name in media_settings["b_roll"].get("selected",[])
                  if (media_paths["broll"]/name).is_file()],
        "music":[{"id":media_settings["music"].get("selected"),"filename":media_settings["music"].get("selected")}]
                if media_settings["music"].get("selected") else [],
        "sfx":[{"id":media_settings["sfx"].get("asset"),"filename":media_settings["sfx"].get("asset")}]
              if media_settings["sfx"].get("asset") else []}
    reference_path = app_dir / "format3" / "references" / "claude-quality-target-v1" / "reference-spec.md"
    reference = reference_path.read_text(encoding="utf-8") if reference_path.is_file() else ""
    provider = (OpenRouterProvider(_provider_key("OPENROUTER_API_KEY"), model=model)
                if provider_name == "OpenRouter (free)" else GeminiProvider(_gemini_key(), model=model))
    provider.set_source_video(registry["assets"][source_asset_id]["path"])
    for item in context["asset_catalog"]["b_roll"]:
        provider.add_broll_video(media_paths["broll"]/item["filename"],item["id"])
    coordinator = BotCoordinator(provider, on_event=on_event)
    team = coordinator.run(context, profile, reference=reference)
    rerank_run = _rerank_local_brolls(app_dir,provider,team,source.get("words",[]))
    if rerank_run:
        team["runs"].append(rerank_run)
        team.get("pending",{}).pop("asset_researcher",None)

    run_id = stamp()
    run_dir = job / "bot-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    for run in team["runs"]:
        _write_json(run_dir / (run["bot_id"] + ".json"), run)
    _write_json(run_dir / "combined-intent.json", team["combined_intent"])

    compiler_report = {"compiler_mode": "compiled_editorial_intent_v1", "fallback_used": False}
    try:
        resolved = resolve_selection(team["combined_intent"], context)
        compiled_plan, compiler_report = compile_intent(resolved, source, source_asset, profile, existing_plan)
        _apply_face_side_placements(job, compiled_plan, capabilities)
        compiler_report["window_id"] = team["selected_window_id"]
        candidate_bytes = (json.dumps(compiled_plan, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        accepted = accept_plan(candidate_bytes, registry, source, capabilities)
        report = accepted.report()
    except CompilationError as exc:
        accepted = None
        compiled_plan = None
        report = {"status": "rejected", "render_authorized": False, "issues": exc.issues}

    team["run_id"] = run_id
    team["run_folder"] = str(run_dir)
    team["compiler_report"] = compiler_report
    team["validation_status"] = report.get("status")
    team["validation_issues"] = report.get("issues", [])
    team["accepted"] = bool(accepted and accepted.accepted)
    _write_json(run_dir / "team-run.json", team)
    _write_json(job / "bot-team-latest.json", team)
    if not team["accepted"]:
        _write_json(job / ("bot-team-rejected-" + stamp() + ".json"), team)
        return None, report, team

    old_plan = job / "edit-plan.json"
    if old_plan.exists():
        shutil.copy2(old_plan, job / ("edit-plan.backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json"))
    _write_json(old_plan, compiled_plan)
    snapshot = job / "review-snapshot.json"
    snapshot.write_bytes(accepted.snapshot.json_bytes)
    report = accepted.report(); report["snapshot_path"] = str(snapshot)
    _write_json(job / "validation-report.json", report)
    return compiled_plan, report, team


def _rerank_local_brolls(app_dir,provider,team,source_words=None):
    """Use local retrieval first, then spend one Gemini call on a tiny shortlist."""
    _, media_settings = _media_settings(app_dir)
    if not media_settings["b_roll"].get("enabled", True):
        return None
    index=read_broll_index(app_dir)
    if not index.get("model"): return None
    broll_run=next((run for run in team.get("runs",[]) if run.get("bot_id")=="broll_director"),None)
    events=(broll_run or {}).get("output",{}).get("decisions",{}).get("events",[])[:2]
    if not events: return None
    source_words=source_words or []
    word_positions={word.get("id") or f"w_{i:05d}":i for i,word in enumerate(source_words)}
    candidates=[]; choices_by_event={}; spoken_by_event={}; forced_by_event={}
    for event_index,event in enumerate(events):
        anchor=word_positions.get(event.get("anchor_word_id"))
        spoken_context=""
        if anchor is not None:
            spoken_context=" ".join(word.get("text","") for word in
                                     source_words[max(0,anchor-12):anchor+18]).strip()
        spoken_by_event[event_index]=spoken_context
        query=event["query"]
        source_description_terms=("studio","microphone","hoodie","sitting","talking","podcast")
        # A B-roll query occasionally describes the source shot instead of the
        # idea being discussed. In that case, retrieve from the stated visual
        # purpose so the result adds information instead of another talking head.
        search_text=f'{spoken_context}. {event["purpose"]}' if any(term in query.lower() for term in source_description_terms) \
                    else f'{spoken_context}. {query}. {event["purpose"]}'
        hazardous=any(term in spoken_context.lower() for term in ("bomb","weapon","kill everyone","warfare"))
        if hazardous:
            search_text=("person assembling a weapon or dangerous machine, firearm construction, "
                         "threatening military technology, hazardous machinery. "+spoken_context)
        matches=search_broll_library(app_dir,search_text,16)
        talking_head_sources=("wall-street-greed-is-good","true-detective-monologue",
                              "good-will-hunting-park-bench","any-given-sunday-inches",
                              "fight-club-plane")
        visual_matches=[match for match in matches
                        if not any(token in match["source_name"].lower() for token in talking_head_sources)]
        if visual_matches:
            matches=visual_matches
        for match in matches:
            match["motion_score"]=broll_motion_score(match)
        moving=[match for match in matches if match["motion_score"]>=8.0]
        pool=moving or matches
        matches=sorted(pool,key=lambda item:item["score"]+min(item["motion_score"],40.0)*0.003,
                       reverse=True)[:6]
        # Hazard language benefits from the dedicated CLIP query above. Keep its
        # strongest literal visual match instead of allowing a mood-based LLM
        # choice (for example, generic training) to override it.
        if hazardous and matches:
            forced_by_event[event_index]=max(matches,key=lambda item:item["score"])
        choices_by_event[event_index]=[match["id"] for match in matches]
        for match in matches:
            if not any(item["id"]==match["id"] for item in candidates): candidates.append(match)
    if not candidates: return None
    visual_rerank = getattr(provider, "supports_visual_input", True)
    if visual_rerank:
        for candidate in candidates:
            provider.add_broll_video(preview_broll_scene(app_dir,candidate),candidate["id"])
    candidate_ids=[item["id"] for item in candidates]
    schema={"type":"object","additionalProperties":False,
            "required":["bot_id","summary","confidence_milli","warnings","selections"],
            "properties":{"bot_id":{"const":"broll_reranker"},
                          "summary":{"type":"string","minLength":1,"maxLength":500},
                          "confidence_milli":{"type":"integer","minimum":0,"maximum":1000},
                          "warnings":{"type":"array","maxItems":4,"items":{"type":"string","minLength":1,"maxLength":240}},
                          "selections":{"type":"array","minItems":len(events),"maxItems":len(events),
                            "items":{"type":"object","additionalProperties":False,
                              "required":["event_index","candidate_id","literal_match_milli","reason"],
                              "properties":{"event_index":{"type":"integer","minimum":0,"maximum":len(events)-1},
                                            "candidate_id":{"type":"string","enum":candidate_ids},
                                            "literal_match_milli":{"type":"integer","minimum":0,"maximum":1000},
                                            "reason":{"type":"string","minLength":1,"maxLength":300}}}}}}
    payload={"podcast_broll_events":[{"event_index":i,"spoken_context":spoken_by_event[i],
                                      "query":e["query"],"purpose":e["purpose"],
                                      "allowed_candidate_ids":choices_by_event[i]} for i,e in enumerate(events)],
             "candidates":[{"id":c["id"],"movie":c["source_name"],"start_seconds":c["start_seconds"],
                            "local_similarity":c["score"],"motion_score":c["motion_score"]} for c in candidates]}
    if visual_rerank:
        result=provider.run_specialist(bot_id="broll_reranker",
            instructions="Compare the attached podcast source and attached 4.5-second candidate clips. Select exactly one allowed movie candidate for each event. Treat spoken_context as the controlling meaning. The scene must literally show its concrete action, object, consequence, and emotional direction. For example, 'building a thing that could kill everyone' requires dangerous construction, a weapon, threatening machinery, or another visibly hazardous creation; cheerful startup collaboration contradicts it. Score literal_match_milli strictly. Reject loose metaphor, generic training, unrelated dramatic footage, and candidates selected only because their mood matches. Reject another person speaking, an interview, a microphone, or a podium unless the spoken context is specifically about a public speech. Require meaningful physical motion. Prefer distinct source movies, semantic relevance, cinematic composition, and clean motion. Avoid dialogue-dependent shots and repeated candidates.",
            input_data=payload,response_schema=schema)
    else:
        # Text-only models cannot verify footage. Keep the local visual retrieval ranking.
        result = SpecialistResult(provider="local_openclip", model="local-library-index", latency_ms=0,
            output={"bot_id": "broll_reranker", "summary": "Local B-roll retrieval; visual review required.",
                    "confidence_milli": 0, "warnings": ["GLM cannot inspect footage. Review local B-roll choices before rendering."],
                    "selections": []})
    by_id={item["id"]:item for item in candidates}; selected=[]
    for event_index in range(len(events)):
        choice=next((item for item in result.output["selections"] if item["event_index"]==event_index
                     and item["candidate_id"] in choices_by_event[event_index]),None)
        if visual_rerank and event_index in forced_by_event:
            forced=forced_by_event[event_index]
            if choice is None:
                choice={"event_index":event_index}
                result.output["selections"].append(choice)
            choice.update({"candidate_id":forced["id"],"literal_match_milli":1000,
                           "reason":"Hazard-specific local retrieval selected the strongest literal weapon or dangerous-machinery match."})
        if choice and choice.get("literal_match_milli",0)<700:
            continue
        if not choices_by_event[event_index]:
            continue
        scene=by_id[choice["candidate_id"]] if choice else by_id[choices_by_event[event_index][0]]
        exported=export_broll_scene(app_dir,scene,events[event_index]["query"])
        if exported.name not in selected: selected.append(exported.name)
    settings_path,settings=_media_settings(app_dir)
    settings["b_roll"]["selected"] = selected[:2]
    _write_json(settings_path,settings)
    return {"bot_id":"broll_reranker","status":"complete","provider":result.provider,"model":result.model,
            "latency_ms":result.latency_ms,"usage":result.usage,"response_id":result.response_id,
            "output":result.output}


def _build_seed_plan(job, prepared, registry, capabilities, profile):
    source = prepared["analysis"]
    fields = ("id", "kind", "sha256", "duration_frames", "width_px", "height_px", "has_audio")
    asset = {k: prepared["asset"][k] for k in fields}
    words = source["words"]
    limits = profile["hard_limits"]
    candidates = []
    for left in range(len(words)):
        if words[left]["source_start_frame"] < 2:
            continue
        start = words[left]["source_start_frame"] - 2
        if left and start < words[left - 1]["source_end_frame"]:
            continue
        for right in range(left, len(words)):
            end = words[right]["source_end_frame"] + 2
            if end > asset["duration_frames"]:
                break
            if right + 1 < len(words) and end > words[right + 1]["source_start_frame"]:
                continue
            duration = end - start
            if limits["min_duration_frames"] <= duration <= limits["max_duration_frames"]:
                candidates.append((abs(duration - 750), start, end))
            if duration > limits["max_duration_frames"]:
                break
    if not candidates:
        raise ValueError("No self-contained 20-30 second speech window was found.")
    # Use the same caption-feasible catalog exposed to the AI team. This prevents
    # a fast transcript from failing merely because its valid windows are not in
    # the small group nearest the target duration.
    feasible = planning_context(source, profile)["feasible_windows"]
    seed_candidates = sorted(((abs(window["duration_frames"] - 750),
                               window["source_in_frame"], window["source_out_frame"])
                              for window in feasible))
    for _, start, end in seed_candidates:
        draft = {
            "schema_version": "1.0.0", "plan_id": _contract_id("plan_" + job.name),
            "clip": {"id": _contract_id("clip_" + job.name), "title": job.name.replace("-", " ").title(),
                     "language": "en", "source_asset_id": "source"},
            "profile": {"id": profile["id"], "version": profile["version"],
                        "niche": "podcast", "style": "clear_professional_retention"},
            "render": {"timebase": {"fps": 30, "interval_convention": "half_open"},
                       "output": {"width_px": 1080, "height_px": 1920, "duration_frames": end-start,
                                  "audio_sample_rate": 48000, "audio_channels": 2},
                       "assets": [asset], "segments": [{"id": "segment_1", "source_in_frame": start,
                                   "source_out_frame": end, "output_start_frame": 0}],
                       "reframes": [{"id": "reframe_1", "segment_id": "segment_1",
                                     "crop": {"x_px": 0, "y_px": 0, "width_px": 2, "height_px": 2}}],
                       "punch_ins": [], "caption_style": "podcast_dynamic_v1", "captions": [],
                       "b_roll": [], "music": None, "sfx": [], "transitions": [], "overlays": []},
            "editorial": {"confidence_milli": 500, "review_recommended": True,
                          "summary": "Deterministic local seed plan awaiting Gemini editorial planning.",
                          "notes": [], "warnings": ["Review local transcript before publishing."]}}
        compiled, mode = _compile_candidate(draft, source, asset, profile, draft)
        if mode != "compiled_ai_timeline":
            continue
        for count in (None, 2):
            candidate = json.loads(json.dumps(compiled))
            _apply_face_side_placements(job, candidate, capabilities, words_per_caption_override=count)
            result = accept_plan((json.dumps(candidate, separators=(",", ":")) + "\n").encode(), registry, source, capabilities)
            if result.accepted:
                return candidate, result
    raise ValueError("CRE could not build a validator-safe caption grouping for this transcript.")


def _prepare_new_job(app_dir, source_path, job_name, model_size="base",
                     profile_id="PODCAST_HIGH_RETENTION_V1", profile_version="1.0.0"):
    paths = _dirs(app_dir); job = paths["jobs"] / _slug(job_name)
    if job.exists(): raise ValueError("Use a new job name to preserve existing source evidence and plans.")
    job.mkdir(parents=True, exist_ok=False)
    normalized = paths["sources"] / (job.name + "-normalized.mp4")
    prepared, registry = prepare_source(source_path, normalized, model_size=model_size)
    template_caps = _read_json(Path(__file__).parent / "cre_brain" / "data" / "capabilities.json") if (Path(__file__).parent / "cre_brain" / "data" / "capabilities.json").exists() else None
    if template_caps is None:
        existing = next((j for j in _jobs(app_dir) if (j / "renderer-capabilities.json").is_file() and j != job), None)
        if existing is None: raise ValueError("No renderer-capabilities template is available.")
        template_caps = _read_json(existing / "renderer-capabilities.json")
    _write_json(job / "asset-registry.json", registry)
    _write_json(job / "source-analysis.json", prepared["analysis"])
    _write_json(job / "renderer-capabilities.json", template_caps)
    profile, _ = load_profile(profile_id, profile_version)
    try:
        planning_context(prepared["analysis"], profile)
    except CompilationError:
        # A dense but otherwise usable transcript may need the locked fast-speech
        # profile. Select it before capabilities and the review snapshot are made.
        profile_id = "PODCAST_HIGH_RETENTION_FAST_SPEECH_V1"
        profile, _ = load_profile(profile_id, profile_version)
        planning_context(prepared["analysis"], profile)
    capabilities = _capabilities_for_job(job, profile_id, profile_version)
    plan, result = _build_seed_plan(job, prepared, registry, capabilities, profile)
    _write_json(job / "edit-plan.json", plan)
    (job / "review-snapshot.json").write_bytes(result.snapshot.json_bytes)
    report = result.report(); report["snapshot_path"] = str(job / "review-snapshot.json")
    _write_json(job / "validation-report.json", report)
    return job, report


def _show_report(report):
    if report.get("status") == "accepted_for_review":
        st.success("Accepted for review. The render remains local and requires the Render button approval.")
        st.caption(f"Execution: {report.get('execution_sha256', '')[:16]}…")
    else:
        st.error("Plan rejected. Fix every issue before queueing it.")
    for issue in report.get("issues", []):
        st.code(f"{issue.get('code')} {issue.get('path')} — {issue.get('message')}")


def _next_output(app_dir, job):
    folder = _dirs(app_dir)["renders"] / job.name
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("review-%Y%m%d-%H%M%S")
    candidate = folder / f"{stem}.mp4"
    index = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{index}.mp4"; index += 1
    return candidate


def _prepared_media(app_dir, source, kind, asset_id, duration_frames=None):
    """Create contract-normalized local media without changing the library original."""
    folder=_dirs(app_dir)["prepared"]
    token=file_hash(source)[:12]
    if kind == "video":
        target=folder/f"{asset_id}-{token}-v3.mp4"
        if not target.is_file():
            subprocess.run([os.environ.get("CRE_FFMPEG", "ffmpeg"),"-y","-stream_loop","-1","-i",str(source),"-t","5",
                            "-an","-vf","setpts=PTS-STARTPTS,fps=30,setsar=1,format=yuv420p",
                            "-c:v","libx264","-preset","medium","-crf","18","-color_primaries","bt709",
                            "-color_trc","bt709","-colorspace","bt709","-x264-params",
                            "colorprim=bt709:transfer=bt709:colormatrix=bt709",str(target)],check=True,capture_output=True)
    else:
        seconds=(duration_frames/30 if duration_frames else None)
        suffix=f"-{duration_frames}" if duration_frames else ""
        target=folder/f"{asset_id}-{token}{suffix}.wav"
        if not target.is_file():
            command=[os.environ.get("CRE_FFMPEG", "ffmpeg"),"-y"]
            if duration_frames: command += ["-stream_loop","-1"]
            command += ["-i",str(source)]
            if duration_frames: command += ["-t",f"{seconds:.6f}"]
            command += ["-vn","-af","loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000:async=1:first_pts=0",
                        "-ar","48000","-ac","2","-c:a","pcm_s16le",str(target)]
            subprocess.run(command,check=True,capture_output=True)
    metadata=probe_media(target,kind)
    sha=file_hash(target)
    asset={"id":asset_id,"kind":kind,"sha256":sha,"duration_frames":metadata.get("duration_frames"),
           "width_px":metadata.get("width_px"),"height_px":metadata.get("height_px"),
           "has_audio":metadata.get("has_audio",False)}
    if kind == "audio":
        asset["duration_frames"]=metadata["audio_samples"]*30//metadata["audio_sample_rate"]
    registry={"path":str(target.resolve()),"sha256":sha,"approved":True,
              "approval_id":"user-selected-format3-media-v1"}
    if kind == "audio":
        registry["normalization"]={"recipe":"cre_audio_minus16_v1","sha256":sha,
                                   "integrated_lufs_milli":-16000,"true_peak_mdbtp":-1500,
                                   "evidence_id":"ffmpeg-loudnorm-format3-media-v1"}
    return asset,registry


def _team_hook(job):
    path=job/"bot-team-latest.json"
    if path.is_file():
        for run in _read_json(path).get("runs",[]):
            if run.get("bot_id")=="onscreen_text_director":
                return str(run.get("output",{}).get("decisions",{}).get("hook_text","")).strip()
    return ""


def _broll_slots(render,count,targets=None,duration=BROLL_PLAYBACK_FRAMES):
    total=render["output"]["duration_frames"]
    candidates=[s for s in range(45,max(46,total-duration-29),15)
                if s+duration<=total-30]
    selected=[]
    # Keep inserts predictable for short clips: one begins in the opening five
    # seconds and the second lands near the midpoint. Move only as needed to
    # avoid punch-ins and preserve a visible return to the podcast between them.
    desired=[min(120,max(45,total//7)),round(total*.5)]
    for target in desired:
        choices=[s for s in candidates if all(abs(s-old)>=240 for old in selected)]
        if choices: selected.append(min(choices,key=lambda s:abs(s-target)))
        if len(selected)>=count: break
    return sorted(selected)


def _apply_media_to_job(app_dir,job):
    _,settings=_media_settings(app_dir); paths=_dirs(app_dir)
    plan=_read_json(job/"edit-plan.json"); registry=_read_json(job/"asset-registry.json")
    render=plan["render"]; total=render["output"]["duration_frames"]
    source_id=plan["clip"]["source_asset_id"]
    source_asset=next(a for a in render["assets"] if a["id"]==source_id)
    source_registry=registry["assets"][source_id]
    render["assets"]=[source_asset]; registry["assets"]={source_id:source_registry}
    render["b_roll"]=[]; render["music"]=None; render["sfx"]=[]; render["overlays"]=[]
    selected=[paths["broll"]/name for name in settings["b_roll"].get("selected",[])]
    selected=[p for p in selected if p.is_file()][:2]
    targets=[]
    team_path=job/"bot-team-latest.json"
    if team_path.is_file() and selected:
        team=_read_json(team_path)
        run=next((r for r in team.get("runs",[]) if r.get("bot_id")=="broll_director"),None)
        events=(run or {}).get("output",{}).get("decisions",{}).get("events",[])
        by_name={p.name:p for p in selected}; ordered=[]
        words=_read_json(job/"source-analysis.json").get("words",[])
        for event_index,event in enumerate(events):
            media=by_name.get(event.get("asset_id")) or (selected[event_index] if event_index<len(selected) else None)
            if not media or media in ordered: continue
            ordered.append(media)
            try: source_frame=words[int(event["anchor_word_id"][2:])]["source_start_frame"]
            except (ValueError,IndexError,KeyError): continue
            for segment in render["segments"]:
                if segment["source_in_frame"]<=source_frame<segment["source_out_frame"]:
                    targets.append(source_frame-segment["source_in_frame"]+segment["output_start_frame"]); break
        selected=(ordered+[p for p in selected if p not in ordered])[:2]
    if settings["b_roll"].get("enabled") and selected:
        slots=_broll_slots(render,len(selected),targets or None)
        if len(slots)<len(selected): raise ValueError("This clip has no validator-safe 4.5-second slots for all selected B-rolls.")
        for i,(source,start) in enumerate(zip(selected,slots),1):
            asset_id=_contract_id(f"broll_{i}_{source.stem}")
            asset,entry=_prepared_media(app_dir,source,"video",asset_id)
            render["assets"].append(asset); registry["assets"][asset_id]=entry
            render["b_roll"].append({"id":f"broll_{i}","start_frame":start,"end_frame":start+BROLL_PLAYBACK_FRAMES,
                                      "asset_id":asset_id,"source_in_frame":0,"fit":"cover_center",
                                      "playback":"normal","opacity_milli":1000})
        # The requested B-roll rhythm takes priority over optional zooms. Drop a
        # punch-in when it would compete with or hide a movie insert.
        render["punch_ins"]=[punch for punch in render.get("punch_ins",[])
                             if not any(event["start_frame"]<punch["end_frame"] and
                                        punch["start_frame"]<event["end_frame"]
                                        for event in render["b_roll"])]
    music_name=settings["music"].get("selected")
    music_path=paths["music"]/music_name if music_name else None
    if settings["music"].get("enabled") and music_path and music_path.is_file():
        asset_id=_contract_id("music_"+music_path.stem)
        asset,entry=_prepared_media(app_dir,music_path,"audio",asset_id,total)
        render["assets"].append(asset); registry["assets"][asset_id]=entry
        fade=min(24,max(15,total//10)); gain=max(-1000,min(1000,int(settings["music"].get("gain_db",-10))))
        render["music"]={"id":"music_bed","start_frame":0,"end_frame":total,"asset_id":asset_id,
                          "source_in_frame":0,"gain_keyframes":[{"frame":0,"gain_db":-80},
                          {"frame":fade,"gain_db":gain},{"frame":total-fade,"gain_db":gain},
                          {"frame":total,"gain_db":-80}]}
    sfx=settings["sfx"]
    if sfx.get("enabled") and render["b_roll"]:
        name=sfx.get("asset")
        if not name: raise ValueError("Sound Effects is enabled: choose one effect for the B-roll start.")
        source=paths["sfx"]/name
        if not source.is_file(): raise ValueError(f"Missing B-roll-start sound effect: {name}")
        asset_id=_contract_id(f"sfx_broll_start_{source.stem}")
        asset,entry=_prepared_media(app_dir,source,"audio",asset_id)
        duration=min(24,asset["duration_frames"]); start=render["b_roll"][0]["start_frame"]
        render["assets"].append(asset); registry["assets"][asset_id]=entry
        gain=max(-1000,min(1000,int(sfx.get("gain_db",10))))
        render["sfx"].append({"id":"sfx_broll_start","start_frame":start,"end_frame":start+duration,
                               "asset_id":asset_id,"source_in_frame":0,"gain_db":gain,
                               "fade_in_frames":3,"fade_out_frames":3})
    if settings["text"].get("enabled"):
        hook=(settings["text"].get("hook_text") or _team_hook(job) or plan["clip"]["title"])[:80]
        cta=(settings["text"].get("cta_text") or "follow for more")[:80]
        render["overlays"]=[{"id":"hook_text","start_frame":0,"end_frame":total,"text":hook,
                              "position":"top_safe","style":"label_v1"},
                             {"id":"cta_text","start_frame":0,"end_frame":total,"text":cta,
                              "position":"bottom_center","style":"label_v1"}]
    capabilities=_capabilities_for_job(job)
    _apply_face_side_placements(job, plan, capabilities)
    accepted=accept_plan((json.dumps(plan,separators=(",",":"),ensure_ascii=False)+"\n").encode(),registry,
                         _read_json(job/"source-analysis.json"),capabilities)
    report=accepted.report()
    if not accepted.accepted:
        raise ValueError("Media plan was rejected: "+"; ".join(i["message"] for i in report.get("issues",[])[:4]))
    shutil.copy2(job/"edit-plan.json",job/("edit-plan.backup-media-"+stamp()+".json"))
    _write_json(job/"edit-plan.json",plan); _write_json(job/"asset-registry.json",registry)
    (job/"review-snapshot.json").write_bytes(accepted.snapshot.json_bytes)
    report["snapshot_path"]=str(job/"review-snapshot.json"); _write_json(job/"validation-report.json",report)
    for stale in (job/"approved-review.json",job/"review-render-approval.json"):
        if stale.exists(): stale.unlink()
    return report


def render_sources(app_dir, goto_page):
    paths = _dirs(app_dir)
    st.title("🧠 Format 3 — Sources")
    st.caption("Source media and transcript evidence used by Format 3 planning jobs.")
    uploads = st.file_uploader("Add source videos", type=["mp4", "mov", "mkv", "webm", "avi"],
                               accept_multiple_files=True, key="f3_source_upload")
    if st.button("Save source files", type="primary", disabled=not uploads, use_container_width=True):
        for upload in uploads or []:
            secure_save_upload(upload, paths["sources"], "video")
        st.success(f"Saved {len(uploads)} source file(s).")
        st.rerun()
    files = sorted(p for p in paths["sources"].iterdir() if p.is_file())
    st.metric("Format 3 sources", len(files))
    for path in files:
        with st.container(border=True):
            st.write(path.name); st.caption(str(path)); st.video(str(path))
    with st.expander("Prepare a local video for Gemini", expanded=False):
        local_path = st.text_input("Local video path", key="f3_prepare_path")
        job_name = st.text_input("New job name", value="new-podcast-source", key="f3_prepare_job")
        model_size = st.selectbox("Local transcription model", ["base", "small"], key="f3_prepare_model")
        profile_choice = st.selectbox("Caption timing profile", [
            "Standard speech — 24 characters/second",
            "Fast speech — 38 characters/second",
        ], key="f3_prepare_profile")
        profile_id = ("PODCAST_HIGH_RETENTION_FAST_SPEECH_V1" if profile_choice.startswith("Fast")
                      else "PODCAST_HIGH_RETENTION_V1")
        if st.button("Prepare Source", type="primary", disabled=not local_path, use_container_width=True):
            try:
                with st.spinner("Normalizing video, transcribing locally, detecting silence, and validating seed plan..."):
                    job, report = _prepare_new_job(app_dir, local_path, job_name, model_size,
                                                   profile_id=profile_id)
                st.session_state["selected_f3_job"] = job.name
                st.success(f"Prepared {job.name} with {len(_read_json(job/'source-analysis.json')['words'])} timed words.")
                st.session_state["f3_last_report"] = report
            except Exception as exc:
                st.error(f"Source preparation failed safely: {exc}")
    if st.button("Continue to Brain Plans", use_container_width=True): goto_page("Brain Plans")


def render_brolls(app_dir,goto_page):
    paths=_dirs(app_dir); settings_path,settings=_media_settings(app_dir)
    st.title("🎞️ Format 3 — B-Rolls")
    st.caption("Add approved B-roll clips. CRE places up to two semantic 4.5-second moving inserts, returns to the podcast between them, and pauses captions only while each insert is visible.")
    uploads=st.file_uploader("Add B-roll videos",type=["mp4","mov","mkv","webm"],accept_multiple_files=True,key="f3_broll_upload")
    if st.button("Save B-roll files",disabled=not uploads,use_container_width=True):
        st.success(f"Saved {_save_uploads(paths['broll'],uploads,'video')} B-roll file(s)."); st.rerun()
    files=_media_files(paths["broll"],{".mp4",".mov",".mkv",".webm"}); names=[p.name for p in files]
    enabled=st.toggle("Use B-rolls",value=settings["b_roll"].get("enabled",True),key="f3_broll_enabled")
    chosen=st.multiselect("B-rolls available to this edit (maximum 2)",names,
                          default=[n for n in settings["b_roll"].get("selected",[]) if n in names],max_selections=2)
    st.subheader("On-screen text")
    text_enabled=st.toggle("Use top hook and bottom CTA",value=settings["text"].get("enabled",True),key="f3_text_enabled")
    hook=st.text_input("Top hook (leave blank for Gemini's hook)",value=settings["text"].get("hook_text","") or "",max_chars=80)
    cta=st.text_input("Bottom CTA",value=settings["text"].get("cta_text","follow for more"),max_chars=80)
    if st.button("Save B-roll and text controls",type="primary",width="stretch"):
        settings["b_roll"].update({"enabled":enabled,"selected":chosen})
        settings["text"].update({"enabled":text_enabled,"hook_text":hook,"cta_text":cta})
        _write_json(settings_path,settings); st.success("B-roll and on-screen text settings saved.")
    jobs = _jobs(app_dir)
    if jobs:
        selected_job = st.selectbox("Apply B-roll control to job", [p.name for p in jobs], key="f3_broll_apply_job")
        if st.button("Add or remove B-rolls in this job", type="primary", width="stretch"):
            try:
                settings["b_roll"].update({"enabled":enabled,"selected":chosen})
                settings["text"].update({"enabled":text_enabled,"hook_text":hook,"cta_text":cta})
                _write_json(settings_path, settings)
                with st.spinner("Applying the B-roll switch to the validated render plan..."):
                    _apply_media_to_job(app_dir, paths["jobs"] / selected_job)
                st.success("B-roll is ON in this job." if enabled and chosen else "B-roll is OFF in this job.")
            except Exception as exc:
                st.error(str(exc))
    for path in files:
        with st.expander(path.name): st.video(str(path)); st.caption(str(path))
    if st.button("Continue to Music",use_container_width=True): goto_page("Music")


def render_captions(app_dir, goto_page):
    paths = _dirs(app_dir); settings_path, settings = _media_settings(app_dir)
    saved = settings["captions"]
    st.title("Format 3 — Captions")
    st.caption("These switches change the real render plan. Gemini speaker-change anchors are used when available; CRE samples one frame per detected speaker role and keeps captions centered directly below that speaker's cheek.")
    enabled = st.toggle("Use auto captions", value=bool(saved.get("enabled", True)), key="f3_captions_enabled")
    stacked = st.toggle("Use stacking captions", value=saved.get("mode") == "stacked",
                        key="f3_stacking_captions", disabled=not enabled)
    words = st.segmented_control("Words shown at a time", options=[1, 2],
                                 default=int(saved.get("words_per_caption", 1)),
                                 format_func=lambda value: "One word" if value == 1 else "Two words",
                                 key="f3_caption_words", disabled=not enabled)
    adjust_all = st.toggle("Apply one adjustment to every caption",
                           value=bool(saved.get("adjust_all", False)),
                           key="f3_caption_adjust_all", disabled=not enabled)
    x_offset = st.slider("All captions: left / right", -200, 200, int(saved.get("x_offset", 0)), 2,
                         disabled=not enabled or not adjust_all, key="f3_caption_x_offset")
    y_offset = st.slider("All captions: up / down", -160, 160, int(saved.get("y_offset", 0)), 2,
                         disabled=not enabled or not adjust_all, key="f3_caption_y_offset")
    if stacked:
        st.info("Stacking is ON: the Cheek Stack style is used for every one- or two-word cue.")
    else:
        st.info("Stacking is OFF: the active normal style from Caption Library is used.")
    jobs = _jobs(app_dir)
    selected_job = st.selectbox("Apply captions to job", [p.name for p in jobs], key="f3_caption_apply_job") if jobs else None
    if st.button("Save and apply caption controls", type="primary", width="stretch"):
        settings["captions"].update({"enabled": enabled, "mode": "stacked" if stacked else "normal",
                                     "words_per_caption": int(words or 1), "adjust_all": adjust_all,
                                     "x_offset": x_offset, "y_offset": y_offset})
        _write_json(settings_path, settings)
        if selected_job:
            try:
                with st.spinner("Rebuilding exact caption cues and validating placement..."):
                    _apply_media_to_job(app_dir, paths["jobs"] / selected_job)
                state = "OFF" if not enabled else f"ON · {'stacking' if stacked else 'normal'} · {int(words or 1)} word(s)"
                st.success(f"Caption controls applied: {state}.")
            except Exception as exc:
                st.error(str(exc))
        else:
            st.success("Caption defaults saved for the next Format 3 job.")
    if st.button("Open Caption Library", width="stretch"):
        goto_page("Caption Library")


def render_caption_word_templates(app_dir):
    """Immediate one/multi-word templates shown below Format 3's style chooser."""
    paths = _dirs(app_dir); settings_path, settings = _media_settings(app_dir)
    current = int(settings["captions"].get("words_per_caption", 1))
    st.divider()
    st.subheader("Auto-caption word template")
    st.caption("Choose a job, then click a template. CRE applies it immediately and creates a new validated review snapshot.")
    flash = st.session_state.pop("f3_caption_template_flash", None)
    if flash:
        st.success(flash)
    jobs = _jobs(app_dir)
    selected_job = None
    if jobs:
        names = [job.name for job in jobs]
        preferred = st.session_state.get("selected_f3_job")
        selected_job = st.selectbox("Job to update", names,
                                    index=names.index(preferred) if preferred in names else 0,
                                    key="f3_caption_template_job")
    else:
        st.info("Prepare a Format 3 job first. Your chosen template will still become the default.")

    with st.container(horizontal=True):
        one_clicked = st.button("One word", type="primary" if current == 1 else "secondary",
                                icon=":material/looks_one:", key="f3_apply_one_word", width="stretch")
        multi_clicked = st.button("Multi word · 2 words", type="primary" if current == 2 else "secondary",
                                  icon=":material/looks_two:", key="f3_apply_multi_word", width="stretch")

    chosen = 1 if one_clicked else (2 if multi_clicked else None)
    if chosen is not None:
        settings["captions"]["enabled"] = True
        settings["captions"]["words_per_caption"] = chosen
        _write_json(settings_path, settings)
        applied = False
        if selected_job:
            try:
                with st.spinner(f"Applying {'one-word' if chosen == 1 else 'multi-word'} captions..."):
                    report = _apply_media_to_job(app_dir, paths["jobs"] / selected_job)
                st.session_state["selected_f3_job"] = selected_job
                st.session_state["f3_last_report"] = report
                label = "One word" if chosen == 1 else "Multi word · 2 words"
                st.session_state["f3_caption_template_flash"] = (
                    f"{label} is now active on {selected_job}. The new render snapshot passed validation.")
                applied = True
            except Exception as exc:
                st.error(f"Caption template was not applied: {exc}")
        else:
            st.session_state["f3_caption_template_flash"] = "Caption template saved as the Format 3 default."
            applied = True
        if applied:
            st.rerun()


def render_filters(app_dir, goto_page):
    paths = _dirs(app_dir); settings_path, settings = _media_settings(app_dir)
    st.title("Format 3 — Filters")
    st.caption("Upload your LUTs here. The selected .cube LUT is applied once to the complete finished picture when this switch is on.")
    uploads = st.file_uploader("Add LUT files", type=["cube"], accept_multiple_files=True, key="f3_lut_upload")
    if st.button("Save LUT files", disabled=not uploads, width="stretch"):
        st.success(f"Saved {_save_uploads(paths['luts'], uploads, 'lut')} LUT file(s).")
        st.rerun()
    files = _media_files(paths["luts"], {".cube"}); names = [p.name for p in files]
    enabled = st.toggle("Use a LUT filter", value=bool(settings["filters"].get("enabled", False)), key="f3_filter_enabled")
    options = ["None"] + names
    current = settings["filters"].get("selected")
    selected = st.selectbox("LUT", options, index=options.index(current) if current in options else 0,
                            disabled=not enabled, key="f3_filter_selected")
    jobs = _jobs(app_dir)
    selected_job = st.selectbox("Apply filter to job", [p.name for p in jobs], key="f3_filter_apply_job") if jobs else None
    if st.button("Save and apply filter control", type="primary", width="stretch"):
        if enabled and selected == "None":
            st.error("Choose a LUT before turning filters on.")
        else:
            settings["filters"].update({"enabled": enabled, "selected": None if selected == "None" else selected})
            _write_json(settings_path, settings)
            if selected_job:
                try:
                    with st.spinner("Binding the LUT to the validated render snapshot..."):
                        _apply_media_to_job(app_dir, paths["jobs"] / selected_job)
                    st.success("Filter is ON in this job." if enabled else "Filter is OFF in this job.")
                except Exception as exc:
                    st.error(str(exc))
            else:
                st.success("Filter default saved for the next Format 3 job.")


def render_broll_library(app_dir,goto_page):
    st.title("🎞️ Format 3 — B-Roll Library")
    st.caption("Build a free local search index from long 4K movie clips. Originals stay untouched; CRE stores small silent proxies, scene thumbnails, timecodes, and semantic vectors.")
    paths=broll_library_paths(app_dir); config={}
    if paths["config"].is_file():
        try: config=_read_json(paths["config"])
        except (OSError,ValueError): config={}
    folder=st.text_input("Movie-library folder",value=config.get("source_folder","") or "",
                         placeholder=r"C:\Movies\B-Roll Sources")
    controls=st.container(horizontal=True)
    scan_clicked=controls.button("Scan folder",disabled=not folder,width="stretch")
    ingest_clicked=controls.button("Create proxies and scene catalog",type="primary",disabled=not folder,width="stretch")
    if scan_clicked:
        try:
            files=scan_broll_folder(folder); total=sum(path.stat().st_size for path in files)
            st.success(f"Found {len(files)} supported movie file(s), totaling {total/1024**3:.1f} GB.")
            _write_json(paths["config"],{"version":"1.0.0","source_folder":str(Path(folder).resolve()),
                                         "proxy_height":540,"originals_untouched":True})
        except Exception as exc: st.error(str(exc))
    if ingest_clicked:
        try:
            progress_bar=st.progress(0.0,text="Preparing local movie catalog...")
            def update(message,fraction): progress_bar.progress(float(fraction),text=message)
            index=ingest_broll_folder(app_dir,folder,progress=update)
            _write_json(paths["config"],{"version":"1.0.0","source_folder":str(Path(folder).resolve()),
                                         "proxy_height":540,"originals_untouched":True})
            st.success(f"Cataloged {len(index['sources'])} source file(s) and {len(index['scenes'])} searchable 3-second candidates.")
        except Exception as exc: st.error(f"Library indexing stopped safely: {exc}")
    index=read_broll_index(app_dir)
    metrics=st.columns(4)
    metrics[0].metric("Movie files",len(index.get("sources",[])))
    metrics[1].metric("Candidate scenes",len(index.get("scenes",[])))
    metrics[2].metric("Local vectors","Ready" if index.get("model") else "Not built")
    proxy_bytes=sum(Path(item["proxy_path"]).stat().st_size for item in index.get("sources",[])
                    if Path(item.get("proxy_path","")).is_file())
    metrics[3].metric("Proxy storage",f"{proxy_bytes/1024**3:.2f} GB")
    with st.container(border=True):
        st.subheader("Local semantic search")
        st.write("CRE uses a free OpenCLIP model on this computer to narrow thousands of shots before Gemini sees a small final shortlist.")
        if st.button("Build or refresh semantic embeddings",disabled=not index.get("scenes"),width="stretch"):
            try:
                progress_bar=st.progress(0.0,text="Loading the local visual model...")
                def embed_update(message,fraction): progress_bar.progress(float(fraction),text=message)
                index=build_broll_embeddings(app_dir,progress=embed_update)
                st.success(f"Semantic search is ready for {len(index['scenes'])} scenes.")
                st.rerun()
            except Exception as exc: st.error(f"Embedding build stopped safely: {exc}")
        query=st.text_input("Test a cinematic search",placeholder="a lonely person walking through a dark city")
        limit=st.slider("Results",min_value=3,max_value=20,value=8)
        if st.button("Search movie library",disabled=not query or not index.get("model"),width="stretch"):
            try: st.session_state["f3_broll_search_results"]=search_broll_library(app_dir,query,limit)
            except Exception as exc: st.error(str(exc))
        results=st.session_state.get("f3_broll_search_results",[])
        for number,scene in enumerate(results,1):
            with st.container(border=True):
                left,right=st.columns([1,2])
                left.image(scene["thumbnail_path"], caption=f"Preview of {scene['source_name']}", width="stretch")
                right.write(f"**{number}. {scene['source_name']}**")
                right.caption(f"{scene['start_seconds']:.2f}s–{scene['end_seconds']:.2f}s · similarity {scene['score']:.3f}")
                if right.button("Add this 4.5-second moving clip to B-Rolls",key="f3_export_scene_"+scene["id"],width="stretch"):
                    try:
                        exported=export_broll_scene(app_dir,scene,query)
                        st.success(f"Added {exported.name} to the Format 3 B-Rolls page.")
                    except Exception as exc: st.error(str(exc))
    if st.button("Continue to B-Rolls",width="stretch"): goto_page("B-Rolls")


def render_music(app_dir,goto_page):
    paths=_dirs(app_dir); settings_path,settings=_media_settings(app_dir)
    st.title("🎵 Format 3 — Music")
    st.caption("Choose a music bed and adjust its volume. Your requested starting level is -10.")
    uploads=st.file_uploader("Add music",type=["mp3","mp4","wav","m4a","aac","flac","ogg"],accept_multiple_files=True,key="f3_music_upload")
    if st.button("Save music files",disabled=not uploads,use_container_width=True):
        st.success(f"Saved {_save_uploads(paths['music'],uploads,'audio')} music file(s)."); st.rerun()
    files=_media_files(paths["music"],{".mp3",".mp4",".wav",".m4a",".aac",".flac",".ogg"}); names=[p.name for p in files]
    enabled=st.toggle("Use music",value=settings["music"].get("enabled",True),key="f3_music_enabled")
    options=["None"]+names; current=settings["music"].get("selected")
    selected=st.selectbox("Music track",options,index=options.index(current) if current in options else 0)
    saved_gain=max(-1000,min(1000,int(settings["music"].get("gain_db",-10))))
    gain=st.slider("Music volume",min_value=-1000,max_value=1000,value=saved_gain,step=1,
                   help="CapCut-style range. Negative values lower the music; positive values raise it.")
    if st.button("Save music settings",type="primary",use_container_width=True):
        settings["music"].update({"enabled":enabled,"selected":None if selected=="None" else selected,"gain_db":gain})
        _write_json(settings_path,settings); st.success("Music settings saved.")
    for path in files:
        with st.expander(path.name): st.audio(str(path)); st.caption(str(path))
    if st.button("Continue to Sound Effects",use_container_width=True): goto_page("Sound Effects")


def render_sound_effects(app_dir,goto_page):
    paths=_dirs(app_dir); settings_path,settings=_media_settings(app_dir)
    st.title("🔊 Format 3 — Sound Effects")
    st.caption("The selected effect plays once, exactly when the first B-roll starts.")
    uploads=st.file_uploader("Add sound effects",type=["mp3","wav","m4a","aac","flac","ogg"],accept_multiple_files=True,key="f3_sfx_upload")
    if st.button("Save sound-effect files",disabled=not uploads,use_container_width=True):
        st.success(f"Saved {_save_uploads(paths['sfx'],uploads,'audio')} sound-effect file(s)."); st.rerun()
    files=_media_files(paths["sfx"],{".mp3",".wav",".m4a",".aac",".flac",".ogg"}); names=[p.name for p in files]
    enabled=st.toggle("Use sound effects",value=settings["sfx"].get("enabled",True),key="f3_sfx_enabled")
    options=["None"]+names
    current=settings["sfx"].get("asset")
    effect=st.selectbox("B-roll-start sound effect",options,index=options.index(current) if current in options else 0)
    saved_gain=max(-1000,min(1000,int(settings["sfx"].get("gain_db",10))))
    gain=st.slider("Sound-effect volume",min_value=-1000,max_value=1000,value=saved_gain,step=1,
                   help="CapCut-style range. Your requested level is +10.")
    if st.button("Save sound-effect settings",type="primary",use_container_width=True):
        if enabled and effect=="None": st.error("Choose one sound effect when this feature is enabled.")
        else:
            settings["sfx"].update({"enabled":enabled,"asset":None if effect=="None" else effect,"gain_db":gain})
            _write_json(settings_path,settings); st.success("Sound-effect settings saved.")
    for path in files:
        with st.expander(path.name): st.audio(str(path)); st.caption(str(path))
    jobs=_jobs(app_dir)
    if jobs:
        selected=st.selectbox("Apply all saved media settings to job",[p.name for p in jobs],
                              index=max(0,next((i for i,p in enumerate(jobs) if p.name==st.session_state.get("selected_f3_job")),0)))
        if st.button("Apply media and validate job",type="primary",use_container_width=True):
            try:
                with st.spinner("Preparing media, preserving caption placement, and validating the exact render plan..."):
                    report=_apply_media_to_job(app_dir,paths["jobs"]/selected)
                st.session_state["selected_f3_job"]=selected; st.session_state["f3_last_report"]=report
                st.success("Media plan accepted. Review this new version in Plan Review before rendering.")
            except Exception as exc: st.error(str(exc))
    if st.button("Continue to Brain Plans",use_container_width=True): goto_page("Brain Plans")


def render_brain_plans(app_dir, goto_page):
    paths = _dirs(app_dir)
    st.title("🧠 Format 3 — Brain Plans")
    st.caption("Coordinate Gemini editing specialists, compile their supported choices, and validate the result before Plan Review.")
    if not FORMAT3_BRAIN_AVAILABLE:
        st.error("Format 3 dependencies are unavailable: " + str(FORMAT3_IMPORT_ERROR)); return
    with st.expander("Import a contract bundle", expanded=not bool(_jobs(app_dir))):
        name = st.text_input("Job name", value="new-edit-plan")
        uploads = {label: st.file_uploader(label, type=["json"], key="f3_" + filename)
                   for label, filename in [("Edit plan", "edit-plan.json"), ("Trusted asset registry", "asset-registry.json"),
                                           ("Source analysis", "source-analysis.json"), ("Renderer capabilities", "renderer-capabilities.json")]}
        if st.button("Import bundle", type="primary", use_container_width=True):
            if not all(uploads.values()): st.error("Choose all four JSON files.")
            else:
                job = paths["jobs"] / _slug(name)
                if job.exists():
                    st.error("Choose a new job name; existing bundles are preserved."); return
                job.mkdir(parents=True, exist_ok=False)
                for (label, filename), upload in zip([("Edit plan", "edit-plan.json"), ("Trusted asset registry", "asset-registry.json"),
                                                      ("Source analysis", "source-analysis.json"), ("Renderer capabilities", "renderer-capabilities.json")], uploads.values()):
                    secure_save_upload(upload, job, "json", forced_name=filename)
                st.session_state["selected_f3_job"] = job.name; st.success("Bundle imported."); st.rerun()
    jobs = _jobs(app_dir)
    if not jobs: st.info("Import a bundle to create the first Format 3 job."); return
    selected = st.selectbox("Job", [p.name for p in jobs], index=max(0, next((i for i,p in enumerate(jobs) if p.name==st.session_state.get("selected_f3_job")),0)))
    st.session_state["selected_f3_job"] = selected; job = paths["jobs"] / selected
    with st.container(border=True):
        st.subheader("CRE Bot Team")
        provider_name = st.selectbox("Planning provider", ["OpenRouter (free)", "Gemini"], key="f3_planning_provider")
        use_openrouter = provider_name == "OpenRouter (free)"
        key_name = "OPENROUTER_API_KEY" if use_openrouter else "GEMINI_API_KEY"
        configured = bool(_provider_key(key_name))
        if configured:
            st.success(f"{provider_name} key is configured locally.")
        else:
            st.error(f"Add {key_name} to .streamlit/secrets.toml before generating.")
        if use_openrouter:
            model = st.selectbox("Planning model", list(FREE_MODELS), format_func=FREE_MODELS.get, key="f3_openrouter_model")
            st.caption("Free models only; no paid fallback. Each bot team run uses multiple requests from your shared free quota. Qwen sees sampled frames; GLM uses the transcript and local B-roll retrieval.")
            if st.button("Check OpenRouter connection", disabled=not configured):
                try:
                    OpenRouterProvider(_provider_key(key_name), model).check_connection()
                    st.success("Key accepted and selected model listed as free. Generation availability depends on provider capacity and quota.")
                except ProviderError as exc:
                    st.error(str(exc))
        else:
            model = st.selectbox("Planning model", ["gemini-3.5-flash-lite", "gemini-3.6-flash"], key="f3_gemini_model")
            st.caption("Gemini uses your Google API account and its quota/billing settings.")
        roster = bot_roster()
        if read_broll_index(app_dir).get("model"):
            for bot in roster:
                if bot["id"] in ("asset_researcher","broll_reranker"):
                    bot["status"]="ready"
        ready_count = sum(bot["status"] in ("ready", "local") for bot in roster)
        waiting_count = len(roster) - ready_count
        metrics = st.columns(3)
        metrics[0].metric("Editing bots", len(roster))
        metrics[1].metric("Ready now", ready_count)
        metrics[2].metric("Waiting for inputs", waiting_count)
        with st.expander("Meet the bot team"):
            for bot in roster:
                icon = "✅" if bot["status"] in ("ready", "local") else "⏳"
                st.write(f"{icon} **{bot['name']}** — {bot['job']}")
        st.caption("Each specialist returns a bounded proposal. CRE combines only supported choices, computes every frame locally, and runs the existing validator. A completed local B-roll index activates shortlist retrieval; visual models can rerank candidates; final QA waits for the render.")
        provider_consent = st.checkbox(
            "I consent to send the selected transcript and the minimum required sampled frames to " + provider_name + ".",
            value=False,
            key="f3_provider_consent_" + provider_name,
            help="The provider processes this material under its own privacy terms. CRE does not send the full local library.",
        )
        if st.button("✨ Run " + provider_name + " Bot Team", type="primary", disabled=not configured or not provider_consent, width="stretch"):
            try:
                with st.status("Starting the CRE bot team...", expanded=True) as status:
                    def bot_event(bot_id, state):
                        label = next((bot["name"] for bot in roster if bot["id"] == bot_id), bot_id)
                        if state == "running": status.write(f"🧠 {label} is working...")
                        elif state == "complete": status.write(f"✅ {label} completed its handoff.")
                        else: status.write(f"⚠️ {label} returned an invalid handoff.")
                    _, report, generation = _generate_gemini_bot_plan(job, model, on_event=bot_event, provider_name=provider_name)
                    status.update(label="Bot team finished; CRE compiled and validated the plan.",
                                  state="complete" if generation["accepted"] else "error")
                st.session_state["f3_last_report"] = report
                st.session_state["f3_last_bot_team"] = generation
                if generation["accepted"]:
                    st.success("The coordinated bot plan passed validation. The previous plan was backed up and this version is ready for Plan Review.")
                else:
                    st.error("The coordinated proposal was rejected. The existing plan was preserved.")
            except (ProviderError, BotTeamError) as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Bot coordination failed safely: {exc}")
        generation = st.session_state.get("f3_last_bot_team")
        if generation is None and (job / "bot-team-latest.json").is_file():
            generation = _read_json(job / "bot-team-latest.json")
        if generation:
            st.subheader("Latest team handoffs")
            for run in generation.get("runs", []):
                with st.expander("✅ " + next((bot["name"] for bot in roster if bot["id"] == run["bot_id"]), run["bot_id"])):
                    output = run["output"]
                    st.write(output["summary"])
                    st.progress(output["confidence_milli"] / 1000, text=f"Confidence {output['confidence_milli']/10:.0f}%")
                    for warning in output.get("warnings", []): st.warning(warning)
            for bot_id, reason in generation.get("pending", {}).items():
                label = next((bot["name"] for bot in roster if bot["id"] == bot_id), bot_id)
                st.info(f"{label}: {reason}")
            st.caption(f"Compiler: {generation.get('compiler_report', {}).get('compiler_mode', 'pending')} · Validation: {generation.get('validation_status', 'pending')}")
    with st.container(border=True):
        st.write(f"**{selected}**"); st.caption(str(job))
        cols=st.columns(2)
        if cols[0].button("Validate plan", type="primary", use_container_width=True):
            with st.spinner("Checking schema, timings, media, transcript, font, and renderer capabilities..."):
                try: _, report = _validate_job(job)
                except Exception as exc: report={"status":"rejected","issues":[{"code":"VALIDATION_FAILED","path":"/job","message":str(exc)}]}
            st.session_state["f3_last_report"] = report
        if cols[1].button("Open Plan Review", use_container_width=True): goto_page("Plan Review")
        report = st.session_state.get("f3_last_report")
        if report: _show_report(report)


def render_queue(app_dir, goto_page):
    st.title("⚙️ Format 3 — Render Queue")
    st.caption("Only explicitly approved reviewed versions can render. Every version is revalidated at execution.")
    ready=[]
    for job in _jobs(app_dir):
        if (job / "approved-review.json").exists():
            try:
                snapshot, result = approved_snapshot(job)
                ready.append(job.name)
                with st.container(border=True):
                    st.write(job.name)
                    st.caption("Approved version: " + snapshot.parent.name)
            except Exception as exc: st.warning(f"{job.name}: {exc}")
    st.session_state["current_jobs_f3"] = ready
    if not ready: st.info("No approved reviews. Open Plan Review to approve a version.")
    if st.button("Open Plan Review"): goto_page("Plan Review")
    if st.button("Continue to Actually Rendering", type="primary", disabled=not ready): goto_page("Actually Rendering")


def render_actually_rendering(app_dir, goto_page):
    st.title("🎬 Format 3 — Actually Rendering")
    st.caption("The button below records approval for a local review render. It does not publish the result.")
    queued=st.session_state.get("current_jobs_f3",[])
    if not queued: st.warning("The Format 3 queue is empty."); return
    for name in queued: st.write("• "+name)
    if st.button("Approve and render queued plans", type="primary", use_container_width=True):
        outputs=[]
        for name in queued:
            job=_dirs(app_dir)["jobs"]/name
            try:
                snapshot,result=approved_snapshot(job)
                snap_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest()
                approval={"version":"1.0.0","snapshot_sha256":snap_hash,"approved_for_review_render":True,
                          "approval_id":"streamlit-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                          "scope":"Local Format 3 review render only; no publishing."}
                approval_path=job/"review-render-approval.json"; _write_json(approval_path,approval)
                snapshot_data=_read_json(snapshot); font_path=snapshot_data["execution"]["renderer_binding"]["caption_preset"]["font_binding"]["path"]
                output=_next_output(app_dir,job)
                with st.spinner(f"Rendering {name}..."):
                    render_report=render_review(snapshot,approval_path,output,font_path=font_path)
                _write_json(job/"last-render-report.json",render_report); outputs.append(str(output)); st.success(f"Rendered {output.name}")
            except Exception as exc: st.error(f"{name}: {exc}")
        prior=st.session_state.setdefault("rendered_files_f3",[])
        for output in outputs:
            if output not in prior: prior.append(output)
        if outputs: goto_page("Preview")


def render_preview(app_dir, queue_for_auto_post):
    st.title("👀 Format 3 — Preview")
    st.caption("Watch and listen before sending a review render to the shared Auto Post queue.")
    files=[Path(p) for p in st.session_state.get("rendered_files_f3",[]) if Path(p).is_file()]
    if not files:
        files=sorted(_dirs(app_dir)["renders"].glob("*/*.mp4"),key=lambda p:p.stat().st_mtime,reverse=True)
    if not files: st.info("No Format 3 renders yet."); return
    queue=st.session_state.get("auto_post_queue",[])
    for path in files:
        with st.container(border=True):
            st.write(f"**{path.name}**"); st.video(str(path)); st.caption(str(path))
            queued=any(item["path"]==str(path) for item in queue)
            if st.button("Already in Auto Post" if queued else "📤 Send to Auto Post", key="f3_post_"+hashlib.sha1(str(path).encode()).hexdigest(), disabled=queued, use_container_width=True):
                queue_for_auto_post("Format 3",path); st.success("Added to Auto Post."); st.rerun()


def render_format3_page(page, app_dir, goto_page, queue_for_auto_post):
    if page == "Sources": render_sources(app_dir,goto_page)
    elif page == "B-Roll Library": render_broll_library(app_dir,goto_page)
    elif page == "B-Rolls": render_brolls(app_dir,goto_page)
    elif page == "Music": render_music(app_dir,goto_page)
    elif page == "Sound Effects": render_sound_effects(app_dir,goto_page)
    elif page == "Captions": render_captions(app_dir,goto_page)
    elif page == "Filters": render_filters(app_dir,goto_page)
    elif page == "Brain Plans": render_brain_plans(app_dir,goto_page)
    elif page == "Plan Review": render_plan_review(app_dir,goto_page)
    elif page == "Render Queue": render_queue(app_dir,goto_page)
    elif page == "Actually Rendering": render_actually_rendering(app_dir,goto_page)
    elif page == "Preview": render_preview(app_dir,queue_for_auto_post)
