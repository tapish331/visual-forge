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


def test_chunk_preview_layout_project_writes_outputs_and_updates_state(tmp_path: Path) -> None:
    project_dir, outputs_dir = prepared_layout_project(tmp_path)
    visual_id = add_chunk_visual(project_dir)

    result = run_cli("preview", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["chunk_id"] == "chunk_001"
    assert summary["visual_count"] == 1
    output_path = Path(string_value(summary, "output_path"))
    manifest_path = Path(string_value(summary, "manifest_path"))
    assert output_path == outputs_dir / "chunk-previews" / "chunk_001.png"
    assert manifest_path == outputs_dir / "chunk-previews" / "chunk_001.json"
    assert read_png_size(output_path) == (1920, 1080)

    manifest = json_object(manifest_path.read_text(encoding="utf-8"))
    assert manifest["chunk_id"] == "chunk_001"
    assert manifest["visual_count"] == 1

    data = load_project(project_dir)
    chunk = record_list(data, "chunks")[0]
    assert chunk["status"] == "previewed"
    visual = record_list(data, "visuals")[0]
    assert visual["id"] == visual_id
    assert visual["status"] == "previewed"
    assert isinstance(visual["preview_id"], str)
    chunk_preview = record_list(data, "chunk_previews")[0]
    assert chunk_preview["id"] == "chunk_preview_chunk_001"
    assert chunk_preview["output"] == "chunk-previews/chunk_001.png"
    assert chunk_preview["manifest"] == "chunk-previews/chunk_001.json"


def test_chunk_preview_legacy_project_writes_project_local_artifacts(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    add_chunk_visual(project_dir)

    result = run_cli("preview", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    output_path = Path(string_value(summary, "output_path"))
    manifest_path = Path(string_value(summary, "manifest_path"))
    assert output_path == project_dir / "chunk-previews" / "chunk_001.png"
    assert manifest_path == project_dir / "chunk-previews" / "chunk_001.json"
    assert read_png_size(output_path) == (1920, 1080)


def test_chunk_preview_rerun_upserts_record_without_duplication(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    add_chunk_visual(project_dir)

    first = run_cli("preview", project_dir, "--chunk", "chunk_001", "--json")
    second = run_cli("preview", project_dir, "--chunk", "chunk_001", "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json_object(first.stdout)
    second_summary = json_object(second.stdout)
    assert first_summary["chunk_preview_id"] == second_summary["chunk_preview_id"]
    data = load_project(project_dir)
    assert len(record_list(data, "chunk_previews")) == 1
    assert len(record_list(data, "previews")) == 1


def test_chunk_preview_human_output_is_compact(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    add_chunk_visual(project_dir)

    result = run_cli("preview", project_dir, "--chunk", "chunk_001")

    assert result.returncode == 0, result.stderr
    assert "Chunk: chunk_001" in result.stdout
    assert "Visuals: 1" in result.stdout
    assert "Status: rendered" in result.stdout
    assert "Output:" in result.stdout


def test_chunk_preview_missing_chunk_records_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)

    result = run_cli("preview", project_dir, "--chunk", "chunk_missing")

    assert result.returncode != 0
    assert "Chunk not found: chunk_missing" in result.stdout
    data = load_project(project_dir)
    failures = record_list(data, "failures")
    assert len(failures) == 1
    failure = failures[0]
    assert failure["stage"] == "chunk_preview"
    assert failure["scope"] == "chunk:chunk_missing"
    assert failure["chunk_id"] == "chunk_missing"


def test_chunk_preview_no_visuals_records_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)

    result = run_cli("preview", project_dir, "--chunk", "chunk_001")

    assert result.returncode != 0
    assert "No visuals found for chunk: chunk_001" in result.stdout
    failures = record_list(load_project(project_dir), "failures")
    assert failures[0]["stage"] == "chunk_preview"


def test_chunk_preview_render_failure_preserves_prior_artifact(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    add_chunk_visual(project_dir)
    prior = project_dir / "chunk-previews" / "chunk_001.png"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior artifact")
    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    visual["params"] = {"subtitle": "Missing title"}
    write_project(project_dir, data)

    result = run_cli("preview", project_dir, "--chunk", "chunk_001")

    assert result.returncode != 0
    assert "title must be a non-empty string" in result.stdout
    assert prior.read_bytes() == b"prior artifact"
    failures = record_list(load_project(project_dir), "failures")
    assert failures[0]["stage"] == "chunk_preview"


def test_preview_chunk_rejects_template_arguments(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)

    result = run_cli(
        "preview",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--params-json",
        '{"title":"Bad"}',
    )

    assert result.returncode != 0
    assert "--chunk cannot be combined" in result.stderr


def prepared_legacy_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "my-video"
    assert run_cli("init", project_dir).returncode == 0
    add_chunk(project_dir)
    return project_dir


def prepared_layout_project(tmp_path: Path) -> tuple[Path, Path]:
    input_dir = tmp_path / "inputs" / "episode"
    input_dir.mkdir(parents=True)
    (input_dir / "script.txt").write_text("Hello world.\n", encoding="utf-8")
    (input_dir / "raw.mp4").write_bytes(b"raw video")
    assert run_cli("init-from-input", input_dir, "--json").returncode == 0
    project_dir = tmp_path / "projects" / "episode"
    outputs_dir = tmp_path / "outputs" / "episode"
    add_chunk(project_dir)
    return project_dir, outputs_dir


def add_chunk(project_dir: Path) -> None:
    data = load_project(project_dir)
    chunks = data["chunks"]
    assert isinstance(chunks, list)
    chunks.append(
        {
            "id": "chunk_001",
            "start": 10.0,
            "end": 30.0,
            "status": "new",
            "alignment_block_ids": ["block_001"],
            "warning_block_ids": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
    )
    write_project(project_dir, data)


def add_chunk_visual(project_dir: Path) -> str:
    result = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--start",
        "12.5",
        "--end",
        "18",
        "--params-json",
        '{"title":"Key idea"}',
        "--json",
    )
    assert result.returncode == 0, result.stderr
    visual_id = json_object(result.stdout)["visual_id"]
    assert isinstance(visual_id, str)
    return visual_id


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def record_list(data: ProjectJson | Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    records: list[Record] = []
    for item in items:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def string_value(data: Record, key: str) -> str:
    value = data[key]
    assert isinstance(value, str)
    return value


def read_png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    width_raw, height_raw = struct.unpack(">II", data[16:24])
    assert isinstance(width_raw, int)
    assert isinstance(height_raw, int)
    return width_raw, height_raw
