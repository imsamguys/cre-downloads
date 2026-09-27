"""Provider-neutral specialist team for CRE Format 3 planning.

Every result is untrusted editorial metadata. Only the existing compiler and
validator can turn the combined intent into renderer instructions.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from jsonschema import Draft202012Validator


TEAM_VERSION = "cre_bot_team_v1"

BOT_ROSTER = (
    ("clip_finder", "Clip Finder", "ready", "Ranks feasible excerpts by hook and completeness."),
    ("story_editor", "Story Editor", "ready", "Chooses the final excerpt and safe silence cuts."),
    ("speaker_director", "Speaker Director", "ready", "Proposes speaker roles and change anchors."),
    ("caption_director", "Caption Director", "ready", "Plans phrase groups, emphasis, color intent and placement intent."),
    ("visual_director", "Visual Director", "ready", "Plans composition and restrained punch-ins."),
    ("onscreen_text_director", "On-Screen Text Director", "ready", "Proposes the hook line and CTA."),
    ("broll_director", "B-Roll Director", "ready", "Plans semantic B-roll searches and entry beats."),
    ("broll_reranker", "B-Roll Reranker", "waiting_for_catalog", "Compares the local shortlist against the podcast video."),
    ("sound_director", "Sound Director", "ready", "Plans music mood, ducking intent and click cues."),
    ("finishing_director", "Finishing Director", "ready", "Proposes restrained clarity and color treatment."),
    ("creative_critic", "Creative Critic", "ready", "Grades the combined proposal against the saved reference."),
    ("asset_researcher", "Asset Researcher", "waiting_for_catalog", "Resolves approved B-roll after a stock or licensed catalog is connected."),
    ("edit_compiler", "Edit Compiler", "local", "Compiles supported proposals into exact frame instructions."),
    ("validation_gate", "Validation Gate", "local", "Rejects invalid compiled plans before Plan Review."),
    ("final_qa", "Final QA", "waiting_for_render", "Inspects the rendered video and audio after rendering."),
)


def roster() -> list[dict[str, str]]:
    return [{"id": bot_id, "name": name, "status": status, "job": job}
            for bot_id, name, status, job in BOT_ROSTER]


def _base_schema(bot_id: str, source_hash: str, decisions: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["bot_id", "source_sha256", "summary", "confidence_milli", "warnings", "decisions"],
            "properties": {
                "bot_id": {"const": bot_id},
                "source_sha256": {"const": source_hash},
                "summary": {"type": "string", "minLength": 1, "maxLength": 600},
                "confidence_milli": {"type": "integer", "minimum": 0, "maximum": 1000},
                "warnings": {"type": "array", "maxItems": 8,
                             "items": {"type": "string", "minLength": 1, "maxLength": 240}},
                "decisions": {"type": "object", "additionalProperties": False,
                              "required": list(decisions), "properties": decisions}}}


def _ids(values: list[str], maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "minItems": minimum, "maxItems": maximum,
            "items": {"type": "string", "enum": values}}


def _strings(maximum: int, length: int = 240) -> dict[str, Any]:
    return {"type": "array", "maxItems": maximum,
            "items": {"type": "string", "minLength": 1, "maxLength": length}}


class BotTeamError(ValueError):
    pass


class BotCoordinator:
    def __init__(self, provider, *, on_event: Callable[[str, str], None] | None = None):
        self.provider = provider
        self.on_event = on_event or (lambda bot_id, state: None)

    def _run(self, bot_id: str, instructions: str, input_data: dict[str, Any], schema: dict[str, Any]):
        self.on_event(bot_id, "running")
        result = self.provider.run_specialist(bot_id=bot_id, instructions=instructions,
                                              input_data=input_data, response_schema=schema)
        errors = sorted(Draft202012Validator(schema).iter_errors(result.output), key=lambda e: str(e.path))
        if errors:
            self.on_event(bot_id, "rejected")
            raise BotTeamError(f"{bot_id} returned output outside its contract.")
        self.on_event(bot_id, "complete")
        return {"bot_id": bot_id, "status": "complete", "provider": result.provider,
                "model": result.model, "latency_ms": result.latency_ms, "usage": result.usage,
                "response_id": result.response_id, "output": result.output}

    def run(self, context: dict[str, Any], profile: dict[str, Any], *, reference: str = "") -> dict[str, Any]:
        source_hash = context["source_sha256"]
        windows = context["feasible_windows"]
        window_ids = [w["id"] for w in windows]
        word_ids = [w["id"] for w in context["words"]]
        silence_ids = [s["id"] for s in context["silences"]]
        runs: list[dict[str, Any]] = []

        clip_schema = _base_schema("clip_finder", source_hash, {
            "ranked_window_ids": _ids(window_ids, 5, 1),
            "hook_reason": {"type": "string", "minLength": 1, "maxLength": 400}})
        clip = self._run("clip_finder",
            "Rank the strongest self-contained podcast excerpts. Prefer a clear hook, escalating idea and payoff. Choose only supplied window IDs.",
            {"source_sha256": source_hash, "words": context["words"], "feasible_windows": windows,
             "reference_goal": reference[:6000]}, clip_schema)
        runs.append(clip)
        shortlist = clip["output"]["decisions"]["ranked_window_ids"]

        story_schema = _base_schema("story_editor", source_hash, {
            "window_id": {"type": "string", "enum": shortlist},
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "silence_cut_ids": _ids(silence_ids, 6),
            "story_shape": {"type": "string", "minLength": 1, "maxLength": 500}})
        story = self._run("story_editor",
            "Choose one shortlisted excerpt. Preserve meaning and chronology. Use only verified silence IDs, and only when shortening the pause improves rhythm.",
            {"source_sha256": source_hash, "shortlisted_window_ids": shortlist,
             "feasible_windows": [w for w in windows if w["id"] in shortlist],
             "words": context["words"], "silences": context["silences"]}, story_schema)
        runs.append(story)
        selected_id = story["output"]["decisions"]["window_id"]
        selected = next(w for w in windows if w["id"] == selected_id)
        first = int(selected["first_word_id"][2:]); last = int(selected["last_word_id"][2:])
        excerpt_words = context["words"][first:last + 1]
        excerpt_ids = [w["id"] for w in excerpt_words]
        shared = {"source_sha256": source_hash, "selected_window": selected,
                  "excerpt_words": excerpt_words, "reference_goal": reference[:6000],
                  "asset_catalog": context.get("asset_catalog", {}),
                  "render_controls": context.get("render_controls", {})}

        speaker_schema = _base_schema("speaker_director", source_hash, {
            "speaker_labels": _strings(4, 80), "change_word_ids": _ids(excerpt_ids, 12),
            "color_strategy": {"type": "string", "minLength": 1, "maxLength": 400}})
        runs.append(self._run("speaker_director",
            "Identify every visible speaker switch supported by the attached source and return the first spoken word after each switch in change_word_ids. These anchors control caption placement under speaker 1 versus speaker 2. If a switch is uncertain, be conservative and explain it.",
            shared, speaker_schema))

        caption_schema = _base_schema("caption_director", source_hash, {
            "caption_break_after_word_ids": _ids(excerpt_ids, 150),
            "emphasis_word_ids": _ids(excerpt_ids, 30),
            "speaker_color_strategy": {"type": "string", "minLength": 1, "maxLength": 400},
            "placement_strategy": {"type": "string", "minLength": 1, "maxLength": 500}})
        caption = self._run("caption_director",
            "Follow render_controls exactly. Plan caption breaks for the requested one-word or two-word template. Captions are centered directly below the active speaker's cheek, and speaker-change anchors determine when placement switches. Plan sparse emphasis and do not invent words or timings; CRE owns exact layout.",
            {**shared, "speaker_proposal": runs[-1]["output"]}, caption_schema)
        runs.append(caption)

        visual_schema = _base_schema("visual_director", source_hash, {
            "punch_ins": {"type": "array", "maxItems": 3, "items": {"type": "object",
                "additionalProperties": False, "required": ["anchor_word_id", "strength"],
                "properties": {"anchor_word_id": {"type": "string", "enum": excerpt_ids},
                               "strength": {"type": "string", "enum": ["subtle", "medium"]}}}},
            "composition_strategy": {"type": "string", "minLength": 1, "maxLength": 500},
            "caption_safe_space_strategy": {"type": "string", "minLength": 1, "maxLength": 500}})
        visual = self._run("visual_director",
            "Preserve a comfortable interview composition with headroom, shoulders, microphone and gestures. Propose at most three meaningful punch-ins; CRE calculates crops and zoom envelopes.",
            shared, visual_schema)
        runs.append(visual)

        text_schema = _base_schema("onscreen_text_director", source_hash, {
            "hook_text": {"type": "string", "minLength": 1, "maxLength": 120},
            "highlight_words": _strings(4, 32),
            "cta_text": {"type": "string", "minLength": 1, "maxLength": 100}})
        runs.append(self._run("onscreen_text_director",
            "Propose a concise persistent top hook and a small bottom CTA inspired by the reference. Do not invent a factual claim absent from the excerpt.",
            shared, text_schema))

        broll_ids = [item["id"] for item in context.get("asset_catalog", {}).get("b_roll", [])]
        event_properties = {"anchor_word_id": {"type": "string", "enum": excerpt_ids},
                            "query": {"type": "string", "minLength": 1, "maxLength": 160},
                            "purpose": {"type": "string", "minLength": 1, "maxLength": 240},
                            "click_sfx": {"type": "boolean"}}
        event_required = ["anchor_word_id", "query", "purpose", "click_sfx"]
        if broll_ids:
            event_properties["asset_id"]={"type":"string","enum":broll_ids}
            event_required.append("asset_id")
        broll_schema = _base_schema("broll_director", source_hash, {
            "events": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "object",
                "additionalProperties": False, "required": event_required,
                "properties": event_properties}}})
        if context.get("render_controls", {}).get("b_roll_enabled", True):
            broll = self._run("broll_director",
                "Always propose one or two precise B-roll moments. Anchor each to the transcript and write a literal visual search query naming concrete people, objects, setting, and physical action that visualize the idea being discussed. Never describe the podcast source shot, speaker, hoodie, studio, chair, or microphone. Do not request another talking head. Do not use abstract mood words, movie genres, training imagery, or generic filler unless the spoken sentence is actually about them. When asset IDs are supplied, choose one; otherwise the local movie index will retrieve candidates from the query.",
                shared, broll_schema)
        else:
            broll = {"bot_id": "broll_director", "status": "complete", "provider": "local_controls",
                     "model": "disabled", "latency_ms": 0, "usage": {}, "response_id": None,
                     "output": {"bot_id": "broll_director", "source_sha256": source_hash,
                                "summary": "B-roll is disabled by the user.", "confidence_milli": 1000,
                                "warnings": [], "decisions": {"events": []}}}
        runs.append(broll)

        sound_schema = _base_schema("sound_director", source_hash, {
            "music_mood": {"type": "string", "minLength": 1, "maxLength": 240},
            "music_gain_db": {"type": "integer", "minimum": -40, "maximum": -20},
            "ducking_strategy": {"type": "string", "minLength": 1, "maxLength": 400},
            "click_anchor_word_ids": _ids(excerpt_ids, 4)})
        runs.append(self._run("sound_director",
            "Plan a restrained instrumental bed and dialogue-first mix. Align subtle click cues only with proposed B-roll entries.",
            {**shared, "broll_proposal": broll["output"]}, sound_schema))

        finishing_schema = _base_schema("finishing_director", source_hash, {
            "sharpening": {"type": "string", "enum": ["off", "subtle", "moderate"]},
            "clarity": {"type": "string", "enum": ["off", "subtle", "moderate"]},
            "denoise": {"type": "string", "enum": ["off", "subtle", "moderate"]},
            "grade_notes": {"type": "string", "minLength": 1, "maxLength": 500}})
        runs.append(self._run("finishing_director",
            "Propose restrained sharpening, clarity, denoise and color treatment. Protect skin tones and avoid halos or crushed blacks.",
            shared, finishing_schema))

        critic_schema = _base_schema("creative_critic", source_hash, {
            "reference_alignment_milli": {"type": "integer", "minimum": 0, "maximum": 1000},
            "verdict": {"type": "string", "enum": ["promising", "needs_revision"]},
            "priority_fixes": _strings(6, 240)})
        summaries = [{"bot_id": r["bot_id"], "summary": r["output"]["summary"],
                      "decisions": r["output"]["decisions"]} for r in runs]
        critic = self._run("creative_critic",
            "Review the coordinated proposal against the supplied quality reference. Be strict about story, composition, captions, B-roll, sound and unsupported claims.",
            {"source_sha256": source_hash, "reference_goal": reference[:6000],
             "team_proposals": summaries}, critic_schema)
        runs.append(critic)

        caption_decisions = caption["output"]["decisions"]
        visual_decisions = visual["output"]["decisions"]
        warnings = []
        for run in runs:
            warnings.extend(run["output"]["warnings"])
        warnings.extend(critic["output"]["decisions"]["priority_fixes"])
        warnings.append("B-roll, speaker colors, dynamic caption placement, on-screen text, music, SFX and finishing remain proposals until their assets and renderer capabilities are validated.")
        warnings = list(dict.fromkeys(w[:240] for w in warnings if w))[:8]
        confidence = min(story["output"]["confidence_milli"], caption["output"]["confidence_milli"],
                         visual["output"]["confidence_milli"], critic["output"]["confidence_milli"])
        intent = {"intent_version": context["intent_schema"]["properties"]["intent_version"]["const"],
                  "source_sha256": source_hash,
                  "title": story["output"]["decisions"]["title"],
                  "window_id": selected_id,
                  "silence_cut_ids": story["output"]["decisions"]["silence_cut_ids"],
                  "caption_break_after_word_ids": caption_decisions["caption_break_after_word_ids"],
                  "emphasis_word_ids": caption_decisions["emphasis_word_ids"],
                  "punch_ins": visual_decisions["punch_ins"],
                  "editorial": {"summary": story["output"]["summary"],
                                "confidence_milli": confidence, "warnings": warnings}}
        if not Draft202012Validator(context["intent_schema"]).is_valid(intent):
            raise BotTeamError("The combined bot intent did not satisfy the compiler contract.")
        return {"version": TEAM_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "provider": self.provider.name, "model": self.provider.model,
                "roster": roster(), "selected_window_id": selected_id,
                "runs": runs, "combined_intent": deepcopy(intent),
                "pending": {"asset_researcher": "Connect an approved stock or licensed media catalog.",
                            "final_qa": "Run after a reviewed plan has rendered."}}
