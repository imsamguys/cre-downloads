"""Run: python -m unittest tests.test_bot_coordinator -v"""
import json
from pathlib import Path
import unittest

from cre_brain.compiler.editorial import planning_context
from cre_brain.coordinator import BotCoordinator, TEAM_VERSION, roster
from cre_brain.providers.base import SpecialistResult


ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "cre_brain/data/profile.json").read_text())


class FakeProvider:
    name = "fake_provider"
    model = "fake-model"

    def __init__(self):
        self.calls = []

    def run_specialist(self, *, bot_id, instructions, input_data, response_schema):
        self.calls.append(bot_id)
        source = input_data["source_sha256"]
        base = {"bot_id": bot_id, "source_sha256": source, "summary": bot_id + " completed",
                "confidence_milli": 800, "warnings": [], "decisions": {}}
        if bot_id == "clip_finder":
            base["decisions"] = {"ranked_window_ids": [input_data["feasible_windows"][0]["id"]],
                                 "hook_reason": "A complete opening claim."}
        elif bot_id == "story_editor":
            base["decisions"] = {"window_id": input_data["shortlisted_window_ids"][0],
                                 "title": "A complete thought", "silence_cut_ids": [],
                                 "story_shape": "Hook, explanation and payoff."}
        else:
            ids = [word["id"] for word in input_data.get("excerpt_words", [])]
            if bot_id == "speaker_director":
                base["decisions"] = {"speaker_labels": ["Speaker 1"], "change_word_ids": [],
                                     "color_strategy": "White primary speaker captions."}
            elif bot_id == "caption_director":
                base["decisions"] = {"caption_break_after_word_ids": [ids[4]],
                                     "emphasis_word_ids": [ids[2]],
                                     "speaker_color_strategy": "White with yellow emphasis.",
                                     "placement_strategy": "Use clear negative space."}
            elif bot_id == "visual_director":
                base["decisions"] = {"punch_ins": [{"anchor_word_id": ids[15], "strength": "subtle"}],
                                     "composition_strategy": "Preserve shoulders and headroom.",
                                     "caption_safe_space_strategy": "Avoid the face and microphone."}
            elif bot_id == "onscreen_text_director":
                base["decisions"] = {"hook_text": "A COMPLETE THOUGHT", "highlight_words": ["THOUGHT"],
                                     "cta_text": "Watch to the end"}
            elif bot_id == "broll_director":
                base["decisions"] = {"events": [{"anchor_word_id": ids[20], "query": "specific evidence",
                                                  "purpose": "Explain the spoken idea.", "click_sfx": True}]}
            elif bot_id == "sound_director":
                base["decisions"] = {"music_mood": "Restrained tension", "music_gain_db": -32,
                                     "ducking_strategy": "Keep dialogue dominant.",
                                     "click_anchor_word_ids": [ids[20]]}
            elif bot_id == "finishing_director":
                base["decisions"] = {"sharpening": "subtle", "clarity": "subtle", "denoise": "off",
                                     "grade_notes": "Protect skin tones and blacks."}
            elif bot_id == "creative_critic":
                base["decisions"] = {"reference_alignment_milli": 760, "verdict": "promising",
                                     "priority_fixes": ["Verify framing after render."]}
        return SpecialistResult(output=base, provider=self.name, model=self.model, latency_ms=1)


def source_fixture():
    return {"version": "1.0.0", "source_asset_id": "source", "source_sha256": "a" * 64,
            "fps": 30, "duration_frames": 1800, "verified": True, "alignment_id": "test",
            "words": [{"text": "word", "source_start_frame": 60 + i * 15,
                       "source_end_frame": 69 + i * 15} for i in range(100)],
            "safe_silence_ranges": []}


class BotCoordinatorTests(unittest.TestCase):
    def test_all_planning_specialists_run_and_combine_bounded_intent(self):
        provider = FakeProvider()
        context = planning_context(source_fixture(), PROFILE)
        result = BotCoordinator(provider).run(context, PROFILE, reference="quality target")
        self.assertEqual(TEAM_VERSION, result["version"])
        self.assertEqual(10, len(result["runs"]))
        self.assertEqual(10, len(provider.calls))
        self.assertEqual(context["source_sha256"], result["combined_intent"]["source_sha256"])
        self.assertIn(result["combined_intent"]["window_id"],
                      [window["id"] for window in context["feasible_windows"]])
        self.assertEqual({"asset_researcher", "final_qa"}, set(result["pending"]))

    def test_roster_includes_ai_local_and_deferred_gates(self):
        states = {bot["id"]: bot["status"] for bot in roster()}
        self.assertEqual("ready", states["creative_critic"])
        self.assertEqual("local", states["edit_compiler"])
        self.assertEqual("waiting_for_catalog", states["asset_researcher"])
        self.assertEqual("waiting_for_render", states["final_qa"])


if __name__ == "__main__":
    unittest.main()
