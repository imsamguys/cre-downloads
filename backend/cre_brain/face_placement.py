"""Deterministic face sampling and caption-side selection for CRE v2."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

import cv2
import numpy as np
from PIL import ImageFont


INSET_WIDTH = 760
INSET_HEIGHT = 428
FACE_PADDING_PX = 12
SAMPLE_FRACTIONS = (0.2, 0.5, 0.8)


@dataclass(frozen=True)
class PlacementResult:
    placement: dict
    face_bbox: dict | None
    caption_bbox: dict
    sampled_faces: tuple
    fallback: str | None
    near_width_limit: bool


def _fit_inset(frame):
    height, width = frame.shape[:2]
    scale = min(INSET_WIDTH / width, INSET_HEIGHT / height)
    resized = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.zeros((INSET_HEIGHT, INSET_WIDTH, 3), dtype=np.uint8)
    x = (INSET_WIDTH - resized.shape[1]) // 2
    y = (INSET_HEIGHT - resized.shape[0]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return canvas


def detect_segment_face(video_path, start_seconds, end_seconds, model_path, score_threshold=0.72):
    """Sample three frames and return the median YuNet face bbox in inset pixels."""
    detector = cv2.FaceDetectorYN.create(str(model_path), "", (INSET_WIDTH, INSET_HEIGHT),
                                         score_threshold, 0.3, 5000)
    capture = cv2.VideoCapture(str(video_path))
    samples = []
    try:
        for fraction in SAMPLE_FRACTIONS:
            timestamp = start_seconds + (end_seconds - start_seconds) * fraction
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            inset = _fit_inset(frame)
            detector.setInputSize((INSET_WIDTH, INSET_HEIGHT))
            _, faces = detector.detect(inset)
            if faces is None or not len(faces):
                continue
            face = max(faces, key=lambda row: row[14])
            x, y, width, height = (int(round(value)) for value in face[:4])
            samples.append({"x": max(0, x), "y": max(0, y),
                            "w": min(INSET_WIDTH - max(0, x), width),
                            "h": min(INSET_HEIGHT - max(0, y), height),
                            "score_milli": int(round(float(face[14]) * 1000)),
                            "timestamp_milli": int(round(timestamp * 1000))})
    finally:
        capture.release()
    if not samples:
        return None, tuple()
    bbox = {key: int(round(median(sample[key] for sample in samples))) for key in ("x", "y", "w", "h")}
    return bbox, tuple(samples)


def detect_frame_face(video_path, timestamp_seconds, model_path, score_threshold=0.72):
    """Detect the largest face in one frame, in the renderer's inset coordinates."""
    detector = cv2.FaceDetectorYN.create(str(model_path), "", (INSET_WIDTH, INSET_HEIGHT),
                                         score_threshold, 0.3, 5000)
    capture = cv2.VideoCapture(str(video_path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp_seconds) * 1000)
        ok, frame = capture.read()
        if not ok:
            return None
        inset = _fit_inset(frame)
        detector.setInputSize((INSET_WIDTH, INSET_HEIGHT))
        _, faces = detector.detect(inset)
        if faces is None or not len(faces):
            return None
        face = max(faces, key=lambda row: row[14])
        x, y, width, height = (int(round(value)) for value in face[:4])
        x = max(0, x); y = max(0, y)
        return {"x": x, "y": y, "w": min(INSET_WIDTH - x, width),
                "h": min(INSET_HEIGHT - y, height),
                "score_milli": int(round(float(face[14]) * 1000)),
                "timestamp_milli": int(round(timestamp_seconds * 1000))}
    finally:
        capture.release()


def _width(text, font_path, size):
    return float(ImageFont.truetype(str(font_path), size, layout_engine=ImageFont.Layout.BASIC).getlength(text))


