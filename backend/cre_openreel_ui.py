"""Template workspaces and the Open Reel handoff for CRE."""
import html
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import streamlit as st
from cre_openreel_bridge import ReelBridge, MEDIA_EXTENSIONS


@st.cache_resource
def get_bridge(app_dir, editor_url, cre_url):
    return ReelBridge(app_dir, editor_url, cre_url)


def bridge_for(app_dir):
    parsed = urlsplit(os.environ.get("CRE_APP_URL") or st.context.url)
    cre_url = f"{parsed.scheme}://{parsed.netloc}"
    return get_bridge(str(app_dir), os.environ.get("CRE_EDITOR_URL", "http://localhost:5173"),
                      cre_url)


def template_label(template):
    return f"#{template['number']:03d}"


def pool_files(folders):
    return sorted({p for folder in folders for p in Path(folder).rglob("*")
                   if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS})


def open_pool(app_dir, scope, folders):
    bridge = bridge_for(app_dir)
    st.session_state["reel_session_" + scope] = bridge.launch(scope, assets=[bridge.register(p) for p in pool_files(folders)])


def workspace_style():
    st.markdown("""
        <style>
            .stMainBlockContainer, .block-container {
                width: 100%; max-width: none;
                padding-left: 0.5rem; padding-right: 0.5rem; padding-bottom: 0.5rem;
            }
            iframe[title="Open Reel Editor"] {
                display: block; width: 100%;
                height: max(1000px, calc(100dvh - 4rem)); border: 0;
            }
        </style>
    """, unsafe_allow_html=True)


def show_editor(bridge, session_id, label="Open editor in separate tab"):
    url = bridge.session_url(session_id)
    st.link_button(label, url)
    st.markdown(f'<iframe title="Open Reel Editor" src="{html.escape(url, quote=True)}" '
                'allow="autoplay; clipboard-write" referrerpolicy="no-referrer" loading="lazy" '
                'sandbox="allow-scripts allow-same-origin allow-downloads allow-forms"></iframe>', unsafe_allow_html=True)


def render_pipeline(app_dir, scope, folders, goto_page):
    workspace_style()
    bridge = bridge_for(app_dir)
    paths = pool_files(folders)
    state_key = "reel_session_" + scope
    if st.button("Saved templates", icon=":material/video_library:"):
        goto_page("Templates")
    with st.expander("Editor media", expanded=state_key not in st.session_state):
        chosen = st.multiselect("Media", paths,
            format_func=lambda p: str(p.relative_to(Path(app_dir) / "assets")), key="reel_assets_" + scope)
        if st.button("Open in Reel Editor", type="primary"):
            with st.spinner("Preparing media..."):
                st.session_state[state_key] = bridge.launch(scope, assets=[bridge.register(p) for p in chosen])
            st.rerun()
    if state_key in st.session_state:
        show_editor(bridge, st.session_state[state_key][0])


