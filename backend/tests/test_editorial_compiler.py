"""Run: python -m unittest discover -s tests -p test_editorial_compiler.py -v"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cre_brain.compiler.editorial import (
    VERSION, CompilationError, compile_intent, planning_context, resolve_selection, word_id,
)
from cre_brain.providers.base import GenerationResult
from cre_brain.providers.gemini import GeminiProvider
from cre_brain.validators.source import validate_source
from cre_brain.validators.semantic import validate_semantics


ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "cre_brain/data/profile.json").read_text())


def fixture():
    source = {"version":"1.0.0","source_asset_id":"source","source_sha256":"a"*64,
              "fps":30,"duration_frames":1600,"verified":True,"alignment_id":"synthetic-v1",
              "words":[{"text":"word", "source_start_frame":3+i*15+(30 if i>=26 else 0),
                        "source_end_frame":11+i*15+(30 if i>=26 else 0)} for i in range(90)],
              "safe_silence_ranges":[[386,423]]}
    asset={"id":"source","kind":"video","sha256":"a"*64,"duration_frames":1600,
           "width_px":848,"height_px":480,"has_audio":True}
    baseline={"schema_version":"1.0.0","plan_id":"plan_test",
              "clip":{"id":"clip_test","source_asset_id":"source","title":"Original","language":"en"},
              "profile":{"id":PROFILE["id"],"version":PROFILE["version"],"niche":"podcast","style":"clear_professional_retention"},
              "render":{"timebase":{"fps":30,"interval_convention":"half_open"},
                        "output":{"duration_frames":777,"width_px":1080,"height_px":1920,"audio_sample_rate":48000,"audio_channels":2},
                        "assets":[asset],"segments":[{"id":"segment_1","source_in_frame":1,"source_out_frame":778,"output_start_frame":0}],
                        "reframes":[{"id":"reframe_1","segment_id":"segment_1","crop":{"x_px":288,"y_px":0,"width_px":270,"height_px":480}}],
                        "caption_style":"podcast_dynamic_v1","captions":[],"punch_ins":[],
                        "b_roll":[],"music":None,"sfx":[],"transitions":[],"overlays":[]}}
    intent={"intent_version":VERSION,"source_sha256":"a"*64,"title":"A complete thought",
            "selection":{"first_word_id":"w_00000","last_word_id":"w_00049"},
            "silence_cut_ids":[],"caption_break_after_word_ids":[],"emphasis_word_ids":[],
            "punch_ins":[],"editorial":{"summary":"Preserve the selected speech.","confidence_milli":800,"warnings":[]}}
    return source,asset,baseline,intent


class EditorialCompilerTests(unittest.TestCase):
    def setUp(self):
        self.source,self.asset,self.baseline,self.intent=fixture()

    def compile(self):
        return compile_intent(self.intent,self.source,self.asset,PROFILE,self.baseline)

    def assert_valid(self, plan):
        self.assertEqual([],validate_semantics(plan,PROFILE))
        self.assertEqual([],validate_source(plan,self.source))

    def test_determinism_selection_and_immutability(self):
        before=deepcopy((self.source,self.asset,self.baseline,self.intent))
        plan,report=self.compile()
        self.assertEqual((plan,report),self.compile())
        self.assertEqual(before,(self.source,self.asset,self.baseline,self.intent))
        self.assert_valid(plan)
        self.assertTrue(report["selection_preserved"])
        self.assertFalse(report["fallback_used"])
        self.assertEqual(50,sum(len(c["words"]) for c in plan["render"]["captions"]))

    def test_verified_silence_maps_words_without_removing_speech(self):
        self.intent["silence_cut_ids"]=["silence_00000"]
        plan,report=self.compile();self.assert_valid(plan)
        self.assertEqual(2,len(plan["render"]["segments"]))
        self.assertEqual(50,sum(len(c["words"]) for c in plan["render"]["captions"]))
        self.assertLess(report["duration_frames"],777)

    def test_occurrence_specific_emphasis(self):
        self.intent["emphasis_word_ids"]=["w_00012"]
        plan,report=self.compile();self.assert_valid(plan)
        self.assertEqual(["w_00012"],report["emphasized_word_ids"])
        self.assertEqual(1,sum(w["emphasis"] for c in plan["render"]["captions"] for w in c["words"]))

    def test_caption_preferences_and_transparent_adjustments(self):
        self.intent["caption_break_after_word_ids"]=["w_00003","w_00004"]
        plan,report=self.compile();self.assert_valid(plan)
        self.assertTrue(any(d["code"]=="CAPTION_BREAKS_ADJUSTED" for d in report["diagnostics"]))
        self.assertTrue(any(c[-1]=="w_00003" or c[-1]=="w_00004" for c in report["caption_groups"]))

    def test_punches_fit_segments_and_conflicts_are_reported(self):
        self.intent["punch_ins"]=[{"anchor_word_id":"w_00020","strength":"medium"},
                                  {"anchor_word_id":"w_00021","strength":"subtle"}]
        plan,report=self.compile();self.assert_valid(plan)
        self.assertEqual(1,len(plan["render"]["punch_ins"]))
        self.assertTrue(any(d["code"]=="PUNCH_OMITTED" for d in report["diagnostics"]))
        self.assertEqual(1080,max(k["scale_milli"] for k in plan["render"]["punch_ins"][0]["keyframes"]))

    def test_invalid_references_never_fallback(self):
        cases=[("selection",{"first_word_id":"w_99999","last_word_id":"w_99999"}),
               ("source_sha256","b"*64),("emphasis_word_ids",["w_00089"]),
               ("silence_cut_ids",["silence_99999"]),("render",{}),
               ("punch_ins",[{"anchor_word_id":"w_00020","strength":"extreme"}])]
        for key,value in cases:
            with self.subTest(key=key):
                bad=deepcopy(self.intent);bad[key]=value
                with self.assertRaises(CompilationError): compile_intent(bad,self.source,self.asset,PROFILE,self.baseline)

    def test_unsafe_guard_and_too_short_clip_rejected(self):
        self.source["words"][0]["source_start_frame"]=0
        with self.assertRaises(CompilationError):self.compile()
        self.source["words"][0]["source_start_frame"]=3
        self.intent["selection"]["last_word_id"]="w_00003"
        with self.assertRaises(CompilationError):self.compile()

    def test_catalog_is_compilable(self):
        context=planning_context(self.source,PROFILE)
        self.assertGreater(len(context["feasible_windows"]),0)
        for window in context["feasible_windows"]:
            self.intent["selection"]={k:window[k] for k in ("first_word_id","last_word_id")}
            plan,_=self.compile();self.assert_valid(plan)

    def test_window_id_resolves_exactly_and_rejects_invented_selection(self):
        context=planning_context(self.source,PROFILE)
        candidate=deepcopy(self.intent);del candidate["selection"]
        candidate["window_id"]=context["feasible_windows"][0]["id"]
        resolved=resolve_selection(candidate,context)
        expected={k:context["feasible_windows"][0][k] for k in ("first_word_id","last_word_id")}
        self.assertEqual(expected,resolved["selection"])
        self.assertNotIn("selection",candidate)
        candidate["window_id"]="window_999"
        with self.assertRaises(CompilationError):resolve_selection(candidate,context)
        with self.assertRaises(CompilationError):resolve_selection(self.intent,context)

    def test_api_request_uses_bounded_schema_and_safe_errors(self):
        context=planning_context(self.source,PROFILE)
        from unittest.mock import Mock
        response=Mock(status_code=403)
        response.json.return_value={"error":{"message":"private-request-contents"}}
        provider=GeminiProvider("test-placeholder-not-a-key")
        with patch("cre_brain.providers.gemini.requests.post",return_value=response) as post:
            with self.assertRaisesRegex(Exception,"HTTP 403") as caught:
                provider.generate_plan(schema={},profile=PROFILE,source_analysis=self.source,asset_registry={},
                    renderer_capabilities={"planning_context":{"editorial_intent":context}})
        config=post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual([w["id"] for w in context["feasible_windows"]],config["responseJsonSchema"]["properties"]["window_id"]["enum"])
        self.assertNotIn("private-request-contents",str(caught.exception))

    def test_intent_prompt_excludes_paths_and_render_arithmetic(self):
        context=planning_context(self.source,PROFILE)
        prompt=GeminiProvider._prompt({},PROFILE,self.source,{"private_path":"DO_NOT_SEND"},
                {"planning_context":{"editorial_intent":context}},None)
        self.assertNotIn("DO_NOT_SEND",prompt)
        self.assertIn("first_word_id",prompt)
        self.assertNotIn("output_start_frame",prompt)

    def test_failed_retries_preserve_existing_plan(self):
        import cre_format3 as f
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)
            for name,value in [("edit-plan.json",self.baseline),("source-analysis.json",self.source),
                               ("asset-registry.json",{}),("renderer-capabilities.json",{})]:
                (job/name).write_text(json.dumps(value))
            original=(job/"edit-plan.json").read_bytes()
            invalid=GenerationResult(plan={"render":{}},provider="test",model="test",latency_ms=1)
            with patch.object(f,"_capabilities_for_job",return_value={}),patch.object(f,"_gemini_key",return_value="test-placeholder-not-a-key"),patch.object(f.GeminiProvider,"generate_plan",return_value=invalid) as generate:
                plan,report,generation=f._generate_gemini_plan(job,"test")
            self.assertIsNone(plan)
            self.assertEqual(3,generate.call_count)
            self.assertFalse(generation["accepted"])
            self.assertEqual("rejected",report["status"])
            self.assertEqual(original,(job/"edit-plan.json").read_bytes())
            self.assertEqual(3,len(list(job.glob("gemini-original-*.json"))))
            self.assertFalse((job/"ai-generation.json").exists())


if __name__ == "__main__": unittest.main()
