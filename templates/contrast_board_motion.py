"""Animated split-board contrast template."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.animation import render_frames_to_mp4
from app.assets import asset_path


TEMPLATE_ID = "contrast_board_motion"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "mp4"
WIDTH = 1920
HEIGHT = 1080
ASSET_ID = "personal_documentary_texture"


def metadata() -> dict[str, object]:
    return {
        "name": "Contrast Board Motion",
        "description": "Animated two-sided editorial board for rejecting generic advice and locating the honest alternative.",
        "required_params": ["title", "left_label", "right_label"],
        "optional_params": ["caption", "balance"],
        "capabilities": [
            "animated_anti_sales_meter",
            "animated_contrast_split",
            "animated_no_fake_motivation",
            "animated_private_public_scale",
        ],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for name in ("title", "left_label", "right_label"):
        value = params.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name} must be a non-empty string")
    caption = params.get("caption")
    if caption is not None and not isinstance(caption, str):
        errors.append("caption must be a string when provided")
    balance = params.get("balance")
    if balance is not None and (isinstance(balance, bool) or not isinstance(balance, int | float)):
        errors.append("balance must be a number when provided")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return [ASSET_ID]


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))

    title = str(params["title"]).strip()
    left_label = str(params["left_label"]).strip()
    right_label = str(params["right_label"]).strip()
    caption = _optional_text(params, "caption")
    balance = _balance(params)
    duration = _duration(params)
    with Image.open(asset_path(ASSET_ID)) as source:
        base = source.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    def frame(index: int, total: int) -> Image.Image:
        t = index / max(1, total - 1)
        image = base.copy()
        draw = ImageDraw.Draw(image)
        _draw_frame(draw, title, left_label, right_label, caption, balance, t)
        return image

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    render_frames_to_mp4(output_path, duration_seconds=duration, frame_renderer=frame, fps=18)


def _draw_frame(
    draw: ImageDraw.ImageDraw,
    title: str,
    left_label: str,
    right_label: str,
    caption: str,
    balance: float,
    t: float,
) -> None:
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline="#eadfce", width=18)
    _draw_header(draw, title, t)
    reveal = min(1.0, t * 1.45)
    left = (150 - int((1.0 - reveal) * 160), 300, 910 - int((1.0 - reveal) * 160), 760)
    right = (1010 + int((1.0 - reveal) * 160), 300, 1770 + int((1.0 - reveal) * 160), 760)
    _panel(draw, left, "noise", left_label, "#fff7ed", "#b45309")
    _panel(draw, right, "honest record", right_label, "#eff6ff", "#2457a6")
    _draw_tension(draw, balance, t)
    if caption:
        _caption(draw, caption, t)


def _draw_header(draw: ImageDraw.ImageDraw, title: str, t: float) -> None:
    eyebrow = _font(30, bold=True)
    title_font = _font(72, bold=True)
    draw.text((150, 58), "CONTRAST BOARD", font=eyebrow, fill="#475569")
    draw.rounded_rectangle((150, 108, 150 + int(500 * min(1.0, t * 1.8)), 124), radius=8, fill="#2457a6")
    y = 154
    for line in _wrap(draw, title, title_font, 1320)[:2]:
        draw.text((150, y), line, font=title_font, fill="#18212f")
        y += _line_height(draw, title_font) + 8


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], eyebrow: str, label: str, fill: str, accent: str) -> None:
    draw.rounded_rectangle((box[0] + 14, box[1] + 16, box[2] + 14, box[3] + 16), radius=34, fill="#ded1c2")
    draw.rounded_rectangle(box, radius=34, fill=fill, outline=accent, width=4)
    draw.text((box[0] + 52, box[1] + 48), eyebrow.upper(), font=_font(28, bold=True), fill="#475569")
    y = box[1] + 134
    for line in _wrap(draw, label, _font(62, bold=True), box[2] - box[0] - 104)[:3]:
        draw.text((box[0] + 52, y), line, font=_font(62, bold=True), fill="#18212f")
        y += _line_height(draw, _font(62, bold=True)) + 14
    draw.line((box[0] + 52, box[3] - 72, box[2] - 52, box[3] - 72), fill=accent, width=8)


def _draw_tension(draw: ImageDraw.ImageDraw, balance: float, t: float) -> None:
    axis = (420, 850, 1500, 850)
    draw.line(axis, fill="#8f5f3b", width=8)
    progress = int(axis[0] + (axis[2] - axis[0]) * min(1.0, t))
    draw.line((axis[0], axis[1], progress, axis[3]), fill="#2457a6", width=12)
    marker_x = int(axis[0] + (axis[2] - axis[0]) * max(0.0, min(1.0, balance)))
    marker_x = int(axis[0] + (marker_x - axis[0]) * min(1.0, t * 1.4))
    draw.ellipse((marker_x - 42, axis[1] - 42, marker_x + 42, axis[1] + 42), fill="#18212f", outline="#fff8ec", width=5)
    _center(draw, "pressure", _font(26, bold=True), axis[0], axis[1] + 54, "#64748b")
    _center(draw, "own pace", _font(26, bold=True), axis[2], axis[1] + 54, "#64748b")


def _caption(draw: ImageDraw.ImageDraw, caption: str, t: float) -> None:
    box = (445, 910, 1475, 1002)
    draw.rounded_rectangle(box, radius=22, fill="#fffaf1", outline="#d7b797", width=2)
    text = caption[:120]
    x = box[0] + 38
    visible = int(len(text) * min(1.0, t * 1.25))
    draw.text((x, box[1] + 27), text[:visible], font=_font(34, bold=False), fill="#475569")


def _balance(params: dict[str, object]) -> float:
    value = params.get("balance")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.68


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


def _center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, x: int, y: int, fill: str) -> None:
    draw.text((x - _text_width(draw, text, font) // 2, y), text, font=font, fill=fill)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return int(right - left)


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    return int(bottom - top)
