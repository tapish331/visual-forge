"""Creator journey map visual template."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.assets import asset_path


TEMPLATE_ID = "creator_journey_map"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "png"
WIDTH = 1920
HEIGHT = 1080
ASSET_ID = "creator_journey_texture"

INK = "#1f2933"
MUTED_INK = "#58616f"
PAPER = "#fbf6ec"
CARD = "#fffdf7"
CARD_SHADOW = "#e3d8c8"
PATH = "#9b6b44"
ACCENT = "#d58a5c"
DEEP_ACCENT = "#6d4f3f"


def metadata() -> dict[str, object]:
    return {
        "name": "Creator Journey Map",
        "description": "A soft editorial path map for personal creator-documentation arcs.",
        "required_params": ["title", "stages"],
        "optional_params": ["center_text"],
        "capabilities": ["creator_journey_map"],
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
        label = item.get("label")
        detail = item.get("detail")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"stages[{index}].label must be a non-empty string")
        if not isinstance(detail, str) or not detail.strip():
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

    with Image.open(asset_path(ASSET_ID)) as source:
        image = source.convert("RGB")
    if image.size != (WIDTH, HEIGHT):
        image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(image)
    _draw_vignette(draw)
    _draw_header(draw, title)
    _draw_center_badge(draw, center_text)
    _draw_journey_path(draw, stages)
    _draw_footer_note(draw)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")


def _stages(params: dict[str, object]) -> list[dict[str, str]]:
    raw = params["stages"]
    if not isinstance(raw, list):
        return []
    output: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        detail = item.get("detail")
        if isinstance(label, str) and isinstance(detail, str):
            output.append({"label": label.strip(), "detail": detail.strip()})
    return output


def _draw_vignette(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline="#eadfce", width=18)
    draw.line((130, 205, WIDTH - 130, 205), fill="#d9c8b5", width=2)
    draw.line((130, 875, WIDTH - 130, 875), fill="#d9c8b5", width=2)
    for x in range(210, WIDTH - 160, 180):
        draw.ellipse((x, 920, x + 8, 928), fill="#d5c2ad")


def _draw_header(draw: ImageDraw.ImageDraw, title: str) -> None:
    eyebrow_font = _font(30, bold=False)
    title_font = _font(76, bold=True)
    _center_text(draw, "PRIVATE CREATOR FIELD NOTES", eyebrow_font, 94, MUTED_INK)
    lines = _wrap_text(draw, title, title_font, 1320)
    y = 132
    for line in lines[:2]:
        _center_text(draw, line, title_font, y, INK)
        y += _line_height(draw, title_font) + 10


def _draw_center_badge(draw: ImageDraw.ImageDraw, text: str) -> None:
    badge = (610, 420, 1310, 650)
    draw.rounded_rectangle((badge[0] + 10, badge[1] + 14, badge[2] + 10, badge[3] + 14), radius=36, fill="#e6d7c7")
    draw.rounded_rectangle(badge, radius=36, fill="#fff9ee", outline="#ccad8d", width=3)
    quote_font = _font(58, bold=True)
    helper_font = _font(28, bold=False)
    _center_text(draw, "the quiet middle", helper_font, 465, MUTED_INK)
    lines = _wrap_text(draw, text, quote_font, 560)
    y = 512 - ((len(lines) * _line_height(draw, quote_font)) // 2)
    for line in lines[:2]:
        _center_text(draw, line, quote_font, y, DEEP_ACCENT)
        y += _line_height(draw, quote_font) + 6


def _draw_journey_path(draw: ImageDraw.ImageDraw, stages: list[dict[str, str]]) -> None:
    points = _stage_points(len(stages))
    if len(points) > 1:
        _draw_polyline(draw, points, width=10, fill=PATH)
        _draw_polyline(draw, [(x, y - 8) for x, y in points], width=3, fill="#e7b080")

    for index, (stage, point) in enumerate(zip(stages, points, strict=False), start=1):
        _draw_stage_card(draw, stage, point, index)


def _stage_points(count: int) -> list[tuple[int, int]]:
    anchors = [
        (310, 335),
        (525, 735),
        (1395, 735),
        (1610, 335),
        (960, 790),
        (960, 330),
    ]
    if count <= 4:
        return anchors[:count]
    return anchors[:count]


def _draw_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[int, int]],
    *,
    width: int,
    fill: str,
) -> None:
    for start, end in zip(points, points[1:], strict=False):
        draw.line((start[0], start[1], end[0], end[1]), fill=fill, width=width, joint="curve")


def _draw_stage_card(
    draw: ImageDraw.ImageDraw,
    stage: dict[str, str],
    point: tuple[int, int],
    index: int,
) -> None:
    x, y = point
    width = 430
    height = 190
    if x < WIDTH // 2:
        left = x - 135
    else:
        left = x - width + 135
    top = y - 95
    box = (left, top, left + width, top + height)

    draw.rounded_rectangle((box[0] + 8, box[1] + 10, box[2] + 8, box[3] + 10), radius=24, fill=CARD_SHADOW)
    draw.rounded_rectangle(box, radius=24, fill=CARD, outline="#d3b999", width=2)
    draw.ellipse((x - 30, y - 30, x + 30, y + 30), fill=ACCENT, outline="#8c5e39", width=3)

    number_font = _font(30, bold=True)
    label_font = _font(36, bold=True)
    detail_font = _font(25, bold=False)
    number = str(index)
    number_w = _text_width(draw, number, number_font)
    draw.text((x - number_w // 2, y - 20), number, font=number_font, fill="#fffdf7")

    text_x = box[0] + 34
    y_cursor = box[1] + 28
    for line in _wrap_text(draw, stage["label"], label_font, width - 68)[:2]:
        draw.text((text_x, y_cursor), line, font=label_font, fill=INK)
        y_cursor += _line_height(draw, label_font) + 5
    y_cursor += 8
    for line in _wrap_text(draw, stage["detail"], detail_font, width - 68)[:3]:
        draw.text((text_x, y_cursor), line, font=detail_font, fill=MUTED_INK)
        y_cursor += _line_height(draw, detail_font) + 4


def _draw_footer_note(draw: ImageDraw.ImageDraw) -> None:
    font = _font(26, bold=False)
    _center_text(draw, "recording, pausing, trying again", font, 945, MUTED_INK)


def _optional_text(params: dict[str, object], name: str) -> str:
    value = params.get(name)
    return value.strip() if isinstance(value, str) else ""


def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("arialbd.ttf", "DejaVuSans-Bold.ttf")
        if bold
        else ("arial.ttf", "DejaVuSans.ttf")
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [text]
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
    return lines


def _center_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    y: int,
    fill: str,
) -> None:
    width = _text_width(draw, text, font)
    draw.text(((WIDTH - width) // 2, y), text, font=font, fill=fill)


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return int(round(right - left))


def _line_height(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    return int(round(bottom - top))
