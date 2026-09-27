"""Compile untrusted word-based editorial intent into the unchanged Edit Plan v1.

No provider code, media I/O, executable expressions or fallback clips live here.
Word IDs are zero-based positions bound to the exact source hash.
"""
from bisect import bisect_left, bisect_right
from copy import deepcopy

from jsonschema import Draft202012Validator


VERSION = "cre_editorial_intent_v1"
MODE = "compiled_editorial_intent_v1"
WORD_ID = {"type": "string", "pattern": "^w_[0-9]{5}$"}


def id_list(item, limit):
    return {"type": "array", "items": item, "maxItems": limit, "uniqueItems": True}


INTENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["intent_version", "source_sha256", "title", "selection", "silence_cut_ids",
                 "caption_break_after_word_ids", "emphasis_word_ids", "punch_ins", "editorial"],
    "properties": {
        "intent_version": {"const": VERSION},
        "source_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "selection": {"type": "object", "additionalProperties": False,
                      "required": ["first_word_id", "last_word_id"],
                      "properties": {"first_word_id": WORD_ID, "last_word_id": WORD_ID}},
        "silence_cut_ids": id_list({"type": "string", "pattern": "^silence_[0-9]{5}$"}, 6),
        "caption_break_after_word_ids": id_list(WORD_ID, 150),
        "emphasis_word_ids": id_list(WORD_ID, 30),
        "punch_ins": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["anchor_word_id", "strength"],
            "properties": {"anchor_word_id": WORD_ID, "strength": {"enum": ["subtle", "medium"]}}}},
        "editorial": {"type": "object", "additionalProperties": False,
                      "required": ["summary", "confidence_milli", "warnings"],
                      "properties": {
                          "summary": {"type": "string", "minLength": 1, "maxLength": 600},
                          "confidence_milli": {"type": "integer", "minimum": 0, "maximum": 1000},
                          "warnings": id_list({"type": "string", "minLength": 1, "maxLength": 240}, 8)}}}}


class CompilationError(ValueError):
    def __init__(self, code, message, path="/intent"):
        super().__init__(message)
        self.issues = [{"code": code, "path": path, "message": message}]


def word_id(index):
    return f"w_{index:05d}"


def partition(words, limits, preferred_breaks=()):
    """Iterative shortest path: respect requested breaks when feasible, never retime words."""
    preferred = set(preferred_breaks)
    n = len(words)
    best = [None] * (n + 1)
    best[n] = (0, [])
    for left in range(n - 1, -1, -1):
        for right in range(left + 1, min(n, left + limits["max_words_per_caption"]) + 1):
            chunk = words[left:right]
            duration = chunk[-1]["end_frame"] - chunk[0]["start_frame"]
            chars = len(" ".join(w["text"] for w in chunk))
            if not (limits["min_caption_frames"] <= duration <= limits["max_caption_frames"]
                    and chars <= limits["max_caption_characters"]
                    and chars * limits["fps"] <= limits["max_caption_characters_per_second"] * duration):
                continue
            if best[right] is None:
                continue
            # Missed requested boundaries dominate cue count. Tie breaks are stable.
            missed = sum(w["word_id"] in preferred for w in chunk[:-1])
            cost = best[right][0] + missed * 1000 + 1
            if best[left] is None or cost < best[left][0]:
                best[left] = (cost, [chunk] + best[right][1])
    return best[0][1] if best[0] is not None else None


def mapped_words(source, first, last, offset=0):
    return [{"word_id": word_id(i), "text": source["words"][i]["text"],
             "start_frame": source["words"][i]["source_start_frame"] + offset,
             "end_frame": source["words"][i]["source_end_frame"] + offset}
            for i in range(first, last + 1)]


