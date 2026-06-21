from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def test_render_template_by_id_creates_png(tmp_path: Path) -> None:
    output_file = tmp_path / "renders" / "simple_card.png"

    result = run_cli(
        "render-template",
        "simple_card",
        output_file,
        "--params-json",
        '{"title":"Hello","subtitle":"World"}',
    )

    assert result.returncode == 0, result.stderr
    assert output_file.exists()
    assert output_file.stat().st_size > 0
    assert read_png_size(output_file) == (1920, 1080)


def test_render_template_by_path_creates_png(tmp_path: Path) -> None:
    output_file = tmp_path / "simple_card_path.png"

    result = run_cli(
        "render-template",
        "templates/simple_card.py",
        output_file,
        "--params-json",
        '{"title":"Direct path"}',
        "--json",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["success"] is True
    assert summary["template_id"] == "simple_card"
    assert Path(summary["output_path"]).exists()


def test_render_template_missing_title_returns_nonzero(tmp_path: Path) -> None:
    output_file = tmp_path / "missing_title.png"

    result = run_cli("render-template", "simple_card", output_file, "--params-json", '{"subtitle":"Only subtitle"}')

    assert result.returncode != 0
    assert "title must be a non-empty string" in result.stdout
    assert not output_file.exists()


def test_render_template_invalid_params_json_returns_nonzero(tmp_path: Path) -> None:
    output_file = tmp_path / "invalid_json.png"

    result = run_cli("render-template", "simple_card", output_file, "--params-json", "{not-json")

    assert result.returncode != 0
    assert "Invalid params JSON" in result.stdout
    assert not output_file.exists()


def test_render_template_missing_template_returns_nonzero(tmp_path: Path) -> None:
    output_file = tmp_path / "missing_template.png"

    result = run_cli("render-template", "does_not_exist", output_file, "--params-json", '{"title":"Hello"}')

    assert result.returncode != 0
    assert "Template not found: does_not_exist" in result.stdout
    assert not output_file.exists()


def test_render_template_creates_output_parent_directory(tmp_path: Path) -> None:
    output_file = tmp_path / "nested" / "deep" / "simple_card.png"

    result = run_cli("render-template", "simple_card", output_file, "--params-json", '{"title":"Nested"}')

    assert result.returncode == 0, result.stderr
    assert output_file.exists()


def read_png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    width_raw, height_raw = struct.unpack(">II", data[16:24])
    assert isinstance(width_raw, int)
    assert isinstance(height_raw, int)
    width = width_raw
    height = height_raw
    return width, height
