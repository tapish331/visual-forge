"""Animated personal field-notes template."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.animation import render_frames_to_mp4
from app.assets import asset_path


TEMPLATE_ID = "field_notes_motion"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "mp4"
WIDTH = 1920
HEIGHT = 1080
ASSET_ID = "personal_documentary_texture"


def metadata() -> dict[str, object]:
    return {
        "name": "Field Notes Motion",
        "description": "Animated handwritten-note motion for candid creator self-documentation.",
        "required_params": ["title", "notes"],
        "optional_params": ["motif"],
        "capabilities": [
            "animated_boundary_scale",
            "animated_confession_spiral",
            "animated_documenting_loop",
            "animated_self_check",
        ],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string")

    notes = params.get("notes")
    if not isinstance(notes, list):
        errors.append("notes must be a list")
        return errors
    if not 1 <= len(notes) <= 5:
        errors.append("notes must contain between 1 and 5 items")
    for index, item in enumerate(notes):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"notes[{index}] must be a non-empty string")

    motif = params.get("motif")
    if motif is not None and not isinstance(motif, str):
        errors.append("motif must be a string when provided")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return [ASSET_ID]


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))

    title = str(params["title"]).strip()
    notes = _notes(params)
    motif = _optional_text(params, "motif") or "spiral"
    duration = _duration(params)
    with Image.open(asset_path(ASSET_ID)) as source:
        base = source.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    def frame(index: int, total: int) -> Image.Image:
        t = index / max(1, total - 1)
        image = base.copy()
        draw = ImageDraw.Draw(image)
        _draw_frame(draw, title, notes, motif, t)
        return image

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    render_frames_to_mp4(output_path, duration_seconds=duration, frame_renderer=frame, fps=18)


def _draw_frame(draw: ImageDraw.ImageDraw, title: str, notes: list[str], motif: str, t: float) -> None:
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline="#eadfce", width=18)
    _draw_header(draw, title, t)
    if motif == "scale":
        _draw_scale(draw, notes, t)
    elif motif == "loop":
        _draw_loop(draw, notes, t)
    elif motif == "reset":
        _draw_reset(draw, notes, t)
    else:
        _draw_spiral(draw, notes, t)
    _draw_footer(draw, motif, t)


def _draw_header(draw: ImageDraw.ImageDraw, title: str, t: float) -> None:
    eyebrow = _font(30, bold=True)
    title_font = _font(76, bold=True)
    reveal = min(1.0, t * 1.8)
    draw.rounded_rectangle((150, 108, 150 + int(520 * reveal), 124), radius=8, fill="#bf6a45")
    draw.text((150, 58), "PRIVATE FIELD NOTES", font=eyebrow, fill="#475569")
    y = 154
    for line in _wrap(draw, title, title_font, 1320)[:2]:
        draw.text((150, y), line, font=title_font, fill="#18212f")
        y += _line_height(draw, title_font) + 8


def _draw_spiral(draw: ImageDraw.ImageDraw, notes: list[str], t: float) -> None:
    center = (960, 610)
    max_points = 160
    visible = max(2, int(max_points * min(1.0, t * 1.15)))
    points: list[tuple[int, int]] = []
    for index in range(visible):
        ratio = index / max_points
        angle = ratio * math.tau * 2.2
        radius = 42 + ratio * 360
        x = int(center[0] + math.cos(angle) * radius)
        y = int(center[1] + math.sin(angle) * radius * 0.62)
        points.append((x, y))
    if len(points) > 1:
        draw.line(points, fill="#8f5f3b", width=9, joint="curve")
    _draw_note_cards(draw, notes, _spiral_card_positions(len(notes)), t)


def _draw_scale(draw: ImageDraw.ImageDraw, notes: list[str], t: float) -> None:
    axis_y = 640
    draw.line((310, axis_y, 1610, axis_y), fill="#8f5f3b", width=10)
    for x, label in ((310, "private"), (1610, "post it")):
        draw.ellipse((x - 24, axis_y - 24, x + 24, axis_y + 24), fill="#bf6a45")
        _center(draw, label, _font(28, bold=True), x, axis_y + 48, "#475569")
    marker = int(310 + (1610 - 310) * (0.28 + 0.36 * min(1.0, t)))
    draw.ellipse((marker - 48, axis_y - 48, marker + 48, axis_y + 48), fill="#1f2937", outline="#fff8ec", width=5)
    _draw_note_cards(draw, notes, [(420, 390), (820, 745), (1180, 390), (1430, 745), (960, 500)], t)


def _draw_loop(draw: ImageDraw.ImageDraw, notes: list[str], t: float) -> None:
    cx, cy = 960, 620
    radius = 300
    visible = int(260 * min(1.0, t * 1.3))
    points: list[tuple[int, int]] = []
    for index in range(visible):
        angle = index / 260 * math.tau
        points.append((int(cx + math.cos(angle) * radius), int(cy + math.sin(angle) * 180)))
    if len(points) > 1:
        draw.line(points, fill="#8f5f3b", width=9, joint="curve")
    dot_angle = math.tau * min(1.0, t)
    dot = (int(cx + math.cos(dot_angle) * radius), int(cy + math.sin(dot_angle) * 180))
    draw.ellipse((dot[0] - 30, dot[1] - 30, dot[0] + 30, dot[1] + 30), fill="#bf6a45")
    _draw_note_cards(draw, notes, [(430, 430), (960, 780), (1380, 430), (690, 315), (1230, 750)], t)


def _draw_reset(draw: ImageDraw.ImageDraw, notes: list[str], t: float) -> None:
    positions = [(430, 420), (780, 660), (1150, 430), (1430, 700), (960, 330)]
    adjusted: list[tuple[int, int]] = []
    for index, (x, y) in enumerate(positions[: len(notes)]):
        wobble = int(math.sin((t * 8 + index) * math.pi) * 10 * (1.0 - min(1.0, t)))
        adjusted.append((x + wobble, y))
    draw.line((360, 810, 1560, 810), fill="#d7b797", width=5)
    draw.text((715, 815), "pause, reset, keep the take honest", font=_font(32, bold=False), fill="#475569")
    _draw_note_cards(draw, notes, adjusted, t)


def _draw_note_cards(draw: ImageDraw.ImageDraw, notes: list[str], positions: list[tuple[int, int]], t: float) -> None:
    label_font = _font(38, bold=True)
    for index, note in enumerate(notes):
        local = min(1.0, max(0.0, t * (len(notes) + 0.6) - index * 0.55))
        if local <= 0:
            continue
        x, y = positions[index % len(positions)]
        slide = int((1.0 - local) * 90)
        box = (x - 205 + slide, y - 78, x + 205 + slide, y + 78)
        draw.rounded_rectangle((box[0] + 10, box[1] + 12, box[2] + 10, box[3] + 12), radius=24, fill="#e5d6c7")
        draw.rounded_rectangle(box, radius=24, fill="#fffaf1", outline="#cfb697", width=2)
        draw.ellipse((box[0] + 24, box[1] + 24, box[0] + 46, box[1] + 46), fill="#bf6a45")
        y_cursor = box[1] + 45
        for line in _wrap(draw, note, label_font, 330)[:2]:
            draw.text((box[0] + 62, y_cursor), line, font=label_font, fill="#18212f")
            y_cursor += _line_height(draw, label_font) + 5


def _draw_footer(draw: ImageDraw.ImageDraw, motif: str, t: float) -> None:
    progress = int(150 + (WIDTH - 300) * t)
    draw.line((150, 970, progress, 970), fill="#bf6a45", width=10)
    draw.text((150, 922), f"motion motif: {motif}", font=_font(28, bold=False), fill="#64748b")


def _notes(params: dict[str, object]) -> list[str]:
    raw = params.get("notes")
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
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
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


def _center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, x: int, y: int, fill: str) -> None:
    draw.text((x - _text_width(draw, text, font) // 2, y), text, font=font, fill=fill)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return int(right - left)


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    return int(bottom - top)


def _spiral_card_positions(count: int) -> list[tuple[int, int]]:
    anchors = [(455, 408), (720, 750), (1140, 430), (1415, 710), (960, 328)]
    return anchors[: max(1, count)]
