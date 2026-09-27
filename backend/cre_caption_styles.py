"""Shared, local caption-style catalog and per-format favorites."""
from copy import deepcopy
import html
import json
from pathlib import Path
import shutil
from datetime import datetime, timezone

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
FONT_DIR = APP_DIR / "assets" / "fonts"


def _font(name, family, *paths):
    return {"key": name, "family": family, "paths": list(paths)}


FONTS = {
    "montserrat_bold": _font("montserrat_bold", "Montserrat", FONT_DIR / "Montserrat.ttf", FONT_DIR / "Montserrat-Bold.ttf"),
    "cantarell_bold": _font("cantarell_bold", "Cantarell", FONT_DIR / "Cantarell-Bold.ttf"),
    "rubik_bold": _font("rubik_bold", "Rubik", FONT_DIR / "Rubik-Bold.ttf"),
    "cabinet_grotesk_bold": _font("cabinet_grotesk_bold", "Cabinet Grotesk", FONT_DIR / "CabinetGrotesk-Bold.ttf"),
    "resolve_regular": _font("resolve_regular", "Resolve", FONT_DIR / "Resolve-Regular-400.otf"),
    "inter": _font("inter", "Inter", FONT_DIR / "Montserrat.ttf"),
    "impact": _font("impact", "Impact", "C:/Windows/Fonts/impact.ttf"),
    "arial_black": _font("arial_black", "Arial Black", "C:/Windows/Fonts/ariblk.ttf"),
    "arial_bold": _font("arial_bold", "Arial", "C:/Windows/Fonts/arialbd.ttf"),
    "segoe_bold": _font("segoe_bold", "Segoe UI", "C:/Windows/Fonts/segoeuib.ttf"),
    "bahnschrift": _font("bahnschrift", "Bahnschrift", "C:/Windows/Fonts/bahnschrift.ttf"),
    "georgia_bold": _font("georgia_bold", "Georgia", "C:/Windows/Fonts/georgiab.ttf"),
    "consolas_bold": _font("consolas_bold", "Consolas", "C:/Windows/Fonts/consolab.ttf"),
    "trebuchet_bold": _font("trebuchet_bold", "Trebuchet MS", "C:/Windows/Fonts/trebucbd.ttf"),
    "comic_bold": _font("comic_bold", "Comic Sans MS", "C:/Windows/Fonts/comicbd.ttf"),
    "segoe_pair": _font("segoe_pair", "Segoe UI", "C:/Windows/Fonts/segoeuib.ttf"),
}

FONT_WEIGHT_PAIRS = {
    "segoe_pair": {
        "family": "Segoe UI",
        "regular": "C:/Windows/Fonts/segoeui.ttf",
        "heavy": "C:/Windows/Fonts/segoeuib.ttf",
    },
}


def _style(style_id, name, category, font, active, emphasis, *, text="#FFFFFF", outline="#000000",
           outline_width=4, shadow_opacity=0, shadow_x=0, shadow_y=0,
           background="#000000", opacity=550, size=64, y=1350, case="normal",
           animation="none", speaker2=None, description=""):
    return {"id": style_id, "name": name, "category": category, "font": font, "font_size_px": size,
            "text_color": text, "active_word_color": active, "emphasis_color": emphasis,
            "outline_color": outline, "outline_width_px": outline_width,
            "shadow_color": "#000000", "shadow_opacity_milli": shadow_opacity,
            "shadow_x_px": shadow_x, "shadow_y_px": shadow_y,
            "background_color": background, "background_opacity_milli": opacity,
            "box_px": {"x": 90, "y": y, "width": 900, "height": 240},
            "caption_boxes": {
                "upper": {"x": 90, "y": 300, "width": 900, "height": 240},
                "lower": {"x": 90, "y": 1350, "width": 900, "height": 240},
            },
            "speaker_colors": {"speaker_1": text, "speaker_2": speaker2 or text},
            "max_lines": 2, "line_height_px": round(size * 1.22), "case": case,
            "animation": animation,
            "description": description}


