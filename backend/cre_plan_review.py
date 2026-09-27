"""Human review of deterministic Format 3 instructions; renderer stays unchanged."""

from copy import deepcopy

from datetime import datetime, timezone

import hashlib

import html

import io

import json
import os
import re

from pathlib import Path

import shutil

import subprocess

import uuid



import streamlit as st

from cre_caption_styles import CAPTION_STYLES, STYLE_MAP, bind_style_fonts





def read(path):

    return json.loads(Path(path).read_text(encoding="utf-8"))





def stamp():

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]





def write(path, data):

    path = Path(path)

    if path.exists():

        shutil.copy2(path, path.with_name(path.stem + ".backup-" + stamp() + path.suffix))

    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")

    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    tmp.replace(path)





def preset_caps(caps, style_id):

    caps = deepcopy(caps)

    preset = deepcopy(STYLE_MAP[style_id])

    bind_style_fonts(preset)

    font = preset["font_binding"]

    font["sha256"] = hashlib.sha256(Path(font["path"]).read_bytes()).hexdigest()

    for weight in preset.get("weight_fonts", {}).values():

        if isinstance(weight, dict) and weight.get("path"):

            weight["sha256"] = hashlib.sha256(Path(weight["path"]).read_bytes()).hexdigest()
    plain_font = preset.get("plain_caption_preset", {}).get("font_binding")
    if plain_font:
        plain_font["sha256"] = hashlib.sha256(Path(plain_font["path"]).read_bytes()).hexdigest()
    caps["caption_preset"] = preset

    caps["font"] = {k: font[k] for k in ("asset_id", "path", "sha256")}

    return caps





def validate(job, plan, caps):
    from cre_brain import accept_plan
    return accept_plan(json.dumps(plan, ensure_ascii=False).encode(), read(job / "asset-registry.json"),

                       read(job / "source-analysis.json"), caps)





def approve(job, plan, caps, base_hash):

    if hashlib.sha256((job / "edit-plan.json").read_bytes()).hexdigest() != base_hash:

        raise ValueError("The planning baseline changed. Reload Plan Review before approving.")

    result = validate(job, plan, caps)

    if not result.accepted:

        raise ValueError("Review has validation issues; approval is blocked.")

    folder = job / "reviews" / stamp()

    folder.mkdir(parents=True)

    write(folder / "reviewed-plan.json", plan)

    write(folder / "capabilities.json", caps)

    write(folder / "validation-report.json", result.report())

    (folder / "snapshot.json").write_bytes(result.snapshot.json_bytes)

    manifest = {"version": "1.0.0", "review": folder.name, "baseline_sha256": base_hash,

                "snapshot_sha256": hashlib.sha256(result.snapshot.json_bytes).hexdigest(),

                "approved_at": datetime.now(timezone.utc).isoformat(), "approved": True}

    write(folder / "approval.json", manifest)

    write(job / "approved-review.json", manifest)

    return manifest





def approved_snapshot(job):

    manifest = read(job / "approved-review.json")

    if not manifest.get("approved") or not isinstance(manifest.get("review"), str) or not re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", manifest["review"]):

        raise ValueError("Invalid review approval.")

    if hashlib.sha256((job / "edit-plan.json").read_bytes()).hexdigest() != manifest["baseline_sha256"]:

        raise ValueError("A new planning baseline needs review.")

    folder = job / "reviews" / manifest["review"]

    result = validate(job, read(folder / "reviewed-plan.json"), read(folder / "capabilities.json"))

    if not result.accepted or hashlib.sha256(result.snapshot.json_bytes).hexdigest() != manifest["snapshot_sha256"]:

        raise ValueError("Reviewed instructions or dependencies changed. Review and approve again.")

    if hashlib.sha256((folder / "snapshot.json").read_bytes()).hexdigest() != manifest["snapshot_sha256"]:

        raise ValueError("Approved snapshot was modified.")

    return folder / "snapshot.json", result





def regroup(plan, rows):

    plan = deepcopy(plan)

    words = [w for c in plan["render"]["captions"] for w in c["words"]]

    if len(words) != len(rows):

        raise ValueError("Every retained word must appear exactly once.")

    groups = []; last = None; seen = set()

    for word, row in zip(words, rows):

        group = int(row["Group"])

        if group < 1: raise ValueError("Caption group numbers must be positive.")

        if group != last:

            if group in seen: raise ValueError("A caption group must contain consecutive words.")

            seen.add(group); groups.append([]); last = group

        word["emphasis"] = bool(row["Emphasize"])

        groups[-1].append(word)

    plan["render"]["captions"] = [{"id": f"caption_{i}", "start_frame": ws[0]["start_frame"],

        "end_frame": ws[-1]["end_frame"], "words": ws} for i, ws in enumerate(groups, 1)]

    return plan