def choose_caption_placement(face_bbox, regular_text, heavy_words, regular_font_path,
                             heavy_font_path, regular_size, heavy_size, line_gap_ratio,
                             emphasis_flags=None):
    """Choose right/left/below and return measured caption/face geometry."""
    regular_width = _width(regular_text, regular_font_path, regular_size) if regular_text else 0.0
    heavy_widths = [_width(word + " ", heavy_font_path, heavy_size) for word in heavy_words]
    heavy_width = sum(heavy_widths)
    required_width = int(np.ceil(max(regular_width, heavy_width)))
    regular_offset = int(regular_size + heavy_size * line_gap_ratio)
    if face_bbox is None:
        placement = {"side": "below", "anchor_x": INSET_WIDTH // 2,
                     "anchor_y": INSET_HEIGHT, "max_text_width": INSET_WIDTH}
        return PlacementResult(placement, None, {"x": 90, "y": 1350, "w": 900, "h": 240},
                               tuple(), "no_face->below", False)

    left_space = max(0, face_bbox["x"] - FACE_PADDING_PX)
    right_edge = face_bbox["x"] + face_bbox["w"]
    right_space = max(0, INSET_WIDTH - right_edge - FACE_PADDING_PX)
    face_center = face_bbox["x"] + face_bbox["w"] / 2
    preferred = "right" if face_center < INSET_WIDTH / 2 else "left"
    alternate = "left" if preferred == "right" else "right"
    spaces = {"left": left_space, "right": right_space}
    side = next((candidate for candidate in (preferred, alternate)
                 if spaces[candidate] >= required_width), "below")
    fallback = None if side == preferred else f"{preferred}->{side}"
    if side == "below":
        placement = {"side": "below", "anchor_x": INSET_WIDTH // 2,
                     "anchor_y": INSET_HEIGHT, "max_text_width": INSET_WIDTH}
        caption_bbox = {"x": 90, "y": 1350, "w": 900, "h": 240}
        return PlacementResult(placement, face_bbox, caption_bbox, tuple(), fallback,
                               required_width >= int(INSET_WIDTH * 0.9))

    max_width = spaces[side]
    anchor_x = right_edge + FACE_PADDING_PX if side == "right" else face_bbox["x"] - FACE_PADDING_PX
    heavy_y = min(INSET_HEIGHT - heavy_size, max(regular_offset, face_bbox["y"] + face_bbox["h"] // 2))
    regular_y = heavy_y - regular_offset
    block_width = max(regular_width, heavy_width)
    block_x = anchor_x if side == "right" else anchor_x - block_width
    caption_bbox = {"x": int(np.floor(block_x)), "y": regular_y,
                    "w": int(np.ceil(block_width)), "h": heavy_y + heavy_size - regular_y}
    placement = {"side": side, "anchor_x": int(anchor_x), "anchor_y": int(heavy_y),
                 "max_text_width": int(max_width)}
    near = required_width >= int(max_width * 0.9)
    return PlacementResult(placement, face_bbox, caption_bbox, tuple(), fallback, near)


def choose_plain_caption_placement(face_bbox, text, font_path, font_size, line_height,
                                   max_lines=2):
    """Place a plain caption beside the face when it fits in a compact wrapped block."""
    font = ImageFont.truetype(str(font_path), font_size, layout_engine=ImageFont.Layout.BASIC)
    if face_bbox is None:
        placement = {"side": "below", "anchor_x": INSET_WIDTH // 2,
                     "anchor_y": INSET_HEIGHT, "max_text_width": INSET_WIDTH}
        return PlacementResult(placement, None, {"x": 90, "y": 1350, "w": 900, "h": 240},
                               tuple(), "no_face->below", False)

    def wrapped(width):
        lines = []; line = ""
        for word in text.split():
            candidate = f"{line} {word}".strip()
            if font.getlength(candidate) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines

    left_space = max(0, face_bbox["x"] - FACE_PADDING_PX)
    right_edge = face_bbox["x"] + face_bbox["w"]
    right_space = max(0, INSET_WIDTH - right_edge - FACE_PADDING_PX)
    preferred = "right" if face_bbox["x"] + face_bbox["w"] / 2 < INSET_WIDTH / 2 else "left"
    alternate = "left" if preferred == "right" else "right"
    spaces = {"left": left_space, "right": right_space}
    side = None; lines = []
    for candidate in (preferred, alternate):
        candidate_lines = wrapped(spaces[candidate])
        if candidate_lines and len(candidate_lines) <= max_lines and all(
                font.getlength(line) <= spaces[candidate] for line in candidate_lines):
            side = candidate; lines = candidate_lines; break
    if side is None:
        placement = {"side": "below", "anchor_x": INSET_WIDTH // 2,
                     "anchor_y": INSET_HEIGHT, "max_text_width": INSET_WIDTH}
        return PlacementResult(placement, face_bbox, {"x": 90, "y": 1350, "w": 900, "h": 240},
                               tuple(), f"{preferred}->below", False)

    block_width = max(font.getlength(line) for line in lines)
    block_height = len(lines) * line_height
    anchor_x = right_edge + FACE_PADDING_PX if side == "right" else face_bbox["x"] - FACE_PADDING_PX
    anchor_y = max(12, min(INSET_HEIGHT - block_height,
                           int(round(face_bbox["y"] + face_bbox["h"] / 2 - block_height / 2))))
    block_x = anchor_x if side == "right" else anchor_x - block_width
    placement = {"side": side, "anchor_x": int(anchor_x), "anchor_y": int(anchor_y),
                 "max_text_width": int(spaces[side])}
    caption_bbox = {"x": int(np.floor(block_x)), "y": int(anchor_y),
                    "w": int(np.ceil(block_width)), "h": int(block_height)}
    fallback = None if side == preferred else f"{preferred}->{side}"
    return PlacementResult(placement, face_bbox, caption_bbox, tuple(), fallback,
                           block_width >= spaces[side] * 0.9)


def choose_below_cheek_placement(face_bbox, text, font_path, font_size, line_height,
                                 x_offset=0, y_offset=0):
    """Center a compact caption immediately below the detected cheek line."""
    font = ImageFont.truetype(str(font_path), font_size, layout_engine=ImageFont.Layout.BASIC)
    text_width = int(np.ceil(font.getlength(text)))
    if face_bbox is None:
        anchor_x = INSET_WIDTH // 2
        anchor_y = min(INSET_HEIGHT - line_height, 300)
        fallback = "no_face->safe_center"
    else:
        anchor_x = int(round(face_bbox["x"] + face_bbox["w"] / 2))
        anchor_y = int(round(face_bbox["y"] + face_bbox["h"] * 0.82 + FACE_PADDING_PX))
        fallback = None
    anchor_x += int(x_offset)
    required_half = min(INSET_WIDTH // 2, max(32, int(np.ceil(text_width / 2))))
    anchor_x = max(required_half, min(INSET_WIDTH - required_half, anchor_x))
    anchor_y = max(0, min(INSET_HEIGHT - line_height, anchor_y + int(y_offset)))
    max_width = max(32, 2 * min(anchor_x, INSET_WIDTH - anchor_x))
    block_width = min(text_width, max_width)
    caption_bbox = {"x": int(round(anchor_x - block_width / 2)), "y": anchor_y,
                    "w": block_width, "h": line_height}
    placement = {"side": "below", "anchor_x": anchor_x, "anchor_y": anchor_y,
                 "max_text_width": max_width}
    return PlacementResult(placement, face_bbox, caption_bbox, tuple(), fallback,
                           text_width >= max_width * 0.9)


def boxes_overlap_with_padding(caption_bbox, face_bbox, padding=FACE_PADDING_PX):
    if face_bbox is None or caption_bbox.get("y", 1000) >= INSET_HEIGHT:
        return False
    fx1 = face_bbox["x"] - padding; fy1 = face_bbox["y"] - padding
    fx2 = face_bbox["x"] + face_bbox["w"] + padding
    fy2 = face_bbox["y"] + face_bbox["h"] + padding
    cx1 = caption_bbox["x"]; cy1 = caption_bbox["y"]
    cx2 = cx1 + caption_bbox["w"]; cy2 = cy1 + caption_bbox["h"]
    return cx1 < fx2 and cx2 > fx1 and cy1 < fy2 and cy2 > fy1