CAPTION_STYLES = [
    _style("black_shadow", "Black Shadow", "Format 3", "montserrat_bold", "#FFFFFF", "#FFFFFF",
           opacity=0, outline_width=0, shadow_opacity=500, shadow_x=3, shadow_y=4,
           size=46, animation="smooth_fade",
           description="Style 4: precise bold white text with a soft black shadow and no border."),
    _style("mono", "Mono", "Format 3", "resolve_regular", "#72F24B", "#72F24B",
           opacity=0, outline_width=0, shadow_opacity=520, shadow_x=3, shadow_y=4,
           size=46, animation="smooth_fade",
           description="Clean phrase captions with restrained green word emphasis."),
    _style("trending", "Trending", "Format 3", "cabinet_grotesk_bold", "#FDE047", "#FF4D4D",
           opacity=0, outline_width=0, shadow_opacity=540, shadow_x=3, shadow_y=4,
           size=48, animation="smooth_fade", speaker2="#7DD3FC",
           description="Bold reaction captions with emotion emphasis and speaker-aware color."),
    {**_style("one_shot_face_stack", "One-shot Face Stack", "Format 3", "segoe_pair", "#FFFFFF", "#FF3B30",
              opacity=0, outline_width=0, shadow_opacity=520, shadow_x=3, shadow_y=4,
              size=54, animation="smooth_fade", description="One mixed-weight hero caption; all other cues stay plain beside the face."),
     "layout_mode": "one_shot_face_stack", "regular_font_size_px": 32,
     "heavy_font_size_px": 54, "stacked_gap_px": -5,
     "emphasis_color_override": "#FF3B30", "font_pair_id": "segoe_pair",
     "face_aware_box_px": {"x": 160, "y": 746, "width": 360, "height": 150}},
    {**_style("cheek_stack", "Cheek Stack", "Format 3", "cabinet_grotesk_bold", "#FFFFFF", "#FDE047",
              opacity=0, outline_width=0, shadow_opacity=650, shadow_x=3, shadow_y=4,
              size=58, animation="smooth_fade", speaker2="#7DD3FC",
              description="One- or two-word captions centered directly below the active speaker's cheek."),
     "layout_mode": "cheek_stack",
     "face_aware_box_px": {"x": 160, "y": 746, "width": 520, "height": 110}},
    _style("podcast_pro", "Podcast Pro", "Clean Pro", "inter", "#FFE066", "#7DD3FC", opacity=0,
           outline_width=0, shadow_opacity=550, shadow_x=3, shadow_y=4,
           description="Clean word-highlight captions with a soft drop shadow and no background plate."),
    _style("clean_creator", "Clean Creator", "Clean Pro", "segoe_bold", "#4ADE80", "#60A5FA", opacity=420, description="Quiet, modern captions for talking-head clips."),
    _style("authority_blue", "Authority Blue", "Clean Pro", "arial_bold", "#38BDF8", "#93C5FD", background="#071525", opacity=700, description="Professional blue emphasis for education and business."),
    _style("executive_white", "Executive White", "Clean Pro", "bahnschrift", "#FFFFFF", "#D1D5DB", text="#F9FAFB", background="#111827", opacity=620, description="Restrained corporate typography with minimal color."),
    _style("studio_green", "Studio Green", "Clean Pro", "inter", "#34D399", "#A7F3D0", background="#052E24", opacity=650, description="Confident creator style with green active words."),
    _style("soft_signal", "Soft Signal", "Clean Pro", "trebuchet_bold", "#FDE68A", "#BFDBFE", background="#172033", opacity=520, outline_width=3, description="Friendly, readable, and less aggressive."),

    _style("bold_pop", "Bold Pop", "Viral Bold", "impact", "#FFE500", "#00E5FF", opacity=600, size=72, case="upper", description="Large uppercase captions for fast social hooks."),
    _style("yellow_punch", "Yellow Punch", "Viral Bold", "arial_black", "#FFF200", "#FF7A00", opacity=720, size=70, case="upper", description="High-contrast yellow word punches."),
    _style("red_alert", "Red Alert", "Viral Bold", "impact", "#FF3B30", "#FFD60A", background="#210000", opacity=700, size=72, case="upper", description="Urgent red emphasis for strong claims."),
    _style("mint_hook", "Mint Hook", "Viral Bold", "arial_black", "#2EF2B1", "#A7F3D0", background="#001D18", opacity=650, size=68, case="upper", description="Bright mint hook captions with a dark plate."),
    _style("orange_snap", "Orange Snap", "Viral Bold", "impact", "#FF8A00", "#FFE066", background="#211000", opacity=670, size=72, case="upper", description="Warm, energetic statement captions."),
    _style("white_crush", "White Crush", "Viral Bold", "arial_black", "#FFFFFF", "#FFE500", background="#000000", opacity=760, size=74, case="upper", description="Heavy monochrome type with selected gold emphasis."),

    _style("neon_punch", "Neon Punch", "Gaming", "arial_black", "#39FF14", "#FF4FD8", background="#12001C", opacity=690, size=68, case="upper", description="Neon green active words and magenta emphasis."),
    _style("electric_blue", "Electric Blue", "Gaming", "bahnschrift", "#00D9FF", "#A855F7", background="#030B22", opacity=720, size=68, case="upper", description="Cool electric captions for tech and gaming."),
    _style("level_up", "Level Up", "Gaming", "consolas_bold", "#A3FF12", "#FACC15", background="#101C05", opacity=700, size=62, case="upper", description="Arcade-flavored monospace without visual noise."),
    _style("hot_pink", "Hot Pink", "Gaming", "arial_black", "#FF4FD8", "#67E8F9", background="#200018", opacity=680, size=68, case="upper", description="Bright streamer-style color pairing."),
    _style("cyber_lime", "Cyber Lime", "Gaming", "consolas_bold", "#CCFF00", "#00E5FF", background="#08110A", opacity=740, size=64, case="upper", description="Sharp tech captions with compact type."),
    _style("purple_combo", "Purple Combo", "Gaming", "impact", "#C084FC", "#F0ABFC", background="#160428", opacity=700, size=70, case="upper", description="Deep purple plate with vivid active words."),

    _style("documentary_gold", "Documentary Gold", "Documentary", "georgia_bold", "#E8C56A", "#F5E6B3", background="#17130A", opacity=590, size=58, description="Editorial serif captions for thoughtful storytelling."),
    _style("newsroom", "Newsroom", "Documentary", "arial_bold", "#F43F5E", "#FCA5A5", background="#111827", opacity=680, size=62, description="Clear newsroom styling for facts and commentary."),
    _style("true_story", "True Story", "Documentary", "georgia_bold", "#FFFFFF", "#EAB308", background="#0C0A09", opacity=560, size=58, description="Calm documentary captions with gold emphasis."),
    _style("archive_type", "Archive Type", "Documentary", "consolas_bold", "#E7E5E4", "#F59E0B", background="#1C1917", opacity=630, size=58, description="Typewriter feel for archival and investigative clips."),
    _style("field_notes", "Field Notes", "Documentary", "trebuchet_bold", "#FDE68A", "#FDBA74", background="#29220E", opacity=600, size=60, description="Warm field-report typography."),
    _style("cold_fact", "Cold Fact", "Documentary", "bahnschrift", "#BAE6FD", "#FFFFFF", background="#071521", opacity=650, size=62, case="upper", description="Controlled all-caps treatment for factual reveals."),

    _style("minimal_white", "Minimal White", "Minimal", "inter", "#FFFFFF", "#D1D5DB", opacity=0, outline_width=3, size=60, description="No caption plate, just clean outlined text."),
    _style("minimal_yellow", "Minimal Yellow", "Minimal", "bahnschrift", "#FDE047", "#FEF9C3", opacity=0, outline_width=3, size=60, description="Simple yellow active-word highlight."),
    _style("lower_clean", "Lower Clean", "Minimal", "segoe_bold", "#93C5FD", "#FFFFFF", opacity=360, size=58, y=1450, description="Lower placement with a lighter background plate."),
    _style("subtle_mint", "Subtle Mint", "Minimal", "trebuchet_bold", "#6EE7B7", "#D1FAE5", opacity=250, outline_width=3, size=58, description="Soft green emphasis and minimal blocking."),
    _style("mono_simple", "Mono Simple", "Minimal", "consolas_bold", "#FFFFFF", "#67E8F9", opacity=300, outline_width=2, size=54, description="Compact monospace captions for technical clips."),
    _style("quiet_gray", "Quiet Gray", "Minimal", "segoe_bold", "#E5E7EB", "#FFFFFF", text="#E5E7EB", opacity=260, outline_width=2, size=58, description="Neutral gray typography for understated edits."),

    _style("luxury_gold", "Luxury Gold", "Luxury", "georgia_bold", "#D4AF37", "#FFF1A8", background="#0D0A02", opacity=680, size=60, description="Gold serif emphasis for premium positioning."),
    _style("midnight", "Midnight", "Luxury", "inter", "#A5B4FC", "#E0E7FF", background="#070A1A", opacity=720, size=62, description="Deep navy plate with quiet indigo highlights."),
    _style("champagne", "Champagne", "Luxury", "georgia_bold", "#F5D7A1", "#FFFFFF", background="#1A1208", opacity=600, size=58, description="Warm premium serif captions."),
    _style("emerald_class", "Emerald Class", "Luxury", "bahnschrift", "#6EE7B7", "#D1FAE5", background="#021B14", opacity=690, size=62, description="Dark emerald styling for polished business clips."),
    _style("silver_screen", "Silver Screen", "Luxury", "georgia_bold", "#E5E7EB", "#FFFFFF", background="#111111", opacity=620, size=58, description="Classic monochrome cinematic captions."),
    _style("royal_violet", "Royal Violet", "Luxury", "inter", "#C4B5FD", "#F5D0FE", background="#160B2E", opacity=670, size=62, description="Premium violet palette with modern type."),
]

