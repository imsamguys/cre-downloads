import hashlib
import json
import os
import subprocess
import textwrap
from pathlib import Path
from PIL import ImageFont
from ..compiler.snapshot import file_hash


ADAPTER_ID = 'cre_ffmpeg_adapter_v1'
ADAPTER_VERSION = '1.12.0'
SCALER_VERSION = 'ffmpeg-full-width-inner-zoom-v1'
CAPTION_LAYOUT_VERSION = 'pillow-caption-safe-zones-v7'
LINE_GAP_RATIO = 0.15
INSET_X = 160
INSET_Y = 746


def _escape_filter_path(path):
    return str(path).replace('\\', '/').replace(':', '\\:').replace("'", "\\'")


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return result


def _verify(snapshot_path, approval_path):
    raw = Path(snapshot_path).read_bytes()
    snap_hash = hashlib.sha256(raw).hexdigest()
    snapshot = json.loads(raw)
    approval = json.loads(Path(approval_path).read_bytes())
    if approval.get('version') != '1.0.0' or approval.get('snapshot_sha256') != snap_hash:
        raise ValueError('Approval does not bind this exact snapshot.')
    if approval.get('approved_for_review_render') is not True or not approval.get('approval_id'):
        raise ValueError('Explicit review-render approval is required.')
    execution = snapshot['execution']
    if execution['renderer_binding']['renderer_id'] != ADAPTER_ID:
        raise ValueError('Snapshot is bound to a different renderer adapter.')
    if execution['renderer_binding'].get('renderer_version') != ADAPTER_VERSION:
        raise ValueError('Snapshot is bound to a different renderer version.')
    if execution['renderer_binding'].get('scaler_version') != SCALER_VERSION:
        raise ValueError('Snapshot is bound to a different scaler version.')
    if execution['renderer_binding'].get('layout_engine') != CAPTION_LAYOUT_VERSION:
        raise ValueError('Snapshot is bound to a different caption layout version.')
    for asset in snapshot['resolved_assets']:
        if file_hash(asset['path']) != asset['sha256']:
            raise ValueError('Asset changed after acceptance: ' + asset['id'])
    return snapshot, approval, snap_hash


def _ffcolor(value):
    return '0x' + value.lstrip('#')


def word_width(text, font_path, fontsize):
    font = ImageFont.truetype(str(font_path), fontsize, layout_engine=ImageFont.Layout.BASIC)
    return font.getlength(text)


def _anchored_x(placement, measured_width):
    anchor_x = INSET_X + placement['anchor_x']
    return anchor_x - measured_width if placement['side'] == 'left' else anchor_x


