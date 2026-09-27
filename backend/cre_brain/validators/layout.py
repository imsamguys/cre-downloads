from pathlib import Path
import PIL
from PIL import ImageFont
from ..compiler.snapshot import file_hash
from .report import Issue


LAYOUT_VERSION = 'pillow-caption-safe-zones-v7'
LINE_GAP_RATIO = 0.15


def measure_layout(plan, profile, capabilities):
    errors = []; layouts = []
    font_entry = capabilities.get('font')
    if not isinstance(font_entry, dict):
        return [Issue('FONT_REQUIRED', '/capabilities/font', 'Font must be resolved before layout.')], []
    if capabilities.get('layout_engine') != LAYOUT_VERSION or capabilities.get('pillow_version') != PIL.__version__:
        return [Issue('LAYOUT_ENGINE_MISMATCH', '/capabilities/layout_engine', 'Pin the installed Pillow BASIC layout implementation.', LAYOUT_VERSION + '/' + PIL.__version__)], []
    try:
        path = Path(font_entry.get('path', ''))
        if not path.is_absolute() or not path.is_file():
            raise ValueError('Font path must be an existing absolute file.')
        before = file_hash(path)
        caption_preset = capabilities.get('caption_preset')
        expected_asset = (caption_preset or {}).get('font_binding', {}).get('asset_id', 'cre_font_inter_bold_v1')
        if before != font_entry.get('sha256') or font_entry.get('asset_id') != expected_asset:
            raise ValueError('Font hash or asset ID differs from capability binding.')
        probe_font = ImageFont.truetype(str(path), size=64, layout_engine=ImageFont.Layout.BASIC)
        family, style = probe_font.getname()
        if not family or not style:
            raise ValueError('Font family/style metadata is missing.')
        weight_fonts = {}
        if (caption_preset or {}).get('layout_mode') == 'one_shot_face_stack':
            pair = caption_preset.get('weight_fonts', {})
            pair_family = pair.get('family')
            for weight in ('regular', 'heavy'):
                binding = pair.get(weight, {})
                weight_path = Path(binding.get('path', ''))
                if not weight_path.is_absolute() or not weight_path.is_file():
                    raise ValueError(f'Stacked Emphasis requires an existing absolute {weight} font path.')
                if file_hash(weight_path) != binding.get('sha256'):
                    raise ValueError(f'Stacked Emphasis {weight} font hash differs from its binding.')
                loaded = ImageFont.truetype(str(weight_path), size=64, layout_engine=ImageFont.Layout.BASIC)
                loaded_family, loaded_style = loaded.getname()
                if loaded_family != pair_family or not loaded_style:
                    raise ValueError('Stacked Emphasis regular and heavy weights must share one font family.')
                weight_fonts[weight] = weight_path
        hero_id = next((c['id'] for c in plan['render']['captions'] if any(w.get('emphasis') for w in c['words'])),
                       plan['render']['captions'][0]['id'] if plan['render']['captions'] else None)
        items = [(f'/render/captions/{i}', c['id'], ' '.join(w['text'] for w in c['words']), 'podcast_dynamic_v1', True, c.get('caption_placement'), c)
                 for i, c in enumerate(plan['render']['captions'])]
        items += [(f'/render/overlays/{i}', o['id'], o['text'], 'label_v1', False, None, o) for i, o in enumerate(plan['render']['overlays'])]
        for ptr, eid, text, style_id, is_caption, requested, item in items:
            if any(ord(c) < 32 or ord(c) > 126 for c in text):
                errors.append(Issue('GLYPH_UNSUPPORTED', ptr, 'This initial layout backend accepts printable ASCII only.'))
                continue
            preset = caption_preset if is_caption and caption_preset else profile['render_presets'][style_id]
            if is_caption and preset.get('layout_mode') == 'one_shot_face_stack' and eid != hero_id:
                preset = preset['plain_caption_preset']
            if preset.get('case') == 'upper':
                text = text.upper()
            anchored = requested and requested.get('side') in ('left', 'right', 'below')
            placement = ('face_aware' if anchored else
                         ('upper' if is_caption and text.rstrip().endswith('?') else 'lower'))
            boxes = preset.get('caption_boxes', {}) if is_caption else {}
            box = dict(boxes.get(placement, preset['box_px']))
            if anchored:
                box = dict(preset.get('face_aware_box_px', box))
                box['width'] = requested['max_text_width']
                if requested['side'] == 'left':
                    box['x'] = 160 + requested['anchor_x'] - box['width']
                elif requested['side'] == 'right':
                    box['x'] = 160 + requested['anchor_x']
                else:
                    box['x'] = 160 + requested['anchor_x'] - box['width'] // 2
                box['y'] = 746 + requested['anchor_y']
            speaker_role = capabilities.get('caption_runtime', {}).get('speaker_roles', {}).get(eid, 'speaker_1')
            speaker_colors = preset.get('speaker_colors', {})
            text_color = speaker_colors.get(speaker_role, preset.get('text_color', '#FFFFFF'))
            effective_path = Path(preset.get('font_binding', {}).get('path', path))
            font = ImageFont.truetype(str(effective_path), size=preset['font_size_px'], layout_engine=ImageFont.Layout.BASIC)
            outline = preset.get('outline_width_px', 0)
            width = box['width'] - 2 * outline
            line_styles = None
            if preset.get('layout_mode') == 'one_shot_face_stack':
                words = text.split(' ')
                split = 2 if len(words) > 3 else 1
                lines = [' '.join(words[:split]), ' '.join(words[split:])] if len(words) > 1 else [words[0]]
                line_styles = ['regular', 'heavy'] if len(lines) == 2 else ['heavy']
                sizes = {'regular': preset['regular_font_size_px'], 'heavy': preset['heavy_font_size_px']}
                measured = []
                for line, weight in zip(lines, line_styles):
                    line_font = ImageFont.truetype(str(weight_fonts[weight]), size=sizes[weight], layout_engine=ImageFont.Layout.BASIC)
                    measured.append((line_font, line))
                total_height = sum(sizes[w] for w in line_styles) + preset['stacked_gap_px'] * (len(lines) - 1)
                overflow = total_height + 2 * outline > box['height']
                for line_font, line in measured:
                    bounds = line_font.getbbox(line, stroke_width=outline)
                    overflow |= line_font.getlength(line) > width or bounds[2] - bounds[0] > box['width']
                if anchored:
                    regular_width = measured[0][0].getlength(measured[0][1]) if line_styles[0] == 'regular' else 0
                    heavy_index = line_styles.index('heavy')
                    heavy_width = measured[heavy_index][0].getlength(measured[heavy_index][1])
                    max_width = requested['max_text_width']
                    overflow |= regular_width > max_width or heavy_width > max_width
                    left = requested['anchor_x'] - max(regular_width, heavy_width) if requested['side'] == 'left' else requested['anchor_x']
                    right = requested['anchor_x'] if requested['side'] == 'left' else requested['anchor_x'] + max(regular_width, heavy_width)
                    heavy_y = requested['anchor_y']
                    regular_y = heavy_y - int(preset['regular_font_size_px'] + preset['heavy_font_size_px'] * LINE_GAP_RATIO)
                    overflow |= left < 0 or right > 760 or regular_y < 0 or heavy_y + preset['heavy_font_size_px'] > 428
            else:
                lines = []; line = ''
                for word in text.split(' '):
                    candidate = line + ' ' + word if line else word
                    if font.getlength(candidate) <= width:
                        line = candidate
                    else:
                        if line: lines.append(line)
                        line = word
                if line: lines.append(line)
                overflow = len(lines) > preset['max_lines'] or len(lines) * preset['line_height_px'] + 2 * outline > box['height']
                for line in lines:
                    bounds = font.getbbox(line, stroke_width=outline)
                    overflow |= font.getlength(line) > width or bounds[2] - bounds[0] > box['width'] or bounds[3]-bounds[1] > preset['line_height_px']
            if overflow:
                errors.append(Issue('TEXT_OVERFLOW', ptr, 'Measured glyphs exceed the pinned text box.'))
            layout = {'id': eid, 'style': preset.get('id', style_id), 'lines': lines,
                            'box_px': box, 'placement': placement, 'speaker_role': speaker_role,
                            'text_color': text_color}
            if line_styles:
                layout['line_styles'] = line_styles
            if is_caption:
                layout['caption_placement'] = requested
            layouts.append(layout)
        if file_hash(path) != before:
            errors.append(Issue('FONT_CHANGED', '/capabilities/font', 'Font changed during measurement.'))
        for weight, weight_path in weight_fonts.items():
            if file_hash(weight_path) != caption_preset['weight_fonts'][weight]['sha256']:
                errors.append(Issue('FONT_CHANGED', f'/capabilities/caption_preset/weight_fonts/{weight}',
                                    'Font changed during measurement.'))
    except (OSError, ValueError) as exc:
        errors.append(Issue('FONT_INVALID', '/capabilities/font', str(exc)))
    return errors, layouts
