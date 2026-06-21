from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.project import JsonObject, ProjectState, build_initial_project, write_project
from app.render_freshness import build_visual_plan_fingerprint
from app.timeline import build_chunking_metadata, build_full_raw_timeline, current_chunk_plan_fingerprint


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


def test_next_ready_for_probe_recommends_probe(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    assert run_cli("init", project_dir).returncode == 0
    (project_dir / "script.txt").write_text("Script.\n", encoding="utf-8")
    (project_dir / "raw.mp4").write_bytes(b"raw")

    result = run_cli("next", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["state"] == "ready_for_probe"
    assert summary["human_input_required"] is False
    assert summary["recommended_command"] == ["python", "-m", "app.main", "probe", str(project_dir), "--json"]


def test_next_pipeline_ready_states_return_exact_commands(tmp_path: Path) -> None:
    cases = [
        ("ready_for_audio", "extract-audio"),
        ("ready_for_transcription", "transcribe"),
        ("ready_for_alignment", "align"),
        ("ready_for_chunks", "create-chunks"),
    ]
    for state, command in cases:
        project_dir = prepare_pipeline_project(tmp_path / state, state)

        result = run_cli("next", project_dir, "--json")

        assert result.returncode == 0, result.stderr
        summary = json_object(result.stdout)
        assert summary["state"] == state
        assert summary["human_input_required"] is False
        assert summary["recommended_command"] == ["python", "-m", "app.main", command, str(project_dir), "--json"]


def test_next_failed_project_points_to_failures(tmp_path: Path) -> None:
    project_dir = prepare_pipeline_project(tmp_path / "failed", "ready_for_probe")
    project = load_project(project_dir)
    project["failures"] = [
        {
            "id": "failure_0001",
            "stage": "probe",
            "scope": "media:raw.mp4",
            "status": "active",
            "errors": ["failed"],
        }
    ]
    write_project(project_dir / "project.json", cast(ProjectState, project))

    result = run_cli("next", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["state"] == "failed"
    assert summary["recommended_command"] == ["python", "-m", "app.main", "failures", str(project_dir), "--json"]


def test_next_undecided_chunk_recommends_codex_planning_context(tmp_path: Path) -> None:
    project_dir = prepare_chunk_project(tmp_path / "undecided", chunk_status="new", with_visual=False)

    result = run_cli("next", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["state"] == "in_progress"
    assert summary["chunk_id"] == "chunk_001"
    assert summary["human_input_required"] is False
    assert summary["recommended_command"] == [
        "python",
        "-m",
        "app.main",
        "planning-context",
        str(project_dir),
        "--chunk",
        "chunk_001",
        "--json",
    ]
    assert "Codex visual planning" in string_value(summary, "recommended_action")


def test_next_no_candidate_chunk_requires_human_input(tmp_path: Path) -> None:
    project_dir = prepare_chunk_project(tmp_path / "no-candidates", chunk_status="new", with_visual=False)
    project = load_project(project_dir)
    chunks = project["chunks"]
    assert isinstance(chunks, list)
    chunk = chunks[0]
    assert isinstance(chunk, dict)
    chunk["visual_planning"] = {
        "planner": "auto_v0",
        "status": "no_candidates",
        "planned_at": "2026-06-20T00:00:00Z",
    }
    write_project(project_dir / "project.json", cast(ProjectState, project))

    result = run_cli("next", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["state"] == "in_progress"
    assert summary["chunk_id"] == "chunk_001"
    assert summary["human_input_required"] is True
    assert summary["recommended_command"] == []
    assert "manual visual" in string_value(summary, "recommended_action")


def test_next_visual_chunk_recommends_preview_then_render(tmp_path: Path) -> None:
    new_project = prepare_chunk_project(tmp_path / "new-visual", chunk_status="new", with_visual=True)
    previewed_project = prepare_chunk_project(tmp_path / "previewed", chunk_status="previewed", with_visual=True)

    preview = json_object(run_cli("next", new_project, "--json").stdout)
    render = json_object(run_cli("next", previewed_project, "--json").stdout)

    assert preview["recommended_command"] == [
        "python",
        "-m",
        "app.main",
        "preview",
        str(new_project),
        "--chunk",
        "chunk_001",
        "--json",
    ]
    assert render["recommended_command"] == [
        "python",
        "-m",
        "app.main",
        "render-chunk",
        str(previewed_project),
        "chunk_001",
        "--json",
    ]


def test_next_complete_project_has_no_mutating_command(tmp_path: Path) -> None:
    project_dir = prepare_complete_project(tmp_path / "complete")

    human = run_cli("next", project_dir)
    machine = run_cli("next", project_dir, "--json")

    assert human.returncode == 0, human.stderr
    assert "State: complete" in human.stdout
    assert "Command: none" in human.stdout
    assert machine.returncode == 0, machine.stderr
    summary = json_object(machine.stdout)
    assert summary["state"] == "complete"
    assert summary["action_kind"] == "none"
    assert summary["recommended_command"] == []
    assert summary["recommended_action"] == "Review final video."


def prepare_pipeline_project(project_dir: Path, state: str) -> Path:
    project_dir.mkdir(parents=True)
    script_file = project_dir / "script.txt"
    raw_file = project_dir / "raw.mp4"
    script_file.write_text("Script.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")
    project = build_initial_project(project_dir)
    if state in {"ready_for_audio", "ready_for_transcription", "ready_for_alignment", "ready_for_chunks"}:
        raw_fingerprint = stat_fingerprint(raw_file)
        project["media"] = {
            "raw": {
                "path": "raw.mp4",
                "duration_seconds": 10.0,
                "source_fingerprint": raw_fingerprint,
            }
        }
    if state in {"ready_for_transcription", "ready_for_alignment", "ready_for_chunks"}:
        audio_file = project_dir / "audio" / "narration.wav"
        audio_file.parent.mkdir()
        audio_file.write_bytes(b"audio")
        media = object_dict(project, "media")
        media["audio"] = {
            "narration": {
                "path": "audio/narration.wav",
                "source_fingerprint": object_dict(media, "raw")["source_fingerprint"],
                "artifact_fingerprint": stat_fingerprint(audio_file),
            }
        }
    if state in {"ready_for_alignment", "ready_for_chunks"}:
        transcript_file = project_dir / "transcripts" / "narration.json"
        atomic_write_json(transcript_file, {"schema_version": 1, "text": "Script.", "segments": []})
        audio = object_dict(object_dict(project, "media"), "audio")
        audio_artifact = cast(JsonObject, object_dict(audio, "narration")["artifact_fingerprint"])
        project["transcript"] = cast(JsonObject, {
            "narration": {
                "path": "transcripts/narration.json",
                "status": "transcribed",
                "source_fingerprint": audio_artifact,
                "artifact_fingerprint": sha256_fingerprint(transcript_file),
            }
        })
    if state == "ready_for_chunks":
        write_alignment(project_dir, project)
    write_project(project_dir / "project.json", project)
    return project_dir


def prepare_chunk_project(project_dir: Path, *, chunk_status: str, with_visual: bool) -> Path:
    project_dir = prepare_pipeline_project(project_dir, "ready_for_chunks")
    project = cast(ProjectState, load_project(project_dir))
    timeline = build_full_raw_timeline(project_dir, project)
    project["chunks"] = [
        {
            "id": "chunk_001",
            "start": 0.0,
            "end": 10.0,
            "status": chunk_status,
            "visual_mode": "visuals" if with_visual else "undecided",
            "alignment_block_ids": ["block_001"],
            "warning_block_ids": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
    ]
    if with_visual:
        project["visuals"] = [
            {
                "id": "visual_001",
                "chunk_id": "chunk_001",
                "template_ref": "simple_card",
                "template_id": "simple_card",
                "params": {"title": "Key idea"},
                "start": 2.0,
                "end": 4.0,
                "status": "previewed" if chunk_status == "previewed" else "planned",
                "preview_id": "preview_001" if chunk_status == "previewed" else None,
            }
        ]
    project["timeline"] = timeline
    project["chunking"] = build_chunking_metadata(
        project,
        timeline,
        project["chunks"],
        {"target_seconds": 180.0, "min_seconds": 90.0, "max_seconds": 240.0},
    )
    write_project(project_dir / "project.json", project)
    return project_dir


def prepare_complete_project(project_dir: Path) -> Path:
    project_dir = prepare_chunk_project(project_dir, chunk_status="previewed", with_visual=True)
    project = cast(ProjectState, load_project(project_dir))
    preview_file = project_dir / "previews" / "preview_001.png"
    preview_file.parent.mkdir(exist_ok=True)
    preview_file.write_bytes(b"preview")
    chunk_file = project_dir / "renders" / "chunks" / "chunk_001.mp4"
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_bytes(b"chunk")
    final_file = project_dir / "final.mp4"
    final_file.write_bytes(b"final")
    report_file = project_dir / "verification" / "final.json"
    template_file = REPO_ROOT / "templates" / "simple_card.py"
    project["previews"] = [
        {
            "id": "preview_001",
            "template_ref": "simple_card",
            "template_id": "simple_card",
            "params": {"title": "Key idea"},
            "output": "previews/preview_001.png",
            "status": "rendered",
            "template_version": "1.0.0",
            "template_fingerprint": sha256_fingerprint(template_file),
            "artifact_fingerprint": sha256_fingerprint(preview_file),
        }
    ]
    project["chunks"][0]["status"] = "rendered"
    visual_plan = build_visual_plan_fingerprint(project, "chunk_001")
    chunk_plan = current_chunk_plan_fingerprint(project)
    assert visual_plan is not None
    assert chunk_plan is not None
    raw_fingerprint = cast(JsonObject, object_dict(object_dict(project, "media"), "raw")["source_fingerprint"])
    chunk_fingerprint = stat_fingerprint(chunk_file)
    project["renders"] = cast(JsonObject, {
        "chunks": {
            "chunk_001": {
                "path": "renders/chunks/chunk_001.mp4",
                "chunk_id": "chunk_001",
                "source_fingerprint": raw_fingerprint,
                "duration_seconds": 10.0,
                "status": "rendered",
                "visual_plan_fingerprint": visual_plan,
                "chunk_plan_fingerprint": chunk_plan,
                "preview_fingerprints": {"preview_001": sha256_fingerprint(preview_file)},
                "artifact_fingerprint": chunk_fingerprint,
            }
        },
        "final": {
            "path": "final.mp4",
            "status": "rendered",
            "chunk_ids": ["chunk_001"],
            "chunk_paths": ["renders/chunks/chunk_001.mp4"],
            "duration_seconds": 10.0,
            "source_fingerprints": {"chunk_001": chunk_fingerprint},
            "timeline_fingerprint": object_dict(project, "chunking")["timeline_fingerprint"],
            "chunk_plan_fingerprint": chunk_plan,
            "artifact_fingerprint": stat_fingerprint(final_file),
        },
    })
    atomic_write_json(
        report_file,
        {
            "schema_version": 1,
            "source": "final.mp4",
            "passed": True,
            "errors": [],
            "warnings": [],
        },
    )
    project["verification"] = {
        "final": {
            "path": "verification/final.json",
            "source": "final.mp4",
            "status": "passed",
            "error_count": 0,
            "warning_count": 0,
            "verified_at": "2026-06-20T00:00:00Z",
            "source_fingerprint": stat_fingerprint(final_file),
            "artifact_fingerprint": sha256_fingerprint(report_file),
        }
    }
    write_project(project_dir / "project.json", project)
    return project_dir


def write_alignment(project_dir: Path, project: ProjectState) -> None:
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    atomic_write_json(
        alignment_file,
        {
            "schema_version": 1,
            "blocks": [
                {
                    "id": "block_001",
                    "status": "aligned",
                    "start": 0.0,
                    "end": 10.0,
                    "text": "Script.",
                }
            ],
        },
    )
    transcript = object_dict(project, "transcript")
    transcript_artifact = cast(JsonObject, object_dict(transcript, "narration")["artifact_fingerprint"])
    project["alignment"] = cast(JsonObject, {
        "script": {
            "path": "alignment/script_alignment.json",
            "status": "aligned",
            "blocks": 1,
            "aligned_blocks": 1,
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(project_dir / "script.txt")),
                "transcript_sha256": digest_value(transcript_artifact),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    })


def load_project(project_dir: Path) -> Record:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(Record, raw)


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def object_dict(data: Record | ProjectState, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def string_value(data: Record, key: str) -> str:
    value = data[key]
    assert isinstance(value, str)
    return value


def digest_value(value: object) -> str:
    assert isinstance(value, dict)
    digest = value.get("sha256")
    assert isinstance(digest, str)
    return digest
