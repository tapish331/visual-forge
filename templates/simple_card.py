"""Simple title/subtitle card template."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast


TEMPLATE_ID = "simple_card"
TEMPLATE_VERSION = "1.0.0"
OUTPUT_TYPE = "png"
WIDTH = 1920
HEIGHT = 1080
HORIZONTAL_MARGIN = 220
TITLE_FONT_SIZE = 86
SUBTITLE_FONT_SIZE = 46
TITLE_COLOR = "#111827"
SUBTITLE_COLOR = "#46515f"
BACKGROUND_COLOR = "#f6f7f9"
ACCENT_COLOR = "#2457a6"


class ImageLike(Protocol):
    def save(self, fp: str | Path, format: str | None = None) -> None: ...


class DrawLike(Protocol):
    def rectangle(self, xy: tuple[int, int, int, int], *, fill: str) -> None: ...
    def textbbox(self, xy: tuple[int, int], text: str, *, font: object) -> tuple[int, int, int, int]: ...
    def text(self, xy: tuple[int, int], text: str, *, font: object, fill: str) -> None: ...


class ImageModuleLike(Protocol):
    def new(self, mode: str, size: tuple[int, int], color: str) -> ImageLike: ...


class ImageDrawModuleLike(Protocol):
    def Draw(self, im: ImageLike) -> DrawLike: ...


class ImageFontModuleLike(Protocol):
    def truetype(self, font: str, size: int) -> object: ...
    def load_default(self) -> object: ...


def metadata() -> dict[str, object]:
    return {
        "name": "Simple Card",
        "description": "A minimal deterministic title/subtitle card.",
        "required_params": ["title"],
        "optional_params": ["subtitle"],
        "capabilities": ["title_card", "key_point", "quote", "definition"],
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

    return errors


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return []


def render(params: dict[str, object], output_path: str) -> None:
    errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))

    title = str(params["title"]).strip()
    subtitle_value = params.get("subtitle")
    subtitle = subtitle_value.strip() if isinstance(subtitle_value, str) else ""

    image_module, draw_module, font_module = _load_pillow_modules()
    image = image_module.new("RGB", (WIDTH, HEIGHT), BACKGROUND_COLOR)
    draw = draw_module.Draw(image)

    draw.rectangle((0, 0, WIDTH, 18), fill=ACCENT_COLOR)
    draw.rectangle((0, HEIGHT - 18, WIDTH, HEIGHT), fill=ACCENT_COLOR)
    draw.rectangle((HORIZONTAL_MARGIN, 206, HORIZONTAL_MARGIN + 12, HEIGHT - 206), fill=ACCENT_COLOR)

    title_font = _load_font(font_module, TITLE_FONT_SIZE, bold=True)
    subtitle_font = _load_font(font_module, SUBTITLE_FONT_SIZE, bold=False)
    max_text_width = WIDTH - (HORIZONTAL_MARGIN * 2)

    title_lines = _wrap_text(draw, title, title_font, max_text_width)
    subtitle_lines = _wrap_text(draw, subtitle, subtitle_font, max_text_width) if subtitle else []

    title_line_height = _line_height(draw, title_font)
    subtitle_line_height = _line_height(draw, subtitle_font)
    title_gap = 18
    subtitle_gap = 12
    block_gap = 44 if subtitle_lines else 0
    title_block_height = len(title_lines) * title_line_height + max(0, len(title_lines) - 1) * title_gap
    subtitle_block_height = (
        len(subtitle_lines) * subtitle_line_height + max(0, len(subtitle_lines) - 1) * subtitle_gap
    )
    total_height = title_block_height + block_gap + subtitle_block_height
    y = max(160, (HEIGHT - total_height) // 2)

    for line in title_lines:
        _draw_centered_text(draw, line, title_font, y, TITLE_COLOR)
        y += title_line_height + title_gap

    y += block_gap - title_gap if subtitle_lines else 0
    for line in subtitle_lines:
        _draw_centered_text(draw, line, subtitle_font, y, SUBTITLE_COLOR)
        y += subtitle_line_height + subtitle_gap

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")


def _load_pillow_modules() -> tuple[ImageModuleLike, ImageDrawModuleLike, ImageFontModuleLike]:
    try:
        image_module = import_module("PIL.Image")
        draw_module = import_module("PIL.ImageDraw")
        font_module = import_module("PIL.ImageFont")
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render simple_card. Install project dependencies first.") from exc

    return (
        cast(ImageModuleLike, _module(image_module)),
        cast(ImageDrawModuleLike, _module(draw_module)),
        cast(ImageFontModuleLike, _module(font_module)),
    )


def _module(value: ModuleType) -> ModuleType:
    return value


def _load_font(font_module: ImageFontModuleLike, size: int, *, bold: bool) -> object:
    candidates = (
        ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf")
        if bold
        else ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf")
    )
    for candidate in candidates:
        try:
            return font_module.truetype(candidate, size=size)
        except OSError:
            continue
    return font_module.load_default()


def _wrap_text(
    draw: DrawLike,
    text: str,
    font: object,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        if _text_width(draw, word, font) <= max_width:
            current = word
        else:
            broken_words = _break_long_word(draw, word, font, max_width)
            lines.extend(broken_words[:-1])
            current = broken_words[-1] if broken_words else ""

    if current:
        lines.append(current)

    return lines or [text]


def _break_long_word(
    draw: DrawLike,
    word: str,
    font: object,
    max_width: int,
) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            pieces.append(current)
            current = char
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _draw_centered_text(
    draw: DrawLike,
    text: str,
    font: object,
    y: int,
    fill: str,
) -> None:
    width = _text_width(draw, text, font)
    x = (WIDTH - width) // 2
    draw.text((x, y), text, font=font, fill=fill)


def _text_width(draw: DrawLike, text: str, font: object) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _line_height(draw: DrawLike, font: object) -> int:
    _, top, _, bottom = draw.textbbox((0, 0), "Ag", font=font)
    return bottom - top
