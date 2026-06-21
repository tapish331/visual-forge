from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from app.assets import build_asset_inventory, register_asset, validate_asset


def test_register_list_validate_and_replace_asset(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    asset_file = write_png(assets_dir / "images" / "paper.png", (120, 80), "#ddd2b8")

    first = register_asset(asset_file, "paper_base", "1.0.0", assets_dir=assets_dir)
    reused = register_asset(asset_file, "paper_base", "1.0.0", assets_dir=assets_dir)
    write_png(asset_file, (140, 90), "#cfc1a2")
    conflict = register_asset(asset_file, "paper_base", "1.0.1", assets_dir=assets_dir)
    replaced = register_asset(asset_file, "paper_base", "1.0.1", replace=True, assets_dir=assets_dir)

    assert first["success"] is True
    assert reused["success"] is True and reused["reused_existing"] is True
    assert conflict["success"] is False
    assert replaced["success"] is True
    validation = validate_asset("paper_base", assets_dir)
    assert validation["valid"] is True
    assert validation["asset"] is not None
    assert validation["asset"]["width"] == 140
    inventory = build_asset_inventory(assets_dir)
    assert inventory["total"] == 1
    assert inventory["current_count"] == 1
    assert not list(assets_dir.glob(".manifest.json.*.tmp"))


def test_registration_rejects_invalid_external_missing_and_malformed_assets(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    outside = write_png(tmp_path / "outside.png", (20, 20), "white")
    malformed = assets_dir / "images" / "bad.png"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not png", encoding="utf-8")

    assert register_asset(outside, "outside", "1.0.0", assets_dir=assets_dir)["success"] is False
    assert register_asset(malformed, "bad_image", "1.0.0", assets_dir=assets_dir)["success"] is False
    assert register_asset(assets_dir / "missing.png", "missing", "1.0.0", assets_dir=assets_dir)["success"] is False
    assert validate_asset("Bad-ID", assets_dir)["state"] == "invalid"


def test_modified_or_deleted_asset_is_stale_or_missing(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    asset_file = write_png(assets_dir / "images" / "paper.png", (40, 40), "white")
    assert register_asset(asset_file, "paper", "1.0.0", assets_dir=assets_dir)["success"] is True

    write_png(asset_file, (40, 40), "black")
    assert validate_asset("paper", assets_dir)["state"] == "stale"
    asset_file.unlink()
    assert validate_asset("paper", assets_dir)["state"] == "missing"


def test_concurrent_asset_registration_preserves_both_records(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    first_file = write_png(assets_dir / "images" / "first.png", (30, 20), "red")
    second_file = write_png(assets_dir / "images" / "second.png", (20, 30), "blue")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(register_asset, first_file, "first_asset", "1.0.0", assets_dir=assets_dir),
            executor.submit(register_asset, second_file, "second_asset", "1.0.0", assets_dir=assets_dir),
        ]
    results = [future.result() for future in futures]

    assert all(result["success"] for result in results)
    inventory = build_asset_inventory(assets_dir)
    assert inventory["total"] == 2
    assert {result["asset_id"] for result in inventory["assets"]} == {"first_asset", "second_asset"}


def test_repository_newspaper_asset_is_registered_and_current() -> None:
    result = validate_asset("newspaper_base")

    assert result["valid"] is True
    assert result["asset"] is not None
    assert result["asset"]["width"] == 1920
    assert result["asset"]["height"] == 1080


def write_png(path: Path, size: tuple[int, int], color: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path
