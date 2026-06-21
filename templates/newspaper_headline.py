"""Period newspaper front-page headline visual."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.assets import asset_path


TEMPLATE_ID = "newspaper_headline"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "ready"
OUTPUT_TYPE = "png"
WIDTH = 1920
HEIGHT = 1080
ASSET_ID = "newspaper_base"
INK = "#191713"
MUTED_INK = "#4a453c"


def metadata() -> dict[str, object]:
    return {
        "name": "Newspaper Headline",
        "description": "A readable period-print front page for presenting a claim as contemporary reporting.",
        "required_params": ["headline"],
        "optional_params": ["publication", "date", "subheadline"],
        "capabilities": ["newspaper_headline"],
        "size": [WIDTH, HEIGHT],
    }


def validate_params(params: dict[str, object]) -> list[str]:
    errors: list[str] = []
    headline = params.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        errors.append("headline must be a non-empty string")
    for name in ("publication", "date", "subheadline"):
        value = params.get(name)
        if value is not None and not isinstance(value, str):
            errors.append(f"{name} must be a string when provided")
    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return [ASSET_ID]


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))

    headline = str(params["headline"]).strip()
    publication = _optional_text(params, "publication") or "THE DAILY CHRONICLE"
    date = _optional_text(params, "date") or "SPECIAL EDITION"
    subheadline = _optional_text(params, "subheadline")

    with Image.open(asset_path(ASSET_ID)) as source:
        image = source.convert("RGB")
    if image.size != (WIDTH, HEIGHT):
        raise ValueError(f"{ASSET_ID} must be {WIDTH}x{HEIGHT}")

    draw = ImageDraw.Draw(image)
    publication_font = _font(60, bold=True, serif=True)
    date_font = _font(24, bold=False, serif=True)
    body_font = _font(28, bold=False, serif=True)
    subheadline_font = _font(38, bold=False, serif=True)

    _center_text(draw, publication.upper(), publication_font, 70, INK)
    _center_text(draw, date.upper(), date_font, 150, MUTED_INK)
    draw.line((115, 194, WIDTH - 115, 194), fill=INK, width=5)
    draw.line((115, 205, WIDTH - 115, 205), fill=INK, width=1)

    headline_font, headline_lines = _fit_headline(draw, headline, max_width=1580, max_height=430)
    line_height = _line_height(draw, headline_font)
    headline_gap = 16
    headline_height = len(headline_lines) * line_height + max(0, len(headline_lines) - 1) * headline_gap
    y = 245 + max(0, (430 - headline_height) // 2)
    for line in headline_lines:
        _center_text(draw, line, headline_font, y, INK)
        y += line_height + headline_gap

    draw.line((180, 705, WIDTH - 180, 705), fill=MUTED_INK, width=2)
    if subheadline:
        lines = _wrap_text(draw, subheadline, subheadline_font, 1450)
        y = 740
        for line in lines[:3]:
            _center_text(draw, line, subheadline_font, y, MUTED_INK)
            y += _line_height(draw, subheadline_font) + 12
    else:
        left_copy = "REPORTS FROM ACROSS THE CITY DESCRIBE A DEFINING MOMENT IN TODAY'S EVENTS."
        right_copy = "ANALYSIS, WITNESS ACCOUNTS, AND THE FULL STORY CONTINUE INSIDE THIS EDITION."
        _draw_column(draw, left_copy, body_font, 240, 755, 650)
        _draw_column(draw, right_copy, body_font, 1030, 755, 650)
        draw.line((960, 750, 960, 990), fill="#837b6d", width=1)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")


def _optional_text(params: dict[str, object], name: str) -> str:
    value = params.get(name)
    return value.strip() if isinstance(value, str) else ""


def _font(size: int, *, bold: bool, serif: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if serif:
        candidates = (
            ("georgiab.ttf", "DejaVuSerif-Bold.ttf")
            if bold
            else ("georgia.ttf", "DejaVuSerif.ttf")
        )
    else:
        candidates = (("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf"))
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_headline(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
    for size in range(112, 47, -4):
        font = _font(size, bold=True, serif=True)
        lines = _wrap_text(draw, text.upper(), font, max_width)
        height = len(lines) * _line_height(draw, font) + max(0, len(lines) - 1) * 16
        if len(lines) <= 4 and height <= max_height:
            return font, lines
    font = _font(48, bold=True, serif=True)
    return font, _wrap_text(draw, text.upper(), font, max_width)[:4]


def _draw_column(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    x: int,
    y: int,
    width: int,
) -> None:
    for line in _wrap_text(draw, text, font, width)[:6]:
        draw.text((x, y), line, font=font, fill=MUTED_INK)
        y += _line_height(draw, font) + 10


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


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