def timeline(plan, duration):
    bars = []

    for seg in plan["render"]["segments"]:

        a, b = seg["source_in_frame"], seg["source_out_frame"]

        bars.append(f'<div title="Kept {a/30:.2f}–{b/30:.2f}s" style="position:absolute;left:{a/duration*100}%;width:{(b-a)/duration*100}%;height:28px;background:#168b70;border-left:2px solid white"></div>')

    st.markdown('<div aria-label="Source cut map" style="position:relative;height:28px;background:#873e49;border-radius:5px;overflow:hidden">'+''.join(bars)+'</div>', unsafe_allow_html=True)

    st.caption(f"Source 0.00–{duration/30:.2f}s · Green = kept · Red = removed · White = segment boundary")


def editorial_summary(editorial):
    st.write(editorial.get("summary", "No editorial summary supplied."))
    if "confidence_milli" in editorial:
        st.caption(f"Editorial confidence: {editorial['confidence_milli']/10:.1f}% · Human review recommended: {editorial.get('review_recommended', True)}")
    for note in editorial.get("notes", []): st.write(note)
    for warning in editorial.get("warnings", []): st.warning(str(warning))


def decision_summary(plan):
    render = plan.get("render", {})
    for cue in render.get("captions", []):
        phrase = " ".join(("**" + html.escape(w.get("text", "")) + "**") if w.get("emphasis") else html.escape(w.get("text", "")) for w in cue.get("words", []))
        st.markdown(f"{cue.get('start_frame',0)/30:.2f}–{cue.get('end_frame',0)/30:.2f}s · {phrase}")
    st.caption("Crop decisions")
    for reframe in render.get("reframes", []):
        crop = reframe.get("crop", {})
        st.write(f"{reframe.get('segment_id', '')}: x {crop.get('x_px')}, y {crop.get('y_px')}, {crop.get('width_px')} × {crop.get('height_px')} px")
    st.caption("Punch-in decisions")
    if not render.get("punch_ins"): st.write("No punch-ins.")
    for event in render.get("punch_ins", []):
        zoom = max((k.get("scale_milli",1000) for k in event.get("keyframes",[])), default=1000)/1000
        st.write(f"{event.get('start_frame',0)/30:.2f}–{event.get('end_frame',0)/30:.2f}s · peak {zoom:.3f}×")


def intent_summary(intent, source):
    """Display editorial choices in words; raw intent is preserved separately on disk."""
    words = {f"w_{i:05d}": w for i,w in enumerate(source["words"])}
    selection = intent.get("selection", {})
    first, last = selection.get("first_word_id"), selection.get("last_word_id")
    if first not in words or last not in words:
        st.warning("The proposed selection contains unavailable word references.")
        return
    st.caption("Selected spoken excerpt (CRE adds safe boundary padding)")
    st.write(f'{words[first]["source_start_frame"]/30:.2f}–{words[last]["source_end_frame"]/30:.2f}s')
    st.write(" ".join(w["text"] for wid,w in words.items() if first <= wid <= last))
    st.caption("Preferred caption breaks")
    if not intent.get("caption_break_after_word_ids"): st.write("CRE chooses readable groups.")
    for wid in intent.get("caption_break_after_word_ids", []):
        if wid in words: st.write(f'After “{words[wid]["text"]}” at {words[wid]["source_end_frame"]/30:.2f}s source time')
    st.caption("Requested emphasis")
    st.write(", ".join(f'{words[wid]["text"]} ({words[wid]["source_start_frame"]/30:.2f}s)' for wid in intent.get("emphasis_word_ids", []) if wid in words) or "No emphasis requested.")
    st.caption("Requested punch-ins")
    if not intent.get("punch_ins"): st.write("No punch-ins requested.")
    for event in intent.get("punch_ins", []):
        word=words.get(event.get("anchor_word_id"))
        if word: st.write(f'{event["strength"].title()} zoom on “{word["text"]}” at {word["source_start_frame"]/30:.2f}s source time')
    st.caption("Requested pause shortening")
    if not intent.get("silence_cut_ids"): st.write("Keep existing pauses.")
    for silence_id in intent.get("silence_cut_ids", []):
        index=int(silence_id[8:])
        if index < len(source["safe_silence_ranges"]):
            a,b=source["safe_silence_ranges"][index]
            st.write(f"Shorten verified silence at {a/30:.2f}–{b/30:.2f}s")




