"""Animated opening hook template."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.animation import render_frames_to_mp4


TEMPLATE_ID = "animated_opening_hook"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "mp4"
WIDTH = 1920
HEIGHT = 1080


def metadata() -> dict[str, object]:
    return {
        "name": "Animated Opening Hook",
        "description": "An immediate full-frame animated hook for the first seconds of a video.",
        "required_params": ["title"],
        "optional_params": ["subtitle", "tone"],
        "capabilities": ["animated_opening_hook"],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string")
    subtitle = params.get("subtitle")
    if subtitle is not None and not isinstance(subtitle, str):
        errors.append("subtitle must be a string when provided")
    tone = params.get("tone")
    if tone is not None and not isinstance(tone, str):
        errors.append("tone must be a string when provided")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return []


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))
    title = str(params["title"]).strip()
    subtitle = _optional_text(params, "subtitle")
    tone = _optional_text(params, "tone") or "private field notes"
    duration = _duration(params)

    def frame(index: int, total: int) -> Image.Image:
        t = index / max(1, total - 1)
        image = Image.new("RGB", (WIDTH, HEIGHT), "#101820")
        draw = ImageDraw.Draw(image)
        _draw_background(draw, t)
        _draw_hook(draw, title, subtitle, tone, t)
        return image

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    render_frames_to_mp4(output_path, duration_seconds=duration, frame_renderer=frame, fps=24)


def _draw_background(draw: ImageDraw.ImageDraw, t: float) -> None:
    for y in range(0, HEIGHT, 6):
        ratio = y / HEIGHT
        r = int(16 + 14 * ratio + 10 * t)
        g = int(24 + 18 * ratio)
        b = int(32 + 26 * ratio)
        draw.rectangle((0, y, WIDTH, y + 6), fill=(r, g, b))
    sweep_x = int(-340 + (WIDTH + 680) * min(1.0, t * 1.2))
    draw.polygon(
        [(sweep_x - 320, 0), (sweep_x + 80, 0), (sweep_x + 440, HEIGHT), (sweep_x + 40, HEIGHT)],
        fill="#233242",
    )
    for offset in range(0, WIDTH, 180):
        x = int((offset + t * 90) % WIDTH)
        draw.line((x, 0, x - 220, HEIGHT), fill="#1d2a36", width=2)


def _draw_hook(draw: ImageDraw.ImageDraw, title: str, subtitle: str, tone: str, t: float) -> None:
    title_font = _font(88, bold=True)
    subtitle_font = _font(44, bold=False)
    eyebrow_font = _font(30, bold=True)
    reveal = min(1.0, t * 1.6)
    x_shift = int((1.0 - reveal) * 90)
    alpha_color = _fade_color("#f8fafc", reveal)
    muted = _fade_color("#b8c2cc", reveal)
    accent = "#f0a15f"
    draw.rounded_rectangle((170, 228, 175 + int(14 + 500 * reveal), 244), radius=7, fill=accent)
    draw.text((180 + x_shift, 278), tone.upper(), font=eyebrow_font, fill=muted)
    y = 350
    for line in _wrap(draw, title, title_font, 1120)[:3]:
        draw.text((180 + x_shift, y), line, font=title_font, fill=alpha_color)
        y += _line_height(draw, title_font) + 18
    if subtitle:
        y += 28
        for line in _wrap(draw, subtitle, subtitle_font, 1100)[:2]:
            draw.text((184, y), line, font=subtitle_font, fill=muted)
            y += _line_height(draw, subtitle_font) + 10
    pulse = int(14 + 12 * abs(0.5 - (t % 1.0)))
    draw.ellipse((1510 - pulse, 770 - pulse, 1510 + pulse, 770 + pulse), fill=accent)
    draw.text((1545, 746), "start here", font=subtitle_font, fill="#f8fafc")


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


def _fade_color(hex_color: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = int(16 + (r - 16) * amount)
    g = int(24 + (g - 24) * amount)
    b = int(32 + (b - 32) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"