def render_saved_settings(bridge, template):
    project = template["project"]
    items = {item["id"]: item for item in project["mediaLibrary"]["items"]}
    selected = st.multiselect("Replaceable media", list(items), default=template["slots"],
        format_func=lambda media_id: items[media_id]["name"], key="slots_" + template["id"])
    if st.button("Save media slots", key="save_slots_" + template["id"], icon=":material/save:"):
        bridge.set_slots(template["id"], selected)
        st.rerun()
    st.subheader("Project")
    settings = project["settings"]
    st.write(f"{settings['width']} x {settings['height']} | {settings['frameRate']} fps | {project['timeline']['duration']:.2f} seconds")
    rows = []
    for track in project["timeline"]["tracks"]:
        for clip in track["clips"]:
            transform = clip.get("transform", {})
            rows.append({"Track": track.get("name", ""), "Media": items.get(clip["mediaId"], {}).get("name", clip["mediaId"]),
                "Start": clip["startTime"], "Duration": clip["duration"], "In": clip["inPoint"], "Out": clip["outPoint"],
                "Position": json.dumps(transform.get("position", {})), "Scale": json.dumps(transform.get("scale", {})),
                "Crop": json.dumps(transform.get("crop", {})), "Rotation": transform.get("rotation", 0),
                "Opacity": transform.get("opacity", 1), "Volume": clip.get("volume", 1),
                "Effects": len(clip.get("effects", [])), "Keyframes": len(clip.get("keyframes", []))})
    st.subheader("Timeline")
    st.dataframe(rows, hide_index=True, use_container_width=True)
    if project.get("textClips"):
        st.subheader("Text")
        st.dataframe([{"Text": text.get("text", ""), "Start": text["startTime"], "Duration": text["duration"],
            "Style": json.dumps(text.get("style", {})), "Position": json.dumps(text.get("transform", {}))}
            for text in project["textClips"]], hide_index=True, use_container_width=True)
    st.download_button("Download template JSON", json.dumps(template, indent=2),
        file_name=f"template_{template['number']:03d}.json", mime="application/json", key="json_" + template["id"])


def prepare_rows(choices, count, register):
    if not choices or any(len(values) not in (1, count) for values in choices.values()):
        raise ValueError(f"Each slot needs one shared file or {count} files")
    assets = {slot: [register(value) for value in values] for slot, values in choices.items()}
    return [{slot: values[0 if len(values) == 1 else i] for slot, values in assets.items()} for i in range(count)]


def render_replicate(bridge, template, folders):
    template_id = template["id"]
    if not template["slots"]:
        st.warning("No replaceable media selected. Choose media slots under Saved settings.")
        return
    count = int(st.number_input("Number of clips", min_value=1, max_value=100, value=10, key="count_" + template_id))
    paths = pool_files(folders)
    items = {m["id"]: m for m in template["project"]["mediaLibrary"]["items"]}
    extensions = {"video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
                  "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
                  "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"}}
    choices = {}
    for index, slot in enumerate(template["slots"], 1):
        item = items[slot]
        st.subheader(f"{index}. {item['name']}")
        types = extensions[item["type"]]
        selected = st.multiselect("Existing footage", [p for p in paths if p.suffix.lower() in types],
            format_func=lambda p: f"{p.parent.name}/{p.name}", key=f"existing_{template_id}_{slot}")
        uploads = st.file_uploader("New footage", type=[ext[1:] for ext in sorted(types)],
            accept_multiple_files=True, key=f"uploads_{template_id}_{slot}")
        choices[slot] = list(selected) + list(uploads or [])
    valid = all(len(values) in (1, count) for values in choices.values())
    if not valid:
        st.warning(f"Each media slot needs 1 shared file or {count} files in output order.")
    else:
        st.dataframe([{"Clip": i + 1, **{f"{index}. {items[slot]['name']}":
            values[0 if len(values) == 1 else i].name for index, (slot, values) in enumerate(choices.items(), 1)}}
            for i in range(count)], hide_index=True, use_container_width=True)
    if st.button("Prepare replicas", type="primary", disabled=not valid, key="prepare_" + template_id):
        def register(value):
            return bridge.register(value) if isinstance(value, Path) else bridge.register_upload(value.getbuffer(), value.name)
        with st.spinner("Preparing replicas..."):
            rows = prepare_rows(choices, count, register)
            session_id, _ = bridge.launch(template["scope"], template=template, replacements=rows)
            st.session_state["batch_" + template_id] = session_id
        st.rerun()
    if "batch_" + template_id in st.session_state:
        st.link_button(f"Open {template_label(template)} renderer", bridge.session_url(st.session_state["batch_" + template_id]))