STYLE_MAP = {style["id"]: style for style in CAPTION_STYLES}
DEFAULT_FAVORITES = {
    "Format 1": ["bold_pop", "clean_creator", "neon_punch", "orange_snap", "minimal_white"],
    "Format 2": ["yellow_punch", "red_alert", "electric_blue", "cold_fact", "white_crush"],
    "Format 3": ["black_shadow", "mono", "trending", "cheek_stack", "one_shot_face_stack"],
}

FORMAT_STYLE_IDS = {
    "Format 1": [style["id"] for style in CAPTION_STYLES if style["category"] != "Format 3"],
    "Format 2": [style["id"] for style in CAPTION_STYLES if style["category"] != "Format 3"],
    "Format 3": ["black_shadow", "mono", "trending", "cheek_stack", "one_shot_face_stack"],
}
FORMAT3_FONT_IDS = ["montserrat_bold", "cantarell_bold", "rubik_bold", "cabinet_grotesk_bold", "resolve_regular"]


def resolve_font(style):
    definition=FONTS[style["font"]]
    for candidate in definition["paths"]:
        if Path(candidate).is_file():
            return {"asset_id":"cre_caption_font_"+definition["key"]+"_v1", "path":str(Path(candidate)),
                    "family":definition["family"]}
    fallback=FONT_DIR / "Montserrat.ttf"
    return {"asset_id":"cre_caption_font_arial_bold_v1", "path":str(fallback), "family":"Arial"}