@st.cache_data(show_spinner=False)

def crop_frame(path, mtime, frame, crop):

    from PIL import Image, ImageDraw

    result = subprocess.run([os.environ.get("CRE_FFMPEG", "ffmpeg"), "-v", "error", "-ss", str(frame/30), "-i", path,

        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True)

    original = Image.open(io.BytesIO(result.stdout)).convert("RGB")

    x,y,w,h = (crop[k] for k in ("x_px", "y_px", "width_px", "height_px"))
    center_x, center_y = x + w // 2, y + h // 2
    wide_h = min(original.height, h)
    wide_w = min(original.width, (wide_h * 16 // 9) // 2 * 2)
    wide_x = max(0, min(original.width - wide_w, center_x - wide_w // 2))
    wide_y = max(0, min(original.height - wide_h, y))
    wide = original.crop((wide_x, wide_y, wide_x + wide_w, wide_y + wide_h))
    wide.thumbnail((760, 428), Image.Resampling.LANCZOS)
    inset = Image.new("RGB", (1080, 1920), "black")
    inset.paste(wide, ((1080 - wide.width) // 2, (1920 - wide.height) // 2))
    guide = ImageDraw.Draw(original)
    guide.line((center_x, 0, center_x, original.height), fill="#00ffbb", width=3)
    guide.ellipse((center_x - 8, center_y - 8, center_x + 8, center_y + 8), outline="#00ffbb", width=3)
    return original, inset





def render_plan_review(app_dir, goto_page):

    import cre_format3 as f3

    st.title("Format 3 — Plan Review")

    st.caption("Review the story, refine each cut, and approve the exact instructions CRE will render.")

    jobs = [j for j in f3._jobs(app_dir) if f3._job_ready(j)]

    if not jobs: st.info("Prepare a source and generate a Brain Plan first."); return

    names = [j.name for j in jobs]

    selected = st.selectbox("Review job", names, index=names.index(st.session_state.get("selected_f3_job")) if st.session_state.get("selected_f3_job") in names else 0)

    st.session_state["selected_f3_job"] = selected

    job = next(j for j in jobs if j.name == selected)

    base_hash = hashlib.sha256((job / "edit-plan.json").read_bytes()).hexdigest()

    key = "review_" + selected + base_hash

    if key not in st.session_state:

        plan = read(job / "edit-plan.json"); caps = f3._capabilities_for_job(job)

        try:

            snapshot, _ = approved_snapshot(job)

            plan = read(snapshot.parent / "reviewed-plan.json")

        except (OSError, ValueError, KeyError): pass

        # The library's active Format 3 style starts each fresh review draft.
        # A later approval freezes it with the snapshot, so prior renders never change.
        st.session_state[key] = {"plan": plan, "caps": caps, "revision": 0}

    state = st.session_state[key]; plan = deepcopy(state["plan"])
    drafts = sorted((job / "review-drafts").glob("*.json"))
    if drafts and st.button("Restore latest saved draft"):
        saved = read(drafts[-1])
        if saved.get("baseline_sha256") != base_hash:
            st.error("This draft belongs to an older planning baseline.")
        else:
            state.update(plan=saved["plan"], caps=saved["capabilities"], revision=state["revision"]+1)
            st.rerun()
    source = read(job / "source-analysis.json")

    source_path = Path(read(job / "asset-registry.json")["assets"][plan["clip"]["source_asset_id"]]["path"])

    generation = read(job / "ai-generation.json") if (job / "ai-generation.json").exists() else {}

    mode = generation.get("attempts", [{}])[-1].get("compiler_mode", "local_seed_or_import")

    st.info("Compiler: " + mode + " · Review edits use explicit frame arithmetic, with no AI fallback.")

    if mode == "trusted_execution_scaffold": st.warning("Gemini editorial intent was retained, but CRE used the trusted execution scaffold for timing. Compare both columns below.")

    a,b,c = st.columns(3)

    a.metric("Output length", f'{plan["render"]["output"]["duration_frames"]/30:.2f}s')

    b.metric("Kept segments", len(plan["render"]["segments"]))

    c.metric("Caption groups", len(plan["render"]["captions"]))

    timeline(plan, source["duration_frames"])

    tabs = st.tabs(["Story & cuts", "Captions", "Framing & punch-ins", "Gemini vs CRE", "Validation & approval"])

    revkey = key + str(state["revision"])

    with tabs[0]:

        st.video(str(source_path))

        st.subheader("Clip window and kept segments")

        st.caption("Source time in seconds, rounded to the nearest 1/30 second. Segment gaps are removed. Applying cuts rebuilds captions from trusted words; inspect grouping afterward.")

        segs = plan["render"]["segments"]

        l,r = st.columns(2)

        clip_in = l.number_input("Clip start (source seconds)", value=segs[0]["source_in_frame"]/30, step=1/30, key=revkey+"in")

        clip_out = r.number_input("Clip end (source seconds)", value=segs[-1]["source_out_frame"]/30, step=1/30, key=revkey+"out")

        rows = st.data_editor([{"Start (s)": s["source_in_frame"]/30, "End (s)": s["source_out_frame"]/30} for s in segs], num_rows="dynamic", key=revkey+"segments", width="stretch")

        cuts_pending = (clip_in != segs[0]["source_in_frame"]/30 or clip_out != segs[-1]["source_out_frame"]/30 or rows != [{"Start (s)": s["source_in_frame"]/30, "End (s)": s["source_out_frame"]/30} for s in segs])

        if cuts_pending: st.warning("Cut changes are pending. Apply cuts to rebuild captions and validate before approval.")

        if st.button("Apply cuts and rebuild captions"):

            try:

                if not rows: raise ValueError("Keep at least one segment.")

                new = [{"id": f"segment_{i}", "source_in_frame": round(row["Start (s)"]*30), "source_out_frame": round(row["End (s)"]*30), "output_start_frame": 0} for i,row in enumerate(rows,1)]

                if clip_in != segs[0]["source_in_frame"]/30: new[0]["source_in_frame"] = round(clip_in*30)

                if clip_out != segs[-1]["source_out_frame"]/30: new[-1]["source_out_frame"] = round(clip_out*30)

                if any(s["source_in_frame"] < 0 or s["source_out_frame"] > source["duration_frames"] or s["source_in_frame"] >= s["source_out_frame"] for s in new): raise ValueError("Segment bounds must stay inside the source with start before end.")

                plan["render"]["segments"] = new

                asset = next(a for a in plan["render"]["assets"] if a["id"] == plan["clip"]["source_asset_id"])

                candidate, compiled = f3._compile_candidate(plan, source, asset, read(Path(app_dir)/"cre_brain/data/profile.json"), plan)

                if compiled == "trusted_execution_scaffold": raise ValueError("These cuts cannot form valid captions. Adjust the boundaries; your draft was preserved.")

                state["plan"] = candidate; state["revision"] += 1; st.rerun()

            except (ValueError, TypeError, KeyError) as exc: st.error(str(exc))

        st.subheader("Complete transcript · kept and removed")

        chunks=[]

        for word in source["words"]:

            kept=any(s["source_in_frame"] <= word["source_start_frame"] and word["source_end_frame"] <= s["source_out_frame"] for s in segs)

            if not chunks or chunks[-1][0] != kept: chunks.append((kept, []))

            chunks[-1][1].append(word)

        for kept, words in chunks:

            label="KEEP" if kept else "REMOVE"

            st.markdown(f'**{label} · {words[0]["source_start_frame"]/30:.2f}–{words[-1]["source_end_frame"]/30:.2f}s**')

            st.write(" ".join(w["text"] for w in words))

        st.subheader("Dead-space evidence")

        silence = source.get("safe_silence_ranges", [])

        if not silence: st.caption("No safe silence ranges recorded.")

        for start, end in silence:

            retained = any(s["source_in_frame"] < end and start < s["source_out_frame"] for s in segs)

            st.write(f"Silence {start/30:.2f}–{end/30:.2f}s · {'partly or fully retained' if retained else 'removed'}")

        for left,right in zip(segs,segs[1:]): st.write(f'Removed gap: {left["source_out_frame"]/30:.2f}–{right["source_in_frame"]/30:.2f}s → cut at output {right["output_start_frame"]/30:.2f}s')

    with tabs[1]:

        st.caption("Assign consecutive words the same group number to merge them, or a new number to split. Word text and timing stay tied to trusted transcription. Bold words below are emphasized.")

        rows=[{"Group": i, "Word": w["text"], "Start (s)": w["start_frame"]/30, "End (s)": w["end_frame"]/30, "Emphasize": w["emphasis"]} for i,cue in enumerate(plan["render"]["captions"],1) for w in cue["words"]]

        edited = st.data_editor(rows, disabled=["Word", "Start (s)", "End (s)"], key=revkey+"words", width="stretch", height=330)

        try: plan = regroup(plan, edited)

        except (ValueError, TypeError) as exc: st.error(str(exc)); return

        for cue in plan["render"]["captions"]:

            with st.container(border=True):

                st.caption(f'{cue["start_frame"]/30:.2f}–{cue["end_frame"]/30:.2f}s · output frames {cue["start_frame"]}–{cue["end_frame"]}')

                st.markdown(" ".join(("**"+html.escape(w["text"])+"**") if w["emphasis"] else html.escape(w["text"]) for w in cue["words"]))

        layout_slot = st.container()
    with tabs[2]:

        ids=list(STYLE_MAP); current=state["caps"]["caption_preset"]["id"]

        st.info("This draft starts with the active Format 3 library style. Approve a new reviewed version after choosing a style; already-rendered clips stay unchanged.")

        style=st.selectbox("Caption style for this reviewed version", ids, index=ids.index(current), format_func=lambda x: STYLE_MAP[x]["name"], key=revkey+"style")

        caps=preset_caps(state["caps"], style)

        # Local seed plans do not pass through the Gemini compilation step that
        # normally adds face-side caption anchors. Apply the same deterministic
        # placement pass during review so One-shot Face Stack behaves identically
        # for locally prepared and Gemini-planned sources.
        if caps["caption_preset"].get("layout_mode") == "one_shot_face_stack":
            if any(not cue.get("caption_placement") for cue in plan["render"]["captions"]):
                f3._apply_face_side_placements(job, plan, caps)

        from cre_caption_styles import _preview

        st.markdown(_preview(STYLE_MAP[style]), unsafe_allow_html=True)

        for i,reframe in enumerate(plan["render"]["reframes"]):

            st.subheader(f"Crop · {reframe['segment_id']}")

            crop=reframe["crop"]; asset=next(a for a in plan["render"]["assets"] if a["id"]==plan["clip"]["source_asset_id"])

            l,r=st.columns(2)

            crop["x_px"]=l.number_input("Horizontal focus position (px)", 0, max(0,asset["width_px"]-crop["width_px"]), crop["x_px"], 2, key=revkey+f"x{i}")

            crop["y_px"]=r.number_input("Vertical focus position (px)", 0, max(0,asset["height_px"]-crop["height_px"]), crop["y_px"], 2, key=revkey+f"y{i}")

            seg=plan["render"]["segments"][i]

            frame=st.number_input("Preview source frame", min_value=seg["source_in_frame"], max_value=max(seg["source_in_frame"],seg["source_out_frame"]-1), value=seg["source_in_frame"], key=revkey+f"frame{i}")

            try:

                original,cropped=crop_frame(str(source_path),source_path.stat().st_mtime,frame,crop)

                l.image(original, caption="Source with focus guide"); r.image(cropped, caption="Actual inset-wide 9:16 composition", width=220)

            except Exception: st.warning("Crop preview unavailable for this frame.")

        st.subheader("Punch-in events")

        st.caption("Output seconds. Zoom ramps up over 0.2 seconds, holds, then returns to 1× over 0.2 seconds. Duration 1.5–4 seconds; starts at least 5 seconds apart. Maximum 1.12×; events must stay inside one segment.")

        events=st.data_editor([{"Start (s)": e["start_frame"]/30,"End (s)":e["end_frame"]/30,"Zoom":max(k["scale_milli"] for k in e["keyframes"])/1000} for e in plan["render"]["punch_ins"]] or {"Start (s)":[],"End (s)":[],"Zoom":[]}, num_rows="dynamic", key=revkey+"punch", width="stretch")

        if isinstance(events, dict): events=[dict(zip(events,vals)) for vals in zip(*events.values())]

        original_events=plan["render"]["punch_ins"]

        original_rows=[{"Start (s)":e["start_frame"]/30,"End (s)":e["end_frame"]/30,"Zoom":max(k["scale_milli"] for k in e["keyframes"])/1000} for e in original_events]

        try:

            if events != original_rows:

                plan["render"]["punch_ins"]=[]

                for i,event in enumerate(events,1):

                    a,b,z=round(event["Start (s)"]*30),round(event["End (s)"]*30),round(event["Zoom"]*1000)

                    plan["render"]["punch_ins"].append({"id":f"punch_{i}","start_frame":a,"end_frame":b,"keyframes":[{"frame":a,"scale_milli":1000},{"frame":a+6,"scale_milli":z},{"frame":b-6,"scale_milli":z},{"frame":b,"scale_milli":1000}]})

        except (ValueError,TypeError): st.error("Complete every punch-in field."); return

    with tabs[3]:

        originals=sorted(job.glob("gemini-original-*.json"))

        original_name = generation.get("attempts", [{}])[-1].get("original_file")
        original_path = job / original_name if original_name and Path(original_name).name == original_name else (originals[-1] if originals else None)
        proposed=read(original_path) if original_path and original_path.exists() else None

        l,r=st.columns(2)

        with l:

            st.subheader("Gemini proposed")

            if proposed:

                editorial=proposed.get("editorial",{})

                editorial_summary(editorial)



                if proposed.get("intent_version") == "cre_editorial_intent_v1":
                    if "window_id" in proposed:
                        context_name = generation.get("planning_context_file", "")
                        if context_name and Path(context_name).name == context_name and (job / context_name).exists():
                            from cre_brain.compiler.editorial import resolve_selection
                            st.caption("Selected excerpt: " + proposed["window_id"])
                            intent_summary(resolve_selection(proposed, read(job / context_name)), source)
                        else:
                            st.warning("The preserved planning context is unavailable for this proposal.")
                    else:
                        intent_summary(proposed, source)
                else:
                    st.table([{"Source start (s)":s.get("source_in_frame",0)/30,"Source end (s)":s.get("source_out_frame",0)/30} for s in proposed.get("render",{}).get("segments",[])])
                    decision_summary(proposed)

            else: st.warning("This older job did not preserve the raw Gemini response. Its compiled plan is not a substitute for the original proposal. Generate a new plan to compare.")

        with r:

            st.subheader("CRE will execute")

            st.write(f'Compiler: {mode}; reviewed draft uses deterministic edits.')
            compiler_report = generation.get("attempts", [{}])[-1].get("compiler_report", {})
            if compiler_report.get("selection_preserved"):
                st.success("At initial compilation, Gemini's selected word range was preserved. No scaffold fallback was used. Human review edits are shown below.")
            for adjustment in compiler_report.get("diagnostics", []):
                st.warning(adjustment["message"])


            st.table([{"Source start (s)":s["source_in_frame"]/30,"Source end (s)":s["source_out_frame"]/30,"Output start (s)":s["output_start_frame"]/30} for s in plan["render"]["segments"]])

            st.write(f'{len(plan["render"]["captions"])} caption groups · {len(plan["render"]["punch_ins"])} punch-ins · {STYLE_MAP[style]["name"]}')

            decision_summary(plan)

        st.subheader("Editorial metadata")
        editorial_summary(plan.get("editorial",{}))

    with tabs[4]:

        result=validate(job,plan,caps)
        with layout_slot:
            with st.expander("Exact renderer line breaks"):
                if result.accepted:
                    for layout in result.snapshot.as_dict()["execution"]["text_layouts"]:
                        st.caption(layout["id"])
                        st.text("\n".join(layout["lines"]))
                else:
                    st.info("Resolve validation issues to calculate accepted renderer line breaks.")

        if cuts_pending: st.warning("Apply pending cut changes before approving. Validation below describes the currently compiled draft.")

        if result.accepted: st.success("Validation passed. This exact draft can be approved.")

        else: st.error("Approval blocked. Resolve the issues below.")

        for issue in result.report().get("issues",[]): st.warning(f'{issue.get("code")} · {issue.get("path")} · {issue.get("message")}')

        for warning in plan.get("editorial",{}).get("warnings",[]): st.warning(str(warning))

        st.caption("Each approval creates a new reviewed-plan file, frozen caption-style binding, validation report and execution snapshot. The Gemini plan remains unchanged.")

        if st.button("Save reviewed draft"):

            folder=job/"review-drafts"; folder.mkdir(exist_ok=True)

            write(folder/(stamp()+".json"),{"plan":plan,"capabilities":caps,"baseline_sha256":base_hash,"validation":result.report()})

            state.update(plan=plan,caps=caps); st.success("Versioned draft saved.")

        if st.button("Approve and Add to Render Queue", type="primary", disabled=not result.accepted or cuts_pending):

            try:

                approve(job,plan,caps,base_hash)

                queue=st.session_state.setdefault("current_jobs_f3",[])

                if job.name not in queue: queue.append(job.name)

                st.success("Reviewed version approved and queued."); goto_page("Render Queue")

            except ValueError as exc: st.error(str(exc))