def _caption_filters(render, layouts, font_path, workdir, style, input_label='zoomed'):
    if style.get('render_enabled') is False:
        return [], input_label
    size=style['font_size_px']; outline=style['outline_width_px']; box=style['box_px']; line_height=style['line_height_px']
    shadow_opacity=style.get('shadow_opacity_milli',0)/1000
    shadow=(f":shadowcolor={_ffcolor(style.get('shadow_color','#000000'))}@{shadow_opacity:.3f}"
            f":shadowx={style.get('shadow_x_px',0)}:shadowy={style.get('shadow_y_px',0)}")
    font = ImageFont.truetype(str(font_path), size, layout_engine=ImageFont.Layout.BASIC)
    font_arg = _escape_filter_path(font_path)
    stacked = style.get('layout_mode') == 'one_shot_face_stack'
    hero_id = next((c['id'] for c in render['captions'] if any(w.get('emphasis') for w in c['words'])),
                   render['captions'][0]['id'] if render['captions'] else None)
    stacked_fonts = {}
    if stacked:
        for weight in ('regular', 'heavy'):
            binding = style['weight_fonts'][weight]
            weight_path = Path(binding['path'])
            stacked_fonts[weight] = {
                'font': ImageFont.truetype(str(weight_path), style[f'{weight}_font_size_px'], layout_engine=ImageFont.Layout.BASIC),
                'arg': _escape_filter_path(weight_path),
                'size': style[f'{weight}_font_size_px'],
            }
    layout_map = {x['id']:x for x in layouts}
    filters=[]; label=input_label; text_index=0
    for caption in render['captions']:
        a=caption['start_frame']/30; b=caption['end_frame']/30
        enable=f"between(t,{a:.6f},{b-1/30000:.6f})"
        # Preserve the exact caption layout while hiding it during B-roll inserts.
        for insert in render.get('b_roll', []):
            ba=insert['start_frame']/30; bb=insert['end_frame']/30
            enable += f"*not(between(t,{ba:.6f},{bb-1/30000:.6f}))"
        alpha=""
        if style.get('animation') == 'smooth_fade':
            fade_in=min(0.12,max(0.04,(b-a)/3)); fade_out=min(0.10,max(0.04,(b-a)/3))
            alpha_expr=(f"if(lt(t,{a+fade_in:.6f}),(t-{a:.6f})/{fade_in:.6f},"
                        f"if(gt(t,{b-fade_out:.6f}),({b:.6f}-t)/{fade_out:.6f},1))")
            alpha=f":alpha='{alpha_expr}'"
        next_label=f'cap{text_index}'; text_index+=1
        opacity=style['background_opacity_milli']/1000
        if opacity > 0:
            filters.append(f"[{label}]drawbox=x={box['x']}:y={box['y']}:w={box['width']}:h={box['height']}:color={_ffcolor(style['background_color'])}@{opacity:.3f}:t=fill:enable='{enable}'[{next_label}]")
            label=next_label
        words=caption['words']; wi=0
        layout=layout_map[caption['id']]; lines=layout['lines']; box=layout['box_px']
        base_text_color=layout.get('text_color',style['text_color'])
        placement = layout.get('caption_placement')
        if stacked and caption['id'] != hero_id:
            plain=style['plain_caption_preset']; plain_size=plain['font_size_px']
            plain_path=Path(plain['font_binding']['path']); plain_font=ImageFont.truetype(str(plain_path),plain_size,layout_engine=ImageFont.Layout.BASIC)
            plain_arg=_escape_filter_path(plain_path)
            plain_shadow=(f":shadowcolor={_ffcolor(plain.get('shadow_color','#000000'))}@{plain.get('shadow_opacity_milli',0)/1000:.3f}:shadowx={plain.get('shadow_x_px',0)}:shadowy={plain.get('shadow_y_px',0)}")
            for line_no,line in enumerate(lines):
                width=plain_font.getlength(line)
                x=_anchored_x(placement,width) if placement and placement.get('side') in ('left','right') else box['x']+box['width']/2-width/2
                y=INSET_Y+placement['anchor_y']+line_no*plain['line_height_px'] if placement and placement.get('side') in ('left','right') else box['y']+line_no*plain['line_height_px']
                line_file=workdir/f'text_{text_index:04d}.txt';text_index+=1;line_file.write_text(line,encoding='utf-8')
                next_label=f'cap{text_index}';text_index+=1
                filters.append(f"[{label}]drawtext=fontfile='{plain_arg}':textfile='{_escape_filter_path(line_file)}':fontsize={plain_size}:fontcolor={_ffcolor(plain['text_color'])}:borderw=0{plain_shadow}{alpha}:x={x:.3f}:y={y}:enable='{enable}'[{next_label}]")
                label=next_label
            continue
        if stacked and placement and placement.get('side') in ('left', 'right'):
            regular_size = stacked_fonts['regular']['size']
            heavy_size = stacked_fonts['heavy']['size']
            heavy_y = INSET_Y + placement['anchor_y']
            regular_y = heavy_y - int(regular_size + heavy_size * LINE_GAP_RATIO)
            line_styles = layout['line_styles']
            word_cursor = 0
            for line, line_style in zip(lines, line_styles):
                line_words = line.split(' ')
                line_font = stacked_fonts[line_style]['font']
                line_font_arg = stacked_fonts[line_style]['arg']
                line_size = stacked_fonts[line_style]['size']
                line_y = regular_y if line_style == 'regular' else heavy_y
                line_width = line_font.getlength(line)
                line_x = _anchored_x(placement, line_width)
                if line_style == 'regular':
                    line_file=workdir/f'text_{text_index:04d}.txt'; text_index+=1
                    line_file.write_text(line,encoding='utf-8')
                    next_label=f'cap{text_index}'; text_index+=1
                    filters.append(f"[{label}]drawtext=fontfile='{line_font_arg}':textfile='{_escape_filter_path(line_file)}':fontsize={line_size}:fontcolor={_ffcolor(base_text_color)}:borderw={outline}:bordercolor={_ffcolor(style['outline_color'])}{shadow}{alpha}:x={line_x:.3f}:y={line_y}:enable='{enable}'[{next_label}]")
                    label=next_label
                    word_cursor += len(line_words)
                    continue
                cursor_x = line_x
                heavy_path = style['weight_fonts']['heavy']['path']
                for displayed in line_words:
                    word = words[word_cursor]; word_cursor += 1
                    word_file=workdir/f'text_{text_index:04d}.txt'; text_index+=1
                    word_file.write_text(displayed,encoding='utf-8')
                    color = (style.get('emphasis_color_override', style['emphasis_color'])
                             if word['emphasis'] else base_text_color)
                    next_label=f'cap{text_index}'; text_index+=1
                    filters.append(f"[{label}]drawtext=fontfile='{line_font_arg}':textfile='{_escape_filter_path(word_file)}':fontsize={line_size}:fontcolor={_ffcolor(color)}:borderw={outline}:bordercolor={_ffcolor(style['outline_color'])}{shadow}{alpha}:x={cursor_x:.3f}:y={line_y}:enable='{enable}'[{next_label}]")
                    label=next_label
                    cursor_x += word_width(displayed + ' ', heavy_path, heavy_size)
            if word_cursor != len(words):
                raise ValueError('Measured anchored caption layout no longer matches words.')
            continue
        if stacked:
            line_styles = layout['line_styles']
            total_height = sum(stacked_fonts[w]['size'] for w in line_styles) + style['stacked_gap_px'] * (len(lines)-1)
            top = box['y'] + (box['height'] - total_height) // 2
        else:
            line_styles = [None] * len(lines)
            top=box['y']+(box['height']-len(lines)*line_height)//2
        current_y = top
        for line_no,line in enumerate(lines):
            if stacked:
                line_style = line_styles[line_no]
                line_font = stacked_fonts[line_style]['font']
                line_font_arg = stacked_fonts[line_style]['arg']
                line_size = stacked_fonts[line_style]['size']
                y = current_y
                current_y += line_size + style['stacked_gap_px']
            else:
                line_font = font
                line_font_arg = font_arg
                line_size = size
                y=top+line_no*line_height
            line_words=line.split(' ')
            line_width=line_font.getlength(line)
            if placement and placement.get('side') in ('left','right'):
                x=_anchored_x(placement,line_width); y=INSET_Y+placement['anchor_y']+line_no*line_height
            elif placement and placement.get('side') == 'below':
                x=INSET_X+placement['anchor_x']-line_width/2
                y=INSET_Y+placement['anchor_y']+line_no*line_height
            else:
                x=box['x']+box['width']/2-line_width/2
            line_file=workdir/f'text_{text_index:04d}.txt'; text_index+=1
            line_file.write_text(line,encoding='utf-8')
            next_label=f'cap{text_index}'; text_index+=1
            filters.append(f"[{label}]drawtext=fontfile='{line_font_arg}':textfile='{_escape_filter_path(line_file)}':fontsize={line_size}:fontcolor={_ffcolor(base_text_color)}:borderw={outline}:bordercolor={_ffcolor(style['outline_color'])}{shadow}{alpha}:x={x:.3f}:y={y}:enable='{enable}'[{next_label}]")
            label=next_label
            cursor=x
            for displayed in line_words:
                word=words[wi]; wi+=1
                word_file=workdir/f'text_{text_index:04d}.txt'; text_index+=1
                word_file.write_text(displayed,encoding='utf-8')
                if word['emphasis']:
                    next_label=f'cap{text_index}'; text_index+=1
                    emphasis_color = style.get('emphasis_color_override', style['emphasis_color'])
                    filters.append(f"[{label}]drawtext=fontfile='{line_font_arg}':textfile='{_escape_filter_path(word_file)}':fontsize={line_size}:fontcolor={_ffcolor(emphasis_color)}:borderw={outline}:bordercolor={_ffcolor(style['outline_color'])}{shadow}{alpha}:x={cursor:.3f}:y={y}:enable='{enable}'[{next_label}]")
                    label=next_label
                wa=word['start_frame']/30; wb=word['end_frame']/30
                active=f"between(t,{wa:.6f},{wb-1/30000:.6f})"
                next_label=f'cap{text_index}'; text_index+=1
                if not (stacked and word['emphasis']):
                    filters.append(f"[{label}]drawtext=fontfile='{line_font_arg}':textfile='{_escape_filter_path(word_file)}':fontsize={line_size}:fontcolor={_ffcolor(style['active_word_color'])}:borderw={outline}:bordercolor={_ffcolor(style['outline_color'])}{shadow}{alpha}:x={cursor:.3f}:y={y}:enable='{active}'[{next_label}]")
                    label=next_label
                cursor += line_font.getlength(displayed+' ')
        if wi != len(words): raise ValueError('Measured caption layout no longer matches words.')
    return filters,label