def resolve_font_pair(style):
    pair_id = style.get("font_pair_id")
    definition = FONT_WEIGHT_PAIRS.get(pair_id)
    if not definition:
        raise ValueError("This caption style does not define a regular/heavy font pair.")
    result = {"family": definition["family"]}
    for weight in ("regular", "heavy"):
        path = Path(definition[weight])
        if not path.is_file():
            raise ValueError(f"Missing {weight} font weight for {definition['family']}: {path}")
        result[weight] = {"asset_id": f"cre_caption_font_{pair_id}_{weight}_v1",
                          "path": str(path), "family": definition["family"]}
    return result


def bind_style_fonts(style):
    style["font_binding"] = resolve_font(style)
    if style.get("layout_mode") == "one_shot_face_stack":
        pair = resolve_font_pair(style)
        style["weight_fonts"] = pair
        style["font_binding"] = dict(pair["heavy"])
        fallback = deepcopy(STYLE_MAP["black_shadow"])
        style["plain_caption_preset"] = bind_style_fonts(fallback)
    return style


def preference_path(app_dir): return Path(app_dir)/"caption_style_preferences.json"


def load_preferences(app_dir):
    path=preference_path(app_dir); data={}
    if path.is_file():
        try: data=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): data={}
    result={"version":"1.0.0","formats":{}}
    for format_name,defaults in DEFAULT_FAVORITES.items():
        saved=data.get("formats",{}).get(format_name,{})
        allowed=FORMAT_STYLE_IDS[format_name]
        favorites=[x for x in saved.get("favorites",[]) if x in allowed][:5]
        for style_id in defaults:
            if len(favorites)>=5: break
            if style_id not in favorites: favorites.append(style_id)
        active=saved.get("active") if saved.get("active") in allowed else favorites[0]
        result["formats"][format_name]={"favorites":favorites,"active":active}
        if format_name == "Format 3":
            result["formats"][format_name]["font"] = saved.get("font") if saved.get("font") in FORMAT3_FONT_IDS else "montserrat_bold"
    return result


