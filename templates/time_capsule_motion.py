"""Animated time capsule and question-stack template."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.animation import render_frames_to_mp4
from app.assets import asset_path


TEMPLATE_ID = "time_capsule_motion"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "mp4"
WIDTH = 1920
HEIGHT = 1080
ASSET_ID = "personal_documentary_texture"


def metadata() -> dict[str, object]:
    return {
        "name": "Time Capsule Motion",
        "description": "Animated timeline, promise, question, and future-archive cards for creator journeys.",
        "required_params": ["title", "items"],
        "optional_params": ["footer", "mode"],
        "capabilities": [
            "animated_future_archive",
            "animated_promise_reveal",
            "animated_question_stack",
            "animated_recap_archive",
            "animated_time_gap",
        ],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string")
    items = params.get("items")
    if not isinstance(items, list):
        errors.append("items must be a list")
        return errors
    if not 1 <= len(items) <= 6:
        errors.append("items must contain between 1 and 6 items")
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"items[{index}] must be a non-empty string")
    footer = params.get("footer")
    if footer is not None and not isinstance(footer, str):
        errors.append("footer must be a string when provided")
    mode = params.get("mode")
    if mode is not None and not isinstance(mode, str):
        errors.append("mode must be a string when provided")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return [ASSET_ID]


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))

    title = str(params["title"]).strip()
    items = _items(params)
    footer = _optional_text(params, "footer")
    mode = _optional_text(params, "mode") or "archive"
    duration = _duration(params)
    with Image.open(asset_path(ASSET_ID)) as source:
        base = source.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    def frame(index: int, total: int) -> Image.Image:
        t = index / max(1, total - 1)
        image = base.copy()
        draw = ImageDraw.Draw(image)
        _draw_frame(draw, title, items, footer, mode, t)
        return image

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    render_frames_to_mp4(output_path, duration_seconds=duration, frame_renderer=frame, fps=18)


def _draw_frame(draw: ImageDraw.ImageDraw, title: str, items: list[str], footer: str, mode: str, t: float) -> None:
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline="#eadfce", width=18)
    _draw_header(draw, title, mode, t)
    if "question" in mode:
        _draw_question_stack(draw, items, t)
    else:
        _draw_timeline(draw, items, t)
    if footer:
        _draw_footer(draw, footer, t)


def _draw_header(draw: ImageDraw.ImageDraw, title: str, mode: str, t: float) -> None:
    draw.text((150, 58), mode.upper(), font=_font(30, bold=True), fill="#475569")
    draw.rounded_rectangle((150, 108, 150 + int(500 * min(1.0, t * 1.8)), 124), radius=8, fill="#2f7d68")
    y = 154
    for line in _wrap(draw, title, _font(76, bold=True), 1320)[:2]:
        draw.text((150, y), line, font=_font(76, bold=True), fill="#18212f")
        y += _line_height(draw, _font(76, bold=True)) + 8


def _draw_timeline(draw: ImageDraw.ImageDraw, items: list[str], t: float) -> None:
    axis_y = 575
    start_x = 250
    end_x = 1670
    draw.line((start_x, axis_y, end_x, axis_y), fill="#8f5f3b", width=8)
    progress = int(start_x + (end_x - start_x) * min(1.0, t * 1.05))
    draw.line((start_x, axis_y, progress, axis_y), fill="#2f7d68", width=12)
    count = len(items)
    for index, item in enumerate(items):
        x = int(start_x + (end_x - start_x) * (index / max(1, count - 1)))
        local = min(1.0, max(0.0, t * (count + 0.7) - index * 0.52))
        if local <= 0:
            continue
        draw.ellipse((x - 30, axis_y - 30, x + 30, axis_y + 30), fill="#2f7d68", outline="#fff8ec", width=4)
        top = 675 if index % 2 else 360
        _archive_card(draw, item, (x - 185, top, x + 185, top + 152), local)


def _draw_question_stack(draw: ImageDraw.ImageDraw, items: list[str], t: float) -> None:
    base_x = 530
    base_y = 350
    for index, item in enumerate(items):
        local = min(1.0, max(0.0, t * (len(items) + 0.8) - index * 0.48))
        if local <= 0:
            continue
        x_offset = int(index * 72 + (1.0 - local) * 120)
        y_offset = index * 88
        box = (base_x + x_offset, base_y + y_offset, base_x + x_offset + 820, base_y + y_offset + 128)
        _archive_card(draw, item, box, local)
        draw.text((box[0] - 58, box[1] + 38), "?", font=_font(48, bold=True), fill="#2f7d68")


def _archive_card(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], local: float) -> None:
    slide = int((1.0 - local) * 40)
    box = (box[0] + slide, box[1], box[2] + slide, box[3])
    draw.rounded_rectangle((box[0] + 9, box[1] + 11, box[2] + 9, box[3] + 11), radius=22, fill="#ded1c2")
    draw.rounded_rectangle(box, radius=22, fill="#fffaf1", outline="#d7b797", width=2)
    y = box[1] + 34
    font = _font(34, bold=True)
    for line in _wrap(draw, text, font, box[2] - box[0] - 58)[:2]:
        draw.text((box[0] + 30, y), line, font=font, fill="#18212f")
        y += _line_height(draw, font) + 5


def _draw_footer(draw: ImageDraw.ImageDraw, footer: str, t: float) -> None:
    box = (430, 915, 1490, 1002)
    draw.rounded_rectangle(box, radius=20, fill="#effaf6", outline="#92c5ad", width=2)
    text = footer[:126]
    visible = int(len(text) * min(1.0, t * 1.35))
    draw.text((box[0] + 34, box[1] + 25), text[:visible], font=_font(32, bold=False), fill="#475569")


def _items(params: dict[str, object]) -> list[str]:
    raw = params.get("items")
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _duration(params: dict[str, object]) -> float:
    value = params.get("duration_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 6.0


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
