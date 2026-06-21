from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def init_project(project_dir: Path) -> None:
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_preview_renders_png_and_records_project_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli(
        "preview",
        project_dir,
        "--template",
        "simple_card",
        "--params-json",
        '{"title":"Hello","subtitle":"World"}',
    )

    assert result.returncode == 0, result.stderr
    data = load_project(project_dir)
    previews = record_list(data, "previews")
    assert len(previews) == 1
    preview = previews[0]
    assert preview["template_ref"] == "simple_card"
    assert preview["template_id"] == "simple_card"
    assert preview["status"] == "rendered"
    assert preview["template_version"] == "1.0.0"
    assert isinstance(preview["updated_at"], str)
    assert object_dict(preview, "template_fingerprint")["kind"] == "sha256_v1"
    assert object_dict(preview, "artifact_fingerprint")["kind"] == "sha256_v1"
    output_value = preview["output"]
    assert isinstance(output_value, str)
    output = project_dir / output_value
    assert output.exists()
    assert read_png_size(output) == (1920, 1080)


def test_preview_json_returns_compact_machine_readable_summary(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli(
        "preview",
        project_dir,
        "--template",
        "simple_card",
        "--params-json",
        '{"title":"JSON"}',
        "--json",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["success"] is True
    assert summary["preview_id"].startswith("preview_")
    assert summary["template_id"] == "simple_card"
    assert Path(summary["output_path"]).exists()
    assert summary["errors"] == []


def test_preview_rerun_uses_same_id_without_duplicate_record(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    args = (
        "preview",
        project_dir,
        "--template",
        "simple_card",
        "--params-json",
        '{"subtitle":"World","title":"Hello"}',
        "--json",
    )

    first = run_cli(*args)
    second = run_cli(*args)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json.loads(first.stdout)
    second_summary = json.loads(second.stdout)
    assert first_summary["preview_id"] == second_summary["preview_id"]
    assert first_summary["output_path"] == second_summary["output_path"]
    data = load_project(project_dir)
    previews = record_list(data, "previews")
    assert len(previews) == 1


def test_preview_normalizes_existing_project_without_previews(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    data = load_project(project_dir)
    data.pop("previews")
    write_project(project_dir, data)

    result = run_cli("preview", project_dir, "--template", "simple_card", "--params-json", '{"title":"Old"}')

    assert result.returncode == 0, result.stderr
    updated = load_project(project_dir)
    previews = record_list(updated, "previews")
    assert len(previews) == 1


def test_preview_invalid_params_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli("preview", project_dir, "--template", "simple_card", "--params-json", '{"subtitle":"Only"}')

    assert result.returncode != 0
    assert "title must be a non-empty string" in result.stdout
    data = load_project(project_dir)
    assert data["previews"] == []
    failures = record_list(data, "failures")
    assert len(failures) == 1
    failure = failures[0]
    assert failure["stage"] == "preview"
    assert failure["template_ref"] == "simple_card"
    assert failure["recommended_next_action"] == "Fix the template reference or params, then rerun preview."


def test_preview_missing_template_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli("preview", project_dir, "--template", "does_not_exist", "--params-json", '{"title":"Hello"}')

    assert result.returncode != 0
    assert "Template not found: does_not_exist" in result.stdout
    data = load_project(project_dir)
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "preview"
    assert failures[0]["template_ref"] == "does_not_exist"


def record_list(data: ProjectJson, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    records: list[Record] = []
    for item in items:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def object_dict(data: Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def read_png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    width_raw, height_raw = struct.unpack(">II", data[16:24])
    assert isinstance(width_raw, int)
    assert isinstance(height_raw, int)
    width = width_raw
    height = height_raw
    return width, height
