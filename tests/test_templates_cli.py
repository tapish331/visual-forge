from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.templates import build_inventory, scaffold_template, validate_template_file


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def test_inventory_finds_simple_card() -> None:
    result = run_cli("templates")

    assert result.returncode == 0, result.stderr
    assert "simple_card (valid) png v1.0.0" in result.stdout


def test_templates_json_returns_valid_compact_json() -> None:
    result = run_cli("templates", "--json")

    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)
    assert inventory["total"] >= 1
    assert inventory["valid_count"] >= 1
    assert inventory["invalid_count"] == 0
    template_ids = {template["template_id"] for template in inventory["templates"]}
    assert "simple_card" in template_ids


def test_simple_card_passes_contract_validation() -> None:
    result = run_cli("validate-template", "templates/simple_card.py")

    assert result.returncode == 0, result.stderr
    assert "Template: simple_card" in result.stdout
    assert "Status: valid" in result.stdout


def test_newspaper_template_is_ready_and_asset_backed() -> None:
    result = validate_template_file(REPO_ROOT / "templates" / "newspaper_headline.py")

    assert result["valid"] is True
    assert result["ready"] is True
    assert result["capabilities"] == ["newspaper_headline"]


def test_validate_template_json_returns_valid_result() -> None:
    result = run_cli("validate-template", "templates/simple_card.py", "--json")

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["valid"] is True
    assert summary["template_id"] == "simple_card"
    assert summary["capabilities"] == ["definition", "key_point", "quote", "title_card"]


def test_template_capabilities_are_optional_but_malformed_values_are_invalid(tmp_path: Path) -> None:
    optional_file = write_contract_template(tmp_path / "optional.py", "return {}")
    malformed_file = write_contract_template(
        tmp_path / "malformed.py",
        "return {'capabilities': ['Valid-But-Not-Snake-Case']}",
    )

    optional = validate_template_file(optional_file)
    malformed = validate_template_file(malformed_file)

    assert optional["valid"] is True
    assert optional["capabilities"] == []
    assert malformed["valid"] is False
    assert "lowercase snake-case" in "; ".join(malformed["errors"])


def test_invalid_template_reports_missing_contract_fields(tmp_path: Path) -> None:
    template_file = tmp_path / "bad_template.py"
    template_file.write_text('TEMPLATE_ID = "bad_template"\n', encoding="utf-8")

    result = validate_template_file(template_file)

    assert result["valid"] is False
    assert "TEMPLATE_VERSION must be a non-empty string" in result["errors"]
    assert "OUTPUT_TYPE must be a non-empty string" in result["errors"]
    assert "metadata must be callable" in result["errors"]


def test_validate_template_returns_nonzero_for_missing_file(tmp_path: Path) -> None:
    result = run_cli("validate-template", tmp_path / "missing.py")

    assert result.returncode != 0
    assert "Template file not found" in result.stdout


def test_invalid_templates_appear_in_inventory_without_breaking_scan(tmp_path: Path) -> None:
    valid_template = tmp_path / "valid_template.py"
    valid_template.write_text(
        "\n".join(
            [
                'TEMPLATE_ID = "valid_template"',
                'TEMPLATE_VERSION = "1.0.0"',
                'OUTPUT_TYPE = "png"',
                "def metadata():",
                "    return {}",
                "def validate_params(params):",
                "    return []",
                "def required_assets(params):",
                "    return []",
                "def render(params, output_path):",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    invalid_template = tmp_path / "invalid_template.py"
    invalid_template.write_text('TEMPLATE_ID = "invalid_template"\n', encoding="utf-8")

    inventory = build_inventory(tmp_path)

    assert inventory["total"] == 2
    assert inventory["valid_count"] == 1
    assert inventory["invalid_count"] == 1
    invalid_entries = [item for item in inventory["templates"] if item["name"] == "invalid_template"]
    assert len(invalid_entries) == 1
    assert invalid_entries[0]["valid"] is False


def test_scaffold_template_is_valid_draft_and_not_ready(tmp_path: Path) -> None:
    result = scaffold_template("timeline_map", "timeline_map", tmp_path)
    duplicate = scaffold_template("timeline_map", "timeline_map", tmp_path)

    assert result["success"] is True
    assert duplicate["success"] is False
    info = validate_template_file(tmp_path / "timeline_map.py")
    assert info["valid"] is True
    assert info["status"] == "draft"
    assert info["ready"] is False
    inventory = build_inventory(tmp_path)
    assert inventory["draft_count"] == 1
    assert inventory["ready_count"] == 0


def write_contract_template(path: Path, metadata_body: str) -> Path:
    path.write_text(
        "\n".join(
            [
                f'TEMPLATE_ID = "{path.stem}"',
                'TEMPLATE_VERSION = "1.0.0"',
                'OUTPUT_TYPE = "png"',
                "def metadata():",
                f"    {metadata_body}",
                "def validate_params(params):",
                "    return []",
                "def required_assets(params):",
                "    return []",
                "def render(params, output_path):",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path
