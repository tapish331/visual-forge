"""Animated kinetic text beat template."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.animation import render_frames_to_mp4


TEMPLATE_ID = "kinetic_text_beat"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "mp4"
WIDTH = 1920
HEIGHT = 1080


def metadata() -> dict[str, object]:
    return {
        "name": "Kinetic Text Beat",
        "description": "Animated emphasis text with moving word groups and accent bars.",
        "required_params": ["text"],
        "optional_params": ["label", "accent"],
        "capabilities": ["kinetic_text_beat", "animated_quote", "animated_key_point"],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append("text must be a non-empty string")
    label = params.get("label")
    if label is not None and not isinstance(label, str):
        errors.append("label must be a string when provided")
    accent = params.get("accent")
    if accent is not None and not isinstance(accent, str):
        errors.append("accent must be a string when provided")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return []


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))
    text = str(params["text"]).strip()
    label = _optional_text(params, "label") or "beat"
    accent = _optional_text(params, "accent") or "#5c7cfa"
    duration = _duration(params)

    def frame(index: int, total: int) -> Image.Image:
        t = index / max(1, total - 1)
        image = Image.new("RGB", (WIDTH, HEIGHT), "#f7f3ea")
        draw = ImageDraw.Draw(image)
        _draw_texture(draw, t)
        _draw_text(draw, text, label, accent, t)
        return image

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    render_frames_to_mp4(output_path, duration_seconds=duration, frame_renderer=frame, fps=24)


def _draw_texture(draw: ImageDraw.ImageDraw, t: float) -> None:
    for x in range(-120, WIDTH + 120, 140):
        offset = int(40 * t)
        draw.line((x + offset, 0, x - 180 + offset, HEIGHT), fill="#eee5d7", width=5)
    draw.rectangle((0, 0, WIDTH, 22), fill="#111827")
    draw.rectangle((0, HEIGHT - 22, WIDTH, HEIGHT), fill="#111827")


def _draw_text(draw: ImageDraw.ImageDraw, text: str, label: str, accent: str, t: float) -> None:
    label_font = _font(32, bold=True)
    word_font = _font(88, bold=True)
    helper_font = _font(34, bold=False)
    reveal = min(1.0, t * 1.8)
    draw.rounded_rectangle((150, 162, 150 + int(440 * reveal), 178), radius=8, fill=accent)
    draw.text((150, 112), label.upper(), font=label_font, fill="#374151")
    lines = _wrap(draw, text, word_font, 1420)[:4]
    y = 310
    for index, line in enumerate(lines):
        local = min(1.0, max(0.0, t * 2.2 - index * 0.22))
        x = int(150 + (1.0 - local) * 180)
        draw.text((x, y), line, font=word_font, fill="#111827")
        y += _line_height(draw, word_font) + 22
    progress = int(150 + (WIDTH - 300) * t)
    draw.line((150, 880, progress, 880), fill=accent, width=12)
    draw.text((150, 910), "moving thought, not a static card", font=helper_font, fill="#6b7280")


def _duration(params: dict[str, object]) -> float:
    value = params.get("duration_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 5.0


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