def render_results(bridge, template, queue_output):
    batches = list(reversed(bridge.batches(template["scope"], template["id"])))
    if not batches:
        st.info("No batches for this template yet.")
        return
    by_id = {batch["id"]: batch for batch in batches}
    selected_id = st.selectbox("Batch", list(by_id), format_func=lambda key: f"{len(by_id[key]['jobs'])} clips - {key[:8]}",
                              key="history_" + template["id"])
    batch = by_id[selected_id]
    jobs = bridge.jobs(batch["id"])
    st.dataframe([{"Clip": i + 1, "Status": job["status"], "Error": job.get("error", "")}
        for i, job in enumerate(jobs)], hide_index=True, use_container_width=True)
    if st.button("Refresh results", key="refresh_" + template["id"], icon=":material/refresh:"):
        st.rerun()
    unfinished = [j["replacements"] for j in jobs if j["status"] in ("failed", "pending", "cancelled")]
    if unfinished and not any(j["status"] == "running" for j in jobs):
        if st.button("Prepare unfinished clips again", key="retry_" + template["id"]):
            session_id, _ = bridge.launch(template["scope"], template=batch["template"], replacements=unfinished)
            st.session_state["batch_" + template["id"]] = session_id
            st.session_state["retry_ready_" + template["id"]] = session_id
            st.rerun()
    if "retry_ready_" + template["id"] in st.session_state:
        st.link_button("Open retry renderer", bridge.session_url(st.session_state["retry_ready_" + template["id"]]))
    for i, job in enumerate(jobs):
        if job["status"] != "complete":
            continue
        path = Path(job["output"])
        with st.expander(f"Clip {i + 1}"):
            if not path.exists():
                st.warning("The rendered file is no longer on disk.")
                continue
            st.video(str(path))
            with path.open("rb") as file:
                st.download_button("Download MP4", file, file_name=f"{template_label(template)}_{i + 1}.mp4",
                    mime="video/mp4", key="download_" + job["id"])
            queued = any(item["path"] == str(path) for item in st.session_state.get("auto_post_queue", []))
            if st.button("Queued" if queued else "Send to Auto Post", disabled=queued, key="post_" + job["id"]):
                queue_output(path)
                st.rerun()


def render_template_library(app_dir, active_format, folders_for_scope, queue_output, goto_page):
    workspace_style()
    bridge = bridge_for(app_dir)
    templates = [t for t in bridge.templates() if t["scope"] == active_format or t["scope"].startswith(active_format + "/")]
    st.title("Templates")
    left, right = st.columns([1, 1])
    if left.button("New template", icon=":material/add:"):
        goto_page("Editor")
    if right.button("Refresh templates", icon=":material/refresh:"):
        st.rerun()
    if not templates:
        st.info("No edited templates saved yet.")
        return
    tab_key = "reel_tabs_" + active_format
    focus_id = st.session_state.pop("reel_focus_template", None)
    labels = [template_label(t) for t in templates]
    focus = next((t for t in templates if t["id"] == focus_id), None)
    if focus:
        st.session_state[tab_key] = template_label(focus)
    elif st.session_state.get(tab_key) not in labels:
        st.session_state[tab_key] = labels[0]
    tabs = st.tabs(labels, key=tab_key, on_change="rerun")
    for template, tab in zip(templates, tabs):
        if not tab.open:
            continue
        with tab:
            st.subheader(f"{template_label(template)} - {template['name']}")
            replicate, edit, settings, results = st.tabs(["Replicate", "Edit", "Saved settings", "Results"])
            with replicate:
                render_replicate(bridge, template, folders_for_scope(template["scope"]))
            with edit:
                edit_key = "edit_" + template["id"]
                if st.button(f"Edit {template_label(template)} in Open Reel", key="launch_" + edit_key):
                    st.session_state[edit_key] = bridge.launch(template["scope"], template=template)[0]
                if edit_key in st.session_state:
                    st.link_button(f"Open {template_label(template)} editor", bridge.session_url(st.session_state[edit_key]))
            with settings:
                render_saved_settings(bridge, template)
            with results:
                render_results(bridge, template, queue_output)