def feasible_windows(source, profile, limit=96):
    """Bounded, distributed candidate catalog with safe outer guards and valid captions.

    Catalog entries are feasible timing choices, not claims about story quality.
    """
    words = source["words"]; h = profile["hard_limits"]
    guard = (60 * h["fps"] + 999) // 1000
    starts = [i for i, w in enumerate(words) if w["source_start_frame"] >= guard and
              (i == 0 or words[i-1]["source_end_frame"] <= w["source_start_frame"] - guard)]
    ends = [i for i, w in enumerate(words) if w["source_end_frame"] + guard <= source["duration_frames"] and
            (i+1 == len(words) or w["source_end_frame"] + guard <= words[i+1]["source_start_frame"])]
    # Sample the complete source rather than only the opening for long podcasts.
    if len(starts) > 512:
        starts = [starts[i * (len(starts)-1) // 511] for i in range(512)]
    end_frames = [words[i]["source_end_frame"] + guard for i in ends]
    candidates = []
    for first in starts:
        start = words[first]["source_start_frame"] - guard
        lo = bisect_left(end_frames, start + h["min_duration_frames"])
        hi = bisect_right(end_frames, start + h["max_duration_frames"])
        possible = sorted(ends[lo:hi], key=lambda i: (abs(words[i]["source_end_frame"] + guard - start - 750), i))[:8]
        for last in possible:
            if last < first: continue
            end = words[last]["source_end_frame"] + guard
            if partition(mapped_words(source, first, last), h) is not None:
                candidates.append({"first_word_id": word_id(first), "last_word_id": word_id(last),
                                   "source_in_frame": start, "source_out_frame": end,
                                   "duration_frames": end-start})
                # Keep a few endpoint alternatives without flooding the model.
                if sum(c["first_word_id"] == word_id(first) for c in candidates[-8:]) >= 2: break
    if len(candidates) > limit:
        candidates = [candidates[i * (len(candidates)-1) // (limit-1)] for i in range(limit)]
    return candidates


def planning_context(source, profile):
    windows = feasible_windows(source, profile)
    if not windows:
        raise CompilationError("NO_FEASIBLE_EXCERPT", "No guard-safe 20–30 second word range can form valid captions. Review transcription or prepare another source.")
    for i, window in enumerate(windows):
        window["id"] = f"window_{i:03d}"
    schema = deepcopy(INTENT_SCHEMA)
    schema["required"] = ["window_id" if k == "selection" else k for k in schema["required"]]
    del schema["properties"]["selection"]
    schema["properties"]["window_id"] = {"type": "string", "enum": [w["id"] for w in windows]}
    schema["properties"]["source_sha256"] = {"type": "string", "enum": [source["source_sha256"]]}
    return {"intent_schema": schema, "source_sha256": source["source_sha256"],
            "words": [{"id": word_id(i), **w} for i, w in enumerate(source["words"])],
            "feasible_windows": windows,
            "silences": [{"id": f"silence_{i:05d}", "source_start_frame": a, "source_end_frame": b}
                         for i, (a, b) in enumerate(source["safe_silence_ranges"])],
            "rules": {"caption_breaks": "Preferences; CRE may regroup without changing word timings.",
                      "internal_cuts": "Only listed verified silences; never remove spoken words.",
                      "framing": "Preserve the local base crop; human reviews framing.",
                      "effects": "Optional word-anchored punch-ins; no external assets."}}


def resolve_selection(intent, context):
    """Resolve only an exact offered window ID; never repair a model's selection."""
    if not Draft202012Validator(context["intent_schema"]).is_valid(intent):
        raise CompilationError("INTENT_SCHEMA", "Choose one supplied window_id and use the editorial response schema. Do not invent a selection or timing fields.")
    selected = next(w for w in context["feasible_windows"] if w["id"] == intent["window_id"])
    resolved = deepcopy(intent)
    del resolved["window_id"]
    resolved["selection"] = {k: selected[k] for k in ("first_word_id", "last_word_id")}
    return resolved


def compile_intent(intent, source, asset, profile, baseline):
    errors = sorted(Draft202012Validator(INTENT_SCHEMA).iter_errors(intent), key=lambda e: str(e.path))
    if errors:
        # Avoid echoing untrusted values (or accidentally supplied credentials) into UI errors.
        path = "/intent/" + "/".join(str(p) for p in errors[0].path)
        raise CompilationError("INTENT_SCHEMA", "Editorial response does not match the word-based intent schema.", path)
    if intent["source_sha256"] != source["source_sha256"] or asset["sha256"] != source["source_sha256"]:
        raise CompilationError("INTENT_SOURCE_MISMATCH", "Editorial intent must reference this exact prepared source.")
    h = profile["hard_limits"]; words = source["words"]
    first, last = (int(intent["selection"][key][2:]) for key in ("first_word_id", "last_word_id"))
    if not 0 <= first <= last < len(words):
        raise CompilationError("INTENT_WORD_RANGE", "Choose ordered word IDs from this source.", "/intent/selection")
    guard = (60 * h["fps"] + 999) // 1000
    start, end = words[first]["source_start_frame"] - guard, words[last]["source_end_frame"] + guard
    if (start < 0 or end > source["duration_frames"] or
            (first and start < words[first-1]["source_end_frame"]) or
            (last+1 < len(words) and end > words[last+1]["source_start_frame"])):
        raise CompilationError("INTENT_CUT_GUARD", "This range has no room for safe speech boundaries. Choose a supplied feasible window.", "/intent/selection")
    selected_ids = {word_id(i) for i in range(first, last+1)}
    references = intent["caption_break_after_word_ids"] + intent["emphasis_word_ids"] + [e["anchor_word_id"] for e in intent["punch_ins"]]
    if any(ref not in selected_ids for ref in references):
        raise CompilationError("INTENT_WORD_REFERENCE", "Caption, emphasis and punch-in word IDs must belong to the selected excerpt.")
    omissions = []
    for silence_id in intent["silence_cut_ids"]:
        index = int(silence_id[8:])
        if index >= len(source["safe_silence_ranges"]):
            raise CompilationError("INTENT_SILENCE_REFERENCE", "Choose a listed verified silence.")
        a, b = source["safe_silence_ranges"][index]
        if not start < a < b < end or (b-a)*1000 < 320*h["fps"]:
            raise CompilationError("INTENT_SILENCE_RANGE", "Requested silence must be internal to the excerpt and at least 320 ms.")
        before = [w for w in words[first:last+1] if w["source_end_frame"] <= a]
        after = [w for w in words[first:last+1] if w["source_start_frame"] >= b]
        if not before or not after or any(a < w["source_end_frame"] and w["source_start_frame"] < b for w in words):
            raise CompilationError("INTENT_UNSAFE_SILENCE", "The requested silence overlaps speech or lacks boundary words.")
        # Retain at least 60ms at each side, using only the trusted silence interval.
        cut_in = max(a, before[-1]["source_end_frame"] + guard)
        cut_out = min(b, after[0]["source_start_frame"] - guard)
        if cut_out <= cut_in:
            raise CompilationError("INTENT_UNSAFE_SILENCE", "The requested silence has no removable interior.")
        omissions.append((cut_in, cut_out))
    segments = []; cursor = start; output = 0
    for a,b in sorted(omissions) + [(end,end)]:
        if a-cursor < h["min_segment_frames"]:
            raise CompilationError("INTENT_SHORT_SEGMENT", "Requested silence cuts leave a segment shorter than the profile allows.")
        segments.append({"id": f"segment_{len(segments)+1}", "source_in_frame": cursor,
                         "source_out_frame": a, "output_start_frame": output})
        output += a-cursor; cursor = b
    if not h["min_duration_frames"] <= output <= h["max_duration_frames"]:
        raise CompilationError("INTENT_DURATION", "Selected excerpt after cuts must be 20–30 seconds. Choose another feasible window or fewer silence cuts.")
    chunks = []
    for segment in segments:
        indices = [i for i in range(first,last+1) if segment["source_in_frame"] <= words[i]["source_start_frame"] and words[i]["source_end_frame"] <= segment["source_out_frame"]]
        if not indices:
            raise CompilationError("INTENT_EMPTY_SEGMENT", "Every kept segment must contain speech.")
        mapped = mapped_words(source, indices[0], indices[-1], segment["output_start_frame"]-segment["source_in_frame"])
        grouped = partition(mapped, h, intent["caption_break_after_word_ids"])
        if grouped is None:
            raise CompilationError("INTENT_CAPTION_GROUPING", "This selection cannot form readable captions without changing trusted word timings. Choose another window or fewer silence cuts.")
        chunks.extend(grouped)
    diagnostics = []
    actual_breaks = {c[-1]["word_id"] for c in chunks}
    missed = sorted(set(intent["caption_break_after_word_ids"]) - actual_breaks)
    if missed:
        diagnostics.append({"code": "CAPTION_BREAKS_ADJUSTED", "word_ids": missed,
                            "message": "Some requested caption breaks were regrouped for timing and readability; spoken words and timings are unchanged."})
    budget = sum(map(len,chunks))*h["max_emphasized_word_ratio_milli"]//1000
    requested_emphasis = set(intent["emphasis_word_ids"]); actual_emphasis = []
    captions = []
    for index, chunk in enumerate(chunks,1):
        used = 0; caption_words = []
        for word in chunk:
            emphasized = word["word_id"] in requested_emphasis and used < h["max_emphasized_words_per_caption"] and len(actual_emphasis) < budget
            if emphasized: actual_emphasis.append(word["word_id"]); used += 1
            caption_words.append({k:word[k] for k in ("text","start_frame","end_frame")} | {"emphasis":emphasized})
        captions.append({"id":f"caption_{index}","start_frame":chunk[0]["start_frame"],
                         "end_frame":chunk[-1]["end_frame"],"words":caption_words})
    omitted_emphasis = sorted(requested_emphasis-set(actual_emphasis))
    if omitted_emphasis:
        diagnostics.append({"code":"EMPHASIS_BUDGET", "word_ids":omitted_emphasis,
                            "message":"Some requested emphasis was omitted to respect the one-word-per-cue and total emphasis limits."})
    punch_ins = []; mapped_by_id = {w["word_id"]:w for chunk in chunks for w in chunk}
    for event in sorted(intent["punch_ins"],key=lambda e:mapped_by_id[e["anchor_word_id"]]["start_frame"]):
        anchor = mapped_by_id[event["anchor_word_id"]]["start_frame"]
        segment = next(s for s in segments if s["output_start_frame"] <= anchor < s["output_start_frame"]+s["source_out_frame"]-s["source_in_frame"])
        lo = segment["output_start_frame"]; hi = lo+segment["source_out_frame"]-segment["source_in_frame"]
        duration = min(60, hi-lo, h["max_punch_frames"])
        a = max(lo,min(anchor-h["min_zoom_ramp_frames"],hi-duration)); b = a+duration
        if duration < h["min_punch_frames"] or (punch_ins and (a-punch_ins[-1]["start_frame"] < h["min_punch_start_spacing_frames"] or a < punch_ins[-1]["end_frame"])):
            diagnostics.append({"code":"PUNCH_OMITTED", "word_ids":[event["anchor_word_id"]],
                                "message":"A requested punch-in was omitted because it conflicts with a cut or another punch-in."})
            continue
        z = {"subtle":1060,"medium":1080}[event["strength"]]; ramp=h["min_zoom_ramp_frames"]
        punch_ins.append({"id":f"punch_{len(punch_ins)+1}","start_frame":a,"end_frame":b,
                          "keyframes":[{"frame":a,"scale_milli":1000},{"frame":a+ramp,"scale_milli":z},
                                       {"frame":b-ramp,"scale_milli":z},{"frame":b,"scale_milli":1000}]})
    plan=deepcopy(baseline); render=plan["render"]
    render.update(assets=[deepcopy(asset)],segments=segments,captions=captions,punch_ins=punch_ins,
                  b_roll=[],music=None,sfx=[],transitions=[],overlays=[])
    render["output"]["duration_frames"]=output
    # Reuse local framing, never ask a text-only model to guess a face position.
    old_crops={r["segment_id"]:r["crop"] for r in baseline["render"]["reframes"]}
    render["reframes"]=[]
    for i,s in enumerate(segments,1):
        prior=next((old for old in baseline["render"]["segments"] if old["source_in_frame"] <= s["source_in_frame"] < old["source_out_frame"]),baseline["render"]["segments"][0])
        render["reframes"].append({"id":f"reframe_{i}","segment_id":s["id"],"crop":deepcopy(old_crops[prior["id"]])})
    plan["clip"]["title"]=intent["title"]
    plan["editorial"]={"confidence_milli":intent["editorial"]["confidence_milli"],"review_recommended":True,
                       "summary":intent["editorial"]["summary"],"notes":[],
                       "warnings":intent["editorial"]["warnings"] + ["Base crop retained from the local plan; inspect subject framing across the selected excerpt."] + [d["message"] for d in diagnostics][:7]}
    report={"compiler_mode":MODE,"fallback_used":False,"selection_preserved":True,
            "source_word_range":intent["selection"],"diagnostics":diagnostics,
            "caption_groups":[[w["word_id"] for w in c] for c in chunks],
            "emphasized_word_ids":actual_emphasis,"duration_frames":output}
    return plan, report