def save_preferences(app_dir, preferences):
    path=preference_path(app_dir); temporary=path.with_suffix(".tmp")
    temporary.write_text(json.dumps(preferences,indent=2)+"\n",encoding="utf-8"); temporary.replace(path)


def active_style(app_dir, format_name):
    prefs=load_preferences(app_dir); return deepcopy(STYLE_MAP[prefs["formats"][format_name]["active"]])


def resolved_caption_preset(app_dir, format_name, style_id=None):
    style=deepcopy(STYLE_MAP[style_id]) if style_id else active_style(app_dir,format_name)
    if format_name == "Format 3" and style.get("layout_mode") != "one_shot_face_stack":
        style["font"] = load_preferences(app_dir)["formats"][format_name]["font"]
    return bind_style_fonts(style)


def _preview(style, active=False, favorite=False):
    font=FONTS[style["font"]]["family"]; phrase="MAKE EVERY WORD COUNT" if style["case"]=="upper" else "Make every word count"
    words=phrase.split(); words[2]=f'<span style="color:{style["active_word_color"]}">{words[2]}</span>'
    text=" ".join(words)
    plate="transparent" if style["background_opacity_milli"]==0 else style["background_color"]
    return f'''<div style="height:190px;border:2px solid {'#22c55e' if active else '#273244'};border-radius:16px;padding:14px;background:linear-gradient(145deg,#263246,#0d1118);display:flex;flex-direction:column;justify-content:space-between">
    <div style="display:flex;justify-content:space-between;color:#cbd5e1;font-size:12px"><span>{html.escape(style['category'])}</span><span>{'ACTIVE' if active else ('★ FAVORITE' if favorite else '')}</span></div>
    <div style="background:{plate};padding:14px;border-radius:8px;text-align:center;color:{style['text_color']};font-family:'{font}',sans-serif;font-size:24px;font-weight:800;text-transform:{'uppercase' if style['case']=='upper' else 'none'};text-shadow:{style.get('shadow_x_px',0)}px {style.get('shadow_y_px',0)}px 5px rgba(0,0,0,{style.get('shadow_opacity_milli',0)/1000})">{text}</div>
    <div style="color:white;font-weight:700">{html.escape(style['name'])}</div></div>'''


