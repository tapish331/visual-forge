from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image

from app.assets import register_asset
from app.render_template import render_template


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


def test_newspaper_template_renders_registered_asset_backed_png(tmp_path: Path) -> None:
    output_file = tmp_path / "newspaper.png"

    result = run_cli(
        "render-template",
        "newspaper_headline",
        output_file,
        "--params-json",
        '{"headline":"A New Capability","publication":"Visual Forge"}',
        "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["required_assets"] == ["newspaper_base"]
    assert "newspaper_base" in summary["asset_fingerprints"]
    assert read_png_size(output_file) == (1920, 1080)


def test_required_asset_is_enforced_before_existing_output_is_replaced(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    assets_dir = tmp_path / "assets"
    templates_dir.mkdir()
    template_file = templates_dir / "asset_template.py"
    template_file.write_text(
        "\n".join(
            [
                'TEMPLATE_ID = "asset_template"',
                'TEMPLATE_VERSION = "1.0.0"',
                'OUTPUT_TYPE = "png"',
                'def metadata(): return {"capabilities": ["asset_test"]}',
                'def validate_params(params): return []',
                'def required_assets(params): return ["test_asset"]',
                'def render(params, output_path): open(output_path, "wb").write(b"new")',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    asset_file = assets_dir / "images" / "test.png"
    asset_file.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(asset_file, format="PNG")
    assert register_asset(asset_file, "test_asset", "1.0.0", assets_dir=assets_dir)["success"] is True
    output = tmp_path / "output.png"
    output.write_bytes(b"previous")
    asset_file.unlink()

    result = render_template("asset_template", output, {}, templates_dir, assets_dir)

    assert result["success"] is False
    assert output.read_bytes() == b"previous"
    assert "missing" in "; ".join(result["errors"]).lower()


def read_png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    width_raw, height_raw = struct.unpack(">II", data[16:24])
    assert isinstance(width_raw, int)
    assert isinstance(height_raw, int)
    width = width_raw
    height = height_raw
    return width, height
