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


def test_plan_visuals_json_creates_chunk_scoped_visuals(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [
            aligned_block("block_001", 0.0, 12.0, "This opening idea deserves a clear visual card."),
            aligned_block("block_002", 12.0, 24.0, "This second point adds important context for the viewer."),
            aligned_block("block_003", 24.0, 36.0, "This final point gives the audience a memorable takeaway."),
        ],
    )
    assert run_cli("create-chunks", project_dir).returncode == 0

    result = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--max-visuals", "2", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["planned_count"] == 2
    assert summary["requires_human_decision"] is False
    visual_ids = record_list_or_strings(summary, "visual_ids")
    assert len(visual_ids) == 2

    project = load_project(project_dir)
    visuals = record_list(project, "visuals")
    assert len(visuals) == 2
    chunk = record_list(project, "chunks")[0]
    assert chunk["visual_mode"] == "visuals"
    assert chunk["status"] == "new"
    for visual in visuals:
        assert visual["planner"] == "auto_v0"
        assert visual["chunk_id"] == "chunk_001"
        assert visual["template_ref"] == "simple_card"
        assert visual["template_id"] == "simple_card"
        assert visual["status"] == "planned"
        assert visual["preview_id"] is None
        assert isinstance(visual["id"], str)
        assert object_dict(visual, "params")["title"]
        assert 0.0 <= float_value(visual, "start") < float_value(visual, "end") <= 36.0
        assert record_list_or_strings(visual, "source_block_ids")
        assert visual["planning_reason"] == "high_signal_script_block"


def test_plan_visuals_rerun_reuses_existing_generated_visuals(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "This reusable point should plan only one visual.")],
    )
    assert run_cli("create-chunks", project_dir).returncode == 0
    first = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")
    second = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json_object(first.stdout)
    second_summary = json_object(second.stdout)
    assert first_summary["visual_ids"] == second_summary["visual_ids"]
    assert second_summary["reused_existing"] is True
    assert len(record_list(load_project(project_dir), "visuals")) == 1


def test_plan_visuals_preserves_manual_visuals_and_records_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "This useful idea would normally generate a card.")],
    )
    assert run_cli("create-chunks", project_dir).returncode == 0
    manual = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--start",
        "2",
        "--end",
        "5",
        "--params-json",
        '{"title":"Manual"}',
    )
    assert manual.returncode == 0, manual.stderr

    result = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 1
    assert "human-created visual" in result.stdout
    project = load_project(project_dir)
    visuals = record_list(project, "visuals")
    assert len(visuals) == 1
    assert "planner" not in visuals[0]
    failure = only_failure(project_dir)
    assert failure["stage"] == "plan_visuals"
    assert failure["scope"] == "chunk:chunk_001"


def test_plan_visuals_force_generated_replaces_auto_visuals_only(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [
            aligned_block("block_001", 0.0, 12.0, "This first idea should become an automatic visual."),
            aligned_block("block_002", 12.0, 24.0, "This second idea should become another automatic visual."),
        ],
    )
    assert run_cli("create-chunks", project_dir).returncode == 0
    assert run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--max-visuals", "1").returncode == 0

    blocked = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--max-visuals", "2", "--json")
    forced = run_cli(
        "plan-visuals",
        project_dir,
        "--chunk",
        "chunk_001",
        "--max-visuals",
        "2",
        "--force-generated",
        "--json",
    )

    assert blocked.returncode == 1
    assert "Use --force-generated" in blocked.stdout
    assert forced.returncode == 0, forced.stderr
    assert json_object(forced.stdout)["planned_count"] == 2
    visuals = record_list(load_project(project_dir), "visuals")
    assert len(visuals) == 2
    assert all(visual["planner"] == "auto_v0" for visual in visuals)


def test_plan_visuals_no_candidates_marks_chunk_without_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, [aligned_block("block_001", 0.0, 12.0, "Hi.")])
    assert run_cli("create-chunks", project_dir).returncode == 0

    result = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["planned_count"] == 0
    assert summary["requires_human_decision"] is True
    project = load_project(project_dir)
    assert record_list(project, "visuals") == []
    chunk = record_list(project, "chunks")[0]
    planning = object_dict(chunk, "visual_planning")
    assert planning["planner"] == "auto_v0"
    assert planning["status"] == "no_candidates"
    assert record_list(project, "failures") == []


def test_plan_visuals_failure_repeated_and_success_resolves(tmp_path: Path) -> None:
    block = aligned_block("block_001", 0.0, 12.0, "This useful idea should recover after stale alignment.")
    project_dir = prepare_project(tmp_path, [block])
    assert run_cli("create-chunks", project_dir).returncode == 0
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    original_alignment = alignment_file.read_bytes()
    alignment_file.write_text('{"changed":true}\n', encoding="utf-8")

    first = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")
    second = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")

    assert first.returncode == 1
    assert second.returncode == 1
    failure = only_failure(project_dir)
    assert failure["stage"] == "plan_visuals"
    assert failure["attempt_count"] == 2

    alignment_file.write_bytes(original_alignment)
    retry = run_cli("plan-visuals", project_dir, "--chunk", "chunk_001", "--json")

    assert retry.returncode == 0, retry.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def prepare_project(tmp_path: Path, blocks: list[Record]) -> Path:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    (project_dir / "script.txt").write_text("Script.\n", encoding="utf-8")
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
            "text": "Script.",
            "segments": [],
        },
    )
    write_alignment_artifact(project_dir, blocks)

    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)

    state: ProjectState = build_initial_project(project_dir)
    state["media"] = {
        "raw": {
            "path": "raw.mp4",
            "duration_seconds": alignment_duration(blocks),
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
    return project_dir


def write_alignment_artifact(project_dir: Path, blocks: list[Record]) -> None:
    payload: JsonObject = {
        "schema_version": 1,
        "source_script": "script.txt",
        "source_transcript": "transcripts/narration.json",
        "method": "sequence_matcher_words_v1",
        "blocks": records_json(blocks),
    }
    atomic_write_json(project_dir / "alignment" / "script_alignment.json", payload)


def alignment_metadata(project_dir: Path, blocks: list[Record]) -> JsonObject:
    script_file = project_dir / "script.txt"
    transcript_file = project_dir / "transcripts" / "narration.json"
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    aligned = [block for block in blocks if block.get("status") == "aligned"]
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
            "needs_review_blocks": 0,
            "unmatched_blocks": 0,
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(script_file)),
                "transcript_sha256": digest_value(sha256_fingerprint(transcript_file)),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }


def aligned_block(block_id: str, start: float, end: float, text: str) -> Record:
    return {
        "id": block_id,
        "status": "aligned",
        "start": start,
        "end": end,
        "text": text,
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


def load_project(project_dir: Path) -> Record:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(Record, raw)


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def object_dict(data: Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    records: list[Record] = []
    for item in value:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def record_list_or_strings(data: Record, key: str) -> list[object]:
    value = data[key]
    assert isinstance(value, list)
    return value


def float_value(data: Record, key: str) -> float:
    value = data[key]
    assert isinstance(value, int | float)
    return float(value)


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value


def only_failure(project_dir: Path) -> Record:
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    return failures[0]