def apply_to_openreel_templates(app_dir, format_name, style):
    root=Path(app_dir)/"openreel_pipeline"/"templates"; changed=0; clips=0
    backup=root/("caption-style-backup-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")); backup.mkdir(exist_ok=True)
    binding=resolve_font(style)
    openreel_style={"fontFamily":binding["family"],"fontSize":style["font_size_px"],"fontWeight":800,
        "fontStyle":"normal","color":style["text_color"],"strokeColor":style["outline_color"],
        "strokeWidth":style["outline_width_px"],"textAlign":"center","verticalAlign":"middle","lineHeight":1.2,"letterSpacing":-1}
    for path in root.glob("*.json"):
        value=json.loads(path.read_text(encoding="utf-8"))
        if not (value.get("scope")==format_name or str(value.get("scope","")).startswith(format_name+"/")): continue
        text_clips=value.get("project",{}).get("textClips",[])
        if not text_clips: continue
        shutil.copy2(path,backup/path.name)
        for clip in text_clips: clip["style"]={**clip.get("style",{}),**openreel_style}; clips+=1
        temporary=path.with_suffix(".tmp"); temporary.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8"); temporary.replace(path); changed+=1
    if not changed: backup.rmdir()
    return changed,clips


def render_caption_library(app_dir, format_name):
    st.title(f"✨ {format_name} — Caption Style Library")
    st.caption(f"{len(FORMAT_STYLE_IDS[format_name])} local, render-safe styles available for {format_name}.")
    prefs=load_preferences(app_dir); scoped=prefs["formats"][format_name]
    options=FORMAT_STYLE_IDS[format_name]
    selected=st.multiselect("Five favorites",options,default=scoped["favorites"],max_selections=5,
                            format_func=lambda x:STYLE_MAP[x]["name"],key="caption_faves_"+format_name)
    active=st.selectbox("Active style",options,index=options.index(scoped["active"]),
                        format_func=lambda x:STYLE_MAP[x]["name"],key="caption_active_"+format_name)
    selected_font = scoped.get("font")
    if format_name == "Format 3":
        selected_font=st.selectbox("Caption font",FORMAT3_FONT_IDS,index=FORMAT3_FONT_IDS.index(scoped["font"]),
                                   format_func=lambda x:FONTS[x]["family"],key="caption_font_"+format_name)
    left,right=st.columns(2)
    if left.button("Save favorites and active style",type="primary",use_container_width=True,key="save_caption_"+format_name):
        required=min(5,len(options))
        if len(selected)!=required: st.error(f"Choose exactly {required} favorites for this format.")
        else:
            scoped["favorites"]=selected; scoped["active"]=active
            if format_name == "Format 3": scoped["font"]=selected_font
            save_preferences(app_dir,prefs); st.success("Caption preferences saved."); st.rerun()
    if format_name in ("Format 1","Format 2"):
        if right.button("Apply active style to saved templates",use_container_width=True,key="apply_caption_"+format_name):
            changed,clips=apply_to_openreel_templates(app_dir,format_name,STYLE_MAP[active])
            st.success(f"Updated {clips} text layer(s) across {changed} saved template(s)." if changed else "No saved templates with text layers were found.")
    else: right.info("Choose One word or Multi word below to apply this style and word template directly to a Format 3 job.")
    st.divider()
    categories=["All"]+sorted({STYLE_MAP[style_id]["category"] for style_id in options})
    category=st.selectbox("Category",categories,key="caption_category_"+format_name)
    search=st.text_input("Search styles",placeholder="bold, clean, documentary, gaming...",key="caption_search_"+format_name).strip().casefold()
    shown=[s for s in CAPTION_STYLES if s["id"] in options and (category=="All" or s["category"]==category) and (not search or search in (s["name"]+" "+s["description"]+" "+s["category"]).casefold())]
    cols=st.columns(3)
    for index,style in enumerate(shown):
        with cols[index%3]:
            st.markdown(_preview(style,style["id"]==scoped["active"],style["id"] in scoped["favorites"]),unsafe_allow_html=True)
            font=resolve_font(style)
            st.caption(style["description"]+f" · {font['family']}")
            if st.button("Use this style",key=f"use_caption_{format_name}_{style['id']}",use_container_width=True):
                scoped["active"]=style["id"]; save_preferences(app_dir,prefs); st.rerun()
