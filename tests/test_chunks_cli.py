from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.project import JsonObject, JsonValue, ProjectState, build_initial_project, write_project


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VISUAL_FORGE_LOG_DISABLED"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
    )


def test_create_chunks_json_creates_one_short_chunk_with_warning_ids(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [
            aligned_block("block_001", 0.0, 40.0),
            warning_block("block_002", "needs_review"),
            aligned_block("block_003", 42.0, 100.0),
        ],
    )

    result = run_cli("create-chunks", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["chunks_created"] == 1
    chunks = record_list(summary, "chunks")
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["id"] == "chunk_001"
    assert chunk["start"] == 0.0
    assert chunk["end"] == 100.0
    assert chunk["status"] == "new"
    assert chunk["visual_mode"] == "undecided"
    assert chunk["alignment_block_ids"] == ["block_001", "block_003"]
    assert chunk["warning_block_ids"] == ["block_002"]
    assert isinstance(chunk["created_at"], str)
    assert chunk["updated_at"] == chunk["created_at"]

    project = load_project(project_dir)
    assert record_list(project, "chunks") == chunks


def test_create_chunks_splits_long_alignment_deterministically(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [
            aligned_block("block_001", 0.0, 60.0),
            aligned_block("block_002", 60.0, 120.0),
            aligned_block("block_003", 120.0, 180.0),
            aligned_block("block_004", 180.0, 240.0),
            aligned_block("block_005", 240.0, 300.0),
        ],
    )

    result = run_cli(
        "create-chunks",
        project_dir,
        "--target-seconds",
        "120",
        "--min-seconds",
        "60",
        "--max-seconds",
        "150",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    chunks = record_list(json_object(result.stdout), "chunks")
    assert [chunk["id"] for chunk in chunks] == ["chunk_001", "chunk_002", "chunk_003"]
    assert [(chunk["start"], chunk["end"]) for chunk in chunks] == [
        (0.0, 120.0),
        (120.0, 240.0),
        (240.0, 300.0),
    ]
    assert chunks[0]["alignment_block_ids"] == ["block_001", "block_002"]
    assert chunks[1]["alignment_block_ids"] == ["block_003", "block_004"]
    assert chunks[2]["alignment_block_ids"] == ["block_005"]


def test_chunks_human_and_json_output_list_chunk_details(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0

    human = run_cli("chunks", project_dir)
    machine = run_cli("chunks", project_dir, "--json")

    assert human.returncode == 0, human.stderr
    assert "Chunks: 1" in human.stdout
    assert "chunk_001 0 -> 12.5 new mode=undecided aligned=1 warnings=0" in human.stdout
    assert machine.returncode == 0, machine.stderr
    summary = json_object(machine.stdout)
    assert summary["total"] == 1
    assert object_dict(summary, "by_status")["new"] == 1
    assert len(record_list(summary, "chunks")) == 1
    assert object_dict(object_dict(summary, "freshness"), "timeline")["state"] == "current"
    assert object_dict(object_dict(summary, "freshness"), "chunking")["state"] == "current"
    assert object_dict(object_dict(summary, "chunking"), "coverage")["complete"] is True


def test_chunks_preserve_full_timeline_and_split_pause_at_midpoint(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [
            aligned_block("block_001", 1.0, 40.0),
            aligned_block("block_002", 44.0, 100.0),
        ],
        raw_duration=105.0,
    )

    result = run_cli(
        "create-chunks",
        project_dir,
        "--target-seconds",
        "30",
        "--min-seconds",
        "10",
        "--max-seconds",
        "50",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    chunks = record_list(summary, "chunks")
    assert [(chunk["start"], chunk["end"]) for chunk in chunks] == [(0.0, 42.0), (42.0, 105.0)]
    chunking = object_dict(summary, "chunking")
    coverage = object_dict(chunking, "coverage")
    assert coverage["head_gap_seconds"] == 0.0
    assert coverage["tail_gap_seconds"] == 0.0
    assert coverage["internal_gap_seconds"] == 0.0
    assert coverage["overlap_seconds"] == 0.0
    assert coverage["complete"] is True


def test_create_chunks_refuses_existing_chunks_until_force_and_resolves_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0

    first_failure = run_cli("create-chunks", project_dir, "--json")
    second_failure = run_cli("create-chunks", project_dir, "--json")

    assert first_failure.returncode == 1
    assert second_failure.returncode == 1
    failure = only_failure(project_dir)
    assert failure["stage"] == "chunks"
    assert failure["scope"] == "chunks:project"
    assert failure["status"] == "active"
    assert failure["attempt_count"] == 2

    retry = run_cli("create-chunks", project_dir, "--force", "--json")

    assert retry.returncode == 0, retry.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def test_create_chunks_force_preserves_unrelated_project_collections(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0
    project = load_project(project_dir)
    project["visuals"] = [{"id": "visual_1", "status": "planned"}]
    project["previews"] = [{"id": "preview_1", "status": "rendered"}]
    project["corrections"] = [{"stage": "visual", "created_at": "2026-06-20T00:00:00Z"}]
    object_dict(project, "cache")["kept"] = True
    write_project_json(project_dir, project)

    result = run_cli("create-chunks", project_dir, "--force", "--json")

    assert result.returncode == 0, result.stderr
    updated = load_project(project_dir)
    assert updated["visuals"] == [{"id": "visual_1", "status": "planned"}]
    assert updated["previews"] == [{"id": "preview_1", "status": "rendered"}]
    assert updated["corrections"] == [{"stage": "visual", "created_at": "2026-06-20T00:00:00Z"}]
    assert object_dict(updated, "cache")["kept"] is True


def test_create_chunks_force_is_atomic_when_visual_would_be_invalid(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0
    project = load_project(project_dir)
    project["visuals"] = [
        {
            "id": "visual_orphaned",
            "chunk_id": "chunk_999",
            "start": 2.0,
            "end": 4.0,
            "status": "planned",
        }
    ]
    write_project_json(project_dir, project)
    before = (project_dir / "project.json").read_bytes()

    result = run_cli("create-chunks", project_dir, "--force", "--json")

    assert result.returncode == 1
    assert "visual_orphaned" in result.stdout
    assert (project_dir / "project.json").read_bytes() == before


def test_missing_stale_unverified_and_invalid_alignment_record_chunk_failures(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])

    (project_dir / "alignment" / "script_alignment.json").unlink()
    missing = run_cli("create-chunks", project_dir, "--json")
    assert missing.returncode == 1
    assert_chunk_failure(project_dir, expected_attempts=1)

    restore_alignment(project_dir, [aligned_block("block_001", 0.0, 12.5)])
    (project_dir / "alignment" / "script_alignment.json").write_text('{"changed":true}\n', encoding="utf-8")
    stale = run_cli("create-chunks", project_dir, "--json")
    assert stale.returncode == 1
    assert_chunk_failure(project_dir, expected_attempts=2)

    restore_alignment(project_dir, [aligned_block("block_001", 0.0, 12.5)])
    project = load_project(project_dir)
    object_dict(object_dict(project, "alignment"), "script").pop("artifact_fingerprint")
    write_project_json(project_dir, project)
    unverified = run_cli("create-chunks", project_dir, "--json")
    assert unverified.returncode == 1
    assert_chunk_failure(project_dir, expected_attempts=3)

    restore_alignment(project_dir, [aligned_block("block_001", 0.0, 12.5)])
    invalid_file = project_dir / "alignment" / "script_alignment.json"
    invalid_file.write_text("{invalid", encoding="utf-8")
    project = load_project(project_dir)
    object_dict(object_dict(project, "alignment"), "script")["artifact_fingerprint"] = sha256_fingerprint(invalid_file)
    write_project_json(project_dir, project)
    invalid = run_cli("create-chunks", project_dir, "--json")
    assert invalid.returncode == 1
    assert "Invalid JSON in alignment artifact" in invalid.stdout
    assert_chunk_failure(project_dir, expected_attempts=4)


def test_no_aligned_blocks_records_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [warning_block("block_001", "unmatched")])

    result = run_cli("create-chunks", project_dir, "--json")

    assert result.returncode == 1
    assert "does not contain any aligned blocks" in result.stdout
    assert_chunk_failure(project_dir, expected_attempts=1)


def test_status_moves_to_in_progress_after_chunks_are_created(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "in_progress"
    assert status["next_action"] == "Plan visuals for each chunk."
    chunks = object_dict(status, "chunks")
    assert chunks["total"] == 1
    assert object_dict(chunks, "by_status")["new"] == 1


def test_approve_camera_only_and_adding_visual_resets_chunk(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0

    approved = run_cli("approve-camera-only", project_dir, "chunk_001", "--json")

    assert approved.returncode == 0, approved.stderr
    chunk = record_list(load_project(project_dir), "chunks")[0]
    assert chunk["visual_mode"] == "camera_only"
    assert chunk["status"] == "previewed"
    assert isinstance(chunk["camera_only_at"], str)

    added = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--start",
        "2",
        "--end",
        "4",
        "--params-json",
        '{"title":"Visual"}',
        "--json",
    )

    assert added.returncode == 0, added.stderr
    chunk = record_list(load_project(project_dir), "chunks")[0]
    assert chunk["visual_mode"] == "visuals"
    assert chunk["status"] == "new"
    assert "camera_only_at" not in chunk


def test_approve_camera_only_rejects_existing_visuals(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.5)])
    assert run_cli("create-chunks", project_dir).returncode == 0
    assert run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--start",
        "2",
        "--end",
        "4",
        "--params-json",
        '{"title":"Visual"}',
    ).returncode == 0

    result = run_cli("approve-camera-only", project_dir, "chunk_001", "--json")

    assert result.returncode == 1
    assert "cannot be camera-only" in result.stdout
    failure = only_failure(project_dir)
    assert failure["stage"] == "camera_only"
    assert failure["scope"] == "chunk:chunk_001"


def prepare_project(tmp_path: Path, blocks: list[Record], *, raw_duration: float | None = None) -> Path:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    (project_dir / "script.txt").write_text("Hello world.\n", encoding="utf-8")
    raw_file = project_dir / "raw.mp4"
    raw_file.write_bytes(b"raw video")
    audio_file = project_dir / "audio" / "narration.wav"
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"narration audio")
    transcript_file = project_dir / "transcripts" / "narration.json"
    atomic_write_json(
        transcript_file,
        {
            "schema_version": 1,
            "source": "audio/narration.wav",
            "text": "Hello world.",
            "segments": [],
        },
    )
    write_alignment_artifact(project_dir, blocks)

    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    alignment_file = project_dir / "alignment" / "script_alignment.json"

    state: ProjectState = build_initial_project(project_dir)
    state["media"] = {
        "raw": {
            "path": "raw.mp4",
            "duration_seconds": raw_duration if raw_duration is not None else alignment_duration(blocks),
            "source_fingerprint": raw_fingerprint,
        },
        "audio": {
            "narration": {
                "path": "audio/narration.wav",
                "source": "raw.mp4",
                "source_fingerprint": raw_fingerprint,
                "artifact_fingerprint": audio_fingerprint,
            }
        },
    }
    state["transcript"] = {
        "narration": {
            "path": "transcripts/narration.json",
            "source": "audio/narration.wav",
            "status": "transcribed",
            "source_fingerprint": audio_fingerprint,
            "artifact_fingerprint": transcript_fingerprint,
        }
    }
    state["alignment"] = alignment_metadata(project_dir, blocks)
    write_project(project_dir / "project.json", state)
    assert alignment_file.is_file()
    return project_dir


def restore_alignment(project_dir: Path, blocks: list[Record]) -> None:
    write_alignment_artifact(project_dir, blocks)
    project = load_project(project_dir)
    project["alignment"] = alignment_metadata(project_dir, blocks)
    write_project_json(project_dir, project)


def write_alignment_artifact(project_dir: Path, blocks: list[Record]) -> None:
    payload: JsonObject = {
        "schema_version": 1,
        "source_script": "script.txt",
        "source_transcript": "transcripts/narration.json",
        "method": "sequence_matcher_words_v1",
        "blocks": records_json(blocks),
    }
    atomic_write_json(
        project_dir / "alignment" / "script_alignment.json",
        payload,
    )


def alignment_metadata(project_dir: Path, blocks: list[Record]) -> JsonObject:
    script_file = project_dir / "script.txt"
    transcript_file = project_dir / "transcripts" / "narration.json"
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    aligned = [block for block in blocks if block.get("status") == "aligned"]
    warnings = [block for block in blocks if block.get("status") in {"needs_review", "unmatched"}]
    return {
        "script": {
            "path": "alignment/script_alignment.json",
            "source_script": "script.txt",
            "source_transcript": "transcripts/narration.json",
            "method": "sequence_matcher_words_v1",
            "status": "aligned",
            "blocks": len(blocks),
            "aligned_blocks": len(aligned),
            "coverage": 1.0 if aligned else 0.0,
            "needs_review_blocks": sum(1 for block in warnings if block.get("status") == "needs_review"),
            "unmatched_blocks": sum(1 for block in warnings if block.get("status") == "unmatched"),
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(script_file)),
                "transcript_sha256": digest_value(sha256_fingerprint(transcript_file)),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }


def aligned_block(block_id: str, start: float, end: float) -> Record:
    return {
        "id": block_id,
        "status": "aligned",
        "start": start,
        "end": end,
        "text": block_id,
    }


def warning_block(block_id: str, status: str) -> Record:
    return {
        "id": block_id,
        "status": status,
        "start": None,
        "end": None,
        "text": block_id,
    }


def alignment_duration(blocks: list[Record]) -> float:
    ends: list[float] = []
    for block in blocks:
        end = block.get("end")
        if isinstance(end, int | float) and not isinstance(end, bool):
            ends.append(float(end))
    return float(max(ends)) if ends else 1.0


def records_json(records: list[Record]) -> list[JsonValue]:
    output: list[JsonValue] = []
    for record in records:
        output.append(cast(JsonValue, record))
    return output


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project_json(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def assert_chunk_failure(project_dir: Path, *, expected_attempts: int) -> None:
    failure = only_failure(project_dir)
    assert failure["stage"] == "chunks"
    assert failure["scope"] == "chunks:project"
    assert failure["status"] == "active"
    assert failure["attempt_count"] == expected_attempts


def only_failure(project_dir: Path) -> Record:
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    return failures[0]


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def object_dict(data: ProjectJson | Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: ProjectJson | Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    records: list[Record] = []
    for item in value:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records
