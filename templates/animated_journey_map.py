"""Animated creator journey map template."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.animation import render_frames_to_mp4
from app.assets import asset_path


TEMPLATE_ID = "animated_journey_map"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "mp4"
WIDTH = 1920
HEIGHT = 1080
ASSET_ID = "creator_journey_texture"


def metadata() -> dict[str, object]:
    return {
        "name": "Animated Journey Map",
        "description": "Animated editorial path map for personal story arcs.",
        "required_params": ["title", "stages"],
        "optional_params": ["center_text"],
        "capabilities": ["animated_journey_map"],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string")
    center_text = params.get("center_text")
    if center_text is not None and not isinstance(center_text, str):
        errors.append("center_text must be a string when provided")
    stages = params.get("stages")
    if not isinstance(stages, list):
        errors.append("stages must be a list")
        return errors
    if not 2 <= len(stages) <= 6:
        errors.append("stages must contain between 2 and 6 items")
    for index, item in enumerate(stages):
        if not isinstance(item, dict):
            errors.append(f"stages[{index}] must be an object")
            continue
        if not isinstance(item.get("label"), str) or not str(item.get("label")).strip():
            errors.append(f"stages[{index}].label must be a non-empty string")
        if not isinstance(item.get("detail"), str) or not str(item.get("detail")).strip():
            errors.append(f"stages[{index}].detail must be a non-empty string")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return [ASSET_ID]


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))
    title = str(params["title"]).strip()
    center_text = _optional_text(params, "center_text") or "honest documentation"
    stages = _stages(params)
    duration = _duration(params)
    with Image.open(asset_path(ASSET_ID)) as source:
        base = source.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    def frame(index: int, total: int) -> Image.Image:
        t = index / max(1, total - 1)
        image = base.copy()
        draw = ImageDraw.Draw(image)
        _draw_frame(draw, title, center_text, stages, t)
        return image

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    render_frames_to_mp4(output_path, duration_seconds=duration, frame_renderer=frame, fps=24)


def _draw_frame(draw: ImageDraw.ImageDraw, title: str, center_text: str, stages: list[dict[str, str]], t: float) -> None:
    title_font = _font(70, bold=True)
    label_font = _font(34, bold=True)
    detail_font = _font(24, bold=False)
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline="#eadfce", width=18)
    _center(draw, "CREATOR JOURNEY MAP", _font(28, bold=True), 72, "#58616f")
    _center(draw, title, title_font, 122, "#1f2933")
    _center_badge(draw, center_text, t)
    points = _points(len(stages))
    visible_segments = min(len(points), int(t * (len(points) + 1)) + 1)
    if len(points) > 1:
        for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
            segment_t = min(1.0, max(0.0, t * len(points) - index))
            if segment_t <= 0:
                continue
            x = int(start[0] + (end[0] - start[0]) * segment_t)
            y = int(start[1] + (end[1] - start[1]) * segment_t)
            draw.line((start[0], start[1], x, y), fill="#9b6b44", width=10)
    for index, (stage, point) in enumerate(zip(stages, points, strict=False)):
        if index >= visible_segments:
            continue
        _stage(draw, stage, point, index + 1, label_font, detail_font)


def _center_badge(draw: ImageDraw.ImageDraw, text: str, t: float) -> None:
    badge = (610, 420, 1310, 650)
    scale = 0.94 + 0.06 * min(1.0, t * 2)
    cx = (badge[0] + badge[2]) // 2
    cy = (badge[1] + badge[3]) // 2
    w = int((badge[2] - badge[0]) * scale)
    h = int((badge[3] - badge[1]) * scale)
    box = (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)
    draw.rounded_rectangle(box, radius=36, fill="#fff9ee", outline="#ccad8d", width=3)
    _center(draw, text, _font(52, bold=True), cy - 26, "#6d4f3f")


def _stage(
    draw: ImageDraw.ImageDraw,
    stage: dict[str, str],
    point: tuple[int, int],
    index: int,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    detail_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x, y = point
    width = 430
    height = 190
    left = x - 135 if x < WIDTH // 2 else x - width + 135
    top = y - 95
    box = (left, top, left + width, top + height)
    draw.rounded_rectangle((box[0] + 8, box[1] + 10, box[2] + 8, box[3] + 10), radius=24, fill="#e3d8c8")
    draw.rounded_rectangle(box, radius=24, fill="#fffdf7", outline="#d3b999", width=2)
    draw.ellipse((x - 30, y - 30, x + 30, y + 30), fill="#d58a5c", outline="#8c5e39", width=3)
    draw.text((x - 9, y - 20), str(index), font=_font(30, bold=True), fill="#fffdf7")
    text_x = box[0] + 34
    y_cursor = box[1] + 28
    for line in _wrap(draw, stage["label"], label_font, width - 68)[:2]:
        draw.text((text_x, y_cursor), line, font=label_font, fill="#1f2933")
        y_cursor += _line_height(draw, label_font) + 5
    y_cursor += 8
    for line in _wrap(draw, stage["detail"], detail_font, width - 68)[:3]:
        draw.text((text_x, y_cursor), line, font=detail_font, fill="#58616f")
        y_cursor += _line_height(draw, detail_font) + 4


def _points(count: int) -> list[tuple[int, int]]:
    anchors = [(310, 335), (525, 735), (1395, 735), (1610, 335), (960, 790), (960, 330)]
    return anchors[:count]


def _stages(params: dict[str, object]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    raw = params.get("stages")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                label = item.get("label")
                detail = item.get("detail")
                if isinstance(label, str) and isinstance(detail, str):
                    output.append({"label": label.strip(), "detail": detail.strip()})
    return output


def _duration(params: dict[str, object]) -> float:
    value = params.get("duration_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 8.0


def _optional_text(params: dict[str, object], name: str) -> str:
    value = params.get(name)
    return value.strip() if isinstance(value, str) else ""


def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = ("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, y: int, fill: str) -> None:
    width = _text_width(draw, text, font)
    draw.text(((WIDTH - width) // 2, y), text, font=font, fill=fill)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines or [text]


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return int(right - left)


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    return int(bottom - top)