def render_review(snapshot_path, approval_path, output_path, *, font_path, ffmpeg=None, ffprobe=None):
    ffmpeg = ffmpeg or os.environ.get("CRE_FFMPEG", "ffmpeg")
    ffprobe = ffprobe or os.environ.get("CRE_FFPROBE", "ffprobe")
    snapshot, approval, snap_hash = _verify(snapshot_path, approval_path)
    execution=snapshot['execution']; render=execution['render']; fps=render['timebase']['fps']
    if fps != 30: raise ValueError('Adapter v1 supports 30 fps only.')
    resolved={x['id']:x for x in snapshot['resolved_assets']}
    source=resolved[execution['source_asset_id']]['path']
    referenced=[]
    for event in render.get('b_roll', []): referenced.append(event['asset_id'])
    if render.get('music'): referenced.append(render['music']['asset_id'])
    for event in render.get('sfx', []): referenced.append(event['asset_id'])
    referenced=list(dict.fromkeys(referenced))
    input_index={asset_id:index+1 for index,asset_id in enumerate(referenced)}
    font_path=Path(font_path)
    if file_hash(font_path) != execution['renderer_binding']['font']['sha256']:
        raise ValueError('Font changed after acceptance.')
    style=execution['renderer_binding'].get('caption_preset') or {}
    runtime=execution['renderer_binding'].get('caption_runtime', {})
    style['render_enabled']=runtime.get('enabled', True)
    if style.get('layout_mode') == 'one_shot_face_stack':
        for weight in ('regular', 'heavy'):
            binding = style.get('weight_fonts', {}).get(weight, {})
            if file_hash(binding.get('path', '')) != binding.get('sha256'):
                raise ValueError(f'Stacked Emphasis {weight} font changed after acceptance.')
        plain=style.get('plain_caption_preset',{}).get('font_binding',{})
        if file_hash(plain.get('path','')) != plain.get('sha256'):
            raise ValueError('Plain side-caption font changed after acceptance.')
    output_path=Path(output_path)
    if output_path.exists(): raise FileExistsError('Review output already exists: '+str(output_path))
    workdir=output_path.parent/(output_path.stem+'-adapter-work')
    workdir.mkdir(parents=True,exist_ok=True)
    chains=[]; concat=[]
    reframes={x['segment_id']:x['crop'] for x in render['reframes']}
    source_meta=resolved[execution['source_asset_id']]['metadata']
    for i,segment in enumerate(render['segments']):
        start=segment['source_in_frame']/fps; end=segment['source_out_frame']/fps
        duration=end-start; crop=reframes[segment['id']]
        source_w=source_meta['width_px']; source_h=source_meta['height_px']
        wide_h=min(source_h,crop['height_px'])
        wide_w=min(source_w,(wide_h*16//9)//2*2)
        center_x=crop['x_px']+crop['width_px']//2
        wide_x=max(0,min(source_w-wide_w,center_x-wide_w//2))
        wide_y=max(0,min(source_h-wide_h,crop['y_px']))
        chains.append(f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,crop={wide_w}:{wide_h}:{wide_x}:{wide_y},scale=1080:608:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:608,setsar=1[v{i}]")
        chains.append(f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.003,afade=t=out:st={duration-0.003:.6f}:d=0.003[a{i}]")
        concat.append(f'[v{i}][a{i}]')
    chains.append(''.join(concat)+f"concat=n={len(render['segments'])}:v=1:a=1[basewide][audio]")
    zoom=render['punch_ins'][0] if render['punch_ins'] else None
    if zoom:
        keys=zoom['keyframes']; a,up,hold,down=keys
        peak=up['scale_milli']/1000
        expr=(f"if(lt(on,{a['frame']}),1,if(lt(on,{up['frame']}),1+(on-{a['frame']})*({peak-1:.8f}/{up['frame']-a['frame']}),"
              f"if(lt(on,{hold['frame']}),{peak:.8f},if(lt(on,{down['frame']}),{peak:.8f}-(on-{hold['frame']})*({peak-1:.8f}/{down['frame']-hold['frame']}),1))))")
        chains.append(f"[basewide]zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x608:fps=30[zoomwide]")
    else:
        chains.append('[basewide]null[zoomwide]')
    # Zoom only the podcast picture, then place that fixed full-width panel on
    # the vertical canvas. Returning to 1.0x no longer shrinks the whole canvas.
    chains.append('[zoomwide]pad=1080:1920:0:(oh-ih)/2:color=black,setsar=1[zoomed]')
    video_base='zoomed'
    for i,event in enumerate(render.get('b_roll', [])):
        idx=input_index[event['asset_id']]
        duration=(event['end_frame']-event['start_frame'])/fps
        source_in=event['source_in_frame']/fps
        a=event['start_frame']/fps; b=event['end_frame']/fps
        # Offset the insert onto the output timeline. Without this, framesync has
        # already consumed the short clip by time the overlay is enabled and
        # repeats its final frame for the whole event.
        chains.append(f"[{idx}:v]trim=start={source_in:.6f}:duration={duration:.6f},setpts=PTS-STARTPTS+{a:.6f}/TB,scale=1080:608:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:608,pad=1080:1920:0:(oh-ih)/2:color=black,setsar=1[br{i}]")
        next_base=f'brbase{i}'
        chains.append(f"[{video_base}][br{i}]overlay=0:0:enable='between(t,{a:.6f},{b-1/30000:.6f})'[{next_base}]")
        video_base=next_base
    lut=execution['renderer_binding'].get('filter_preset') or {}
    if lut.get('enabled'):
        lut_path=Path(lut.get('path',''))
        if not lut_path.is_file() or file_hash(lut_path) != lut.get('sha256'):
            raise ValueError('Selected LUT changed or is missing after acceptance.')
        next_base='graded'
        chains.append(f"[{video_base}]lut3d=file='{_escape_filter_path(lut_path)}'[{next_base}]")
        video_base=next_base
    style=style or {'id':'podcast_pro','font_size_px':64,'text_color':'#FFFFFF','active_word_color':'#FFE066','emphasis_color':'#7DD3FC','outline_color':'#000000','outline_width_px':0,'shadow_color':'#000000','shadow_opacity_milli':550,'shadow_x_px':3,'shadow_y_px':4,'background_color':'#000000','background_opacity_milli':0,'box_px':{'x':90,'y':1350,'width':900,'height':240},'line_height_px':78,'case':'normal'}
    caption_chains,video_label=_caption_filters(render,execution['text_layouts'],font_path,workdir,style,video_base)
    chains.extend(caption_chains)
    for i,overlay in enumerate(render.get('overlays', [])):
        text=overlay['text']
        if overlay['position']=='top_safe':
            text='\n'.join(textwrap.wrap(text.upper(), 45)); y=485; size=38
        else:
            text='\n'.join(textwrap.wrap(text, 52)); y=1635; size=34
        text_file=workdir/f'overlay_{i:02d}.txt'; text_file.write_text(text,encoding='utf-8')
        a=overlay['start_frame']/fps; b=overlay['end_frame']/fps
        next_label=f'overlay{i}'
        chains.append(f"[{video_label}]drawtext=fontfile='{_escape_filter_path(font_path)}':textfile='{_escape_filter_path(text_file)}':fontsize={size}:fontcolor=white:borderw=2:bordercolor=black:line_spacing=5:x=(w-text_w)/2:y={y}:enable='between(t,{a:.6f},{b-1/30000:.6f})'[{next_label}]")
        video_label=next_label
    audio_inputs=['audio']
    if render.get('music'):
        music=render['music']; idx=input_index[music['asset_id']]
        duration=(music['end_frame']-music['start_frame'])/fps
        gain=max(k['gain_db'] for k in music['gain_keyframes'])
        fade=min(0.8,duration/4)
        chains.append(f"[{idx}:a]atrim=start={music['source_in_frame']/fps:.6f}:duration={duration:.6f},asetpts=PTS-STARTPTS,volume={gain}dB,afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0,duration-fade):.3f}:d={fade:.3f}[musicbed]")
        audio_inputs.append('musicbed')
    for i,event in enumerate(render.get('sfx', [])):
        idx=input_index[event['asset_id']]; duration=(event['end_frame']-event['start_frame'])/fps
        delay=round(event['start_frame']*1000/fps)
        fade_in=event['fade_in_frames']/fps; fade_out=event['fade_out_frames']/fps
        filters=f"atrim=start={event['source_in_frame']/fps:.6f}:duration={duration:.6f},asetpts=PTS-STARTPTS,volume={event['gain_db']}dB"
        if fade_in: filters+=f",afade=t=in:st=0:d={fade_in:.6f}"
        if fade_out: filters+=f",afade=t=out:st={max(0,duration-fade_out):.6f}:d={fade_out:.6f}"
        filters+=f",adelay={delay}|{delay}[sfx{i}]"
        chains.append(f"[{idx}:a]{filters}"); audio_inputs.append(f'sfx{i}')
    audio_label='audio'
    if len(audio_inputs)>1:
        audio_label='mixedaudio'
        chains.append(''.join(f'[{label}]' for label in audio_inputs)+f'amix=inputs={len(audio_inputs)}:duration=first:normalize=0[{audio_label}]')
    filter_file=workdir/'filter.txt'; filter_file.write_text(';\n'.join(chains),encoding='utf-8')
    command=[ffmpeg,'-hide_banner','-nostats','-i',source]
    for asset_id in referenced:
        command.extend(['-i',resolved[asset_id]['path']])
    command.extend(['-/filter_complex',str(filter_file),
             '-map',f'[{video_label}]','-map',f'[{audio_label}]','-r','30','-c:v','libx264','-preset','medium','-crf','18',
             '-pix_fmt','yuv420p','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',
             '-c:a','aac','-b:a','192k','-ar','48000','-ac','2','-movflags','+faststart',str(output_path)])
    _run(command)
    probe=json.loads(_run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(output_path)]).stdout)
    video=next(s for s in probe['streams'] if s['codec_type']=='video'); audio=next(s for s in probe['streams'] if s['codec_type']=='audio')
    expected=render['output']['duration_frames']; actual=int(video['nb_frames'])
    if actual != expected or video['avg_frame_rate'] != '30/1' or int(audio['sample_rate']) != 48000 or int(audio['channels']) != 2:
        raise RuntimeError(f'Render verification failed: expected {expected} frames, found {actual}.')
    report={'status':'rendered_for_review','publish_authorized':False,'adapter_id':ADAPTER_ID,'adapter_version':ADAPTER_VERSION,
            'snapshot_sha256':snap_hash,'approval_id':approval['approval_id'],'output_path':str(output_path),
            'output_sha256':file_hash(output_path),'frames':actual,'fps':'30/1','duration_seconds':actual/30,
            'width_px':int(video['width']),'height_px':int(video['height']),'audio_sample_rate':int(audio['sample_rate']),'audio_channels':int(audio['channels'])}
    (output_path.parent/(output_path.stem+'.report.json')).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report
