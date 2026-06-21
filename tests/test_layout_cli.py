from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.project import JsonObject, ProjectState, build_initial_project, write_project


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=command_env,
    )


def test_init_from_input_uses_normalized_slug_and_canonical_files(tmp_path: Path) -> None:
    input_dir = tmp_path / "inputs" / "Neha_much"
    input_dir.mkdir(parents=True)
    (input_dir / "script.txt").write_text("Hello world.\n", encoding="utf-8")
    (input_dir / "raw.mp4").write_bytes(b"raw video")

    result = run_cli("init-from-input", input_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["slug"] == "neha-much"
    project_dir = tmp_path / "projects" / "neha-much"
    outputs_dir = tmp_path / "outputs" / "neha-much"
    assert summary["project_dir"] == str(project_dir)
    assert summary["outputs_dir"] == str(outputs_dir)
    assert project_dir.is_dir()
    assert outputs_dir.is_dir()

    project = load_project(project_dir)
    layout = object_dict(project, "layout")
    assert layout["version"] == 1
    assert layout["slug"] == "neha-much"
    assert layout["inputs_root"] == "../../inputs/Neha_much"
    assert layout["outputs_root"] == "../../outputs/neha-much"
    project_info = object_dict(project, "project")
    assert project_info["script"] == "../../inputs/Neha_much/script.txt"
    assert project_info["video"] == "../../inputs/Neha_much/raw.mp4"


def test_init_from_input_accepts_one_noncanonical_script_and_video(tmp_path: Path) -> None:
    input_dir = tmp_path / "inputs" / "My Topic"
    input_dir.mkdir(parents=True)
    (input_dir / "episode.mp4.txt").write_text("Script.\n", encoding="utf-8")
    (input_dir / "VID-001.MP4").write_bytes(b"video")

    result = run_cli("init-from-input", input_dir, "--slug", "my-topic", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["slug"] == "my-topic"
    assert cast(str, summary["script"]).endswith("episode.mp4.txt")
    assert cast(str, summary["video"]).endswith("VID-001.MP4")


def test_init_from_input_rejects_ambiguous_candidates_and_invalid_slug(tmp_path: Path) -> None:
    input_dir = tmp_path / "inputs" / "topic"
    input_dir.mkdir(parents=True)
    (input_dir / "a.txt").write_text("A\n", encoding="utf-8")
    (input_dir / "b.txt").write_text("B\n", encoding="utf-8")
    (input_dir / "raw.mp4").write_bytes(b"video")

    ambiguous = run_cli("init-from-input", input_dir, "--json")
    invalid_slug = run_cli("init-from-input", input_dir, "--slug", "Bad Slug", "--json")

    assert ambiguous.returncode == 1
    assert "multiple .txt scripts" in ambiguous.stderr
    assert invalid_slug.returncode == 1
    assert "Invalid project slug" in invalid_slug.stderr


def test_layout_project_writes_generated_artifacts_to_outputs(tmp_path: Path) -> None:
    input_dir = tmp_path / "inputs" / "episode"
    input_dir.mkdir(parents=True)
    (input_dir / "script.txt").write_text("Hello world.\n", encoding="utf-8")
    (input_dir / "raw.mp4").write_bytes(b"raw video")
    project_dir = tmp_path / "projects" / "episode"
    outputs_dir = tmp_path / "outputs" / "episode"
    assert run_cli("init-from-input", input_dir, "--json").returncode == 0
    project = load_project(project_dir)
    media = object_dict(project, "media")
    media["raw"] = {
        "path": "../../inputs/episode/raw.mp4",
        "source_fingerprint": stat_fingerprint(input_dir / "raw.mp4"),
        "duration_seconds": 12.345,
        "size_bytes": 9,
        "bit_rate": 8000000,
        "video": {"codec": "h264", "width": 1920, "height": 1080, "frame_rate": 30.0},
        "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
        "probed_at": "2026-06-20T00:00:00Z",
    }
    write_project(project_dir / "project.json", cast_project(project))
    ffmpeg = fake_ffmpeg(tmp_path)

    audio = run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg))
    transcript = run_cli("transcribe", project_dir, "--provider", "mock", "--json")
    preview = run_cli("preview", project_dir, "--template", "simple_card", "--params-json", '{"title":"Layout"}', "--json")

    assert audio.returncode == 0, audio.stderr
    assert transcript.returncode == 0, transcript.stderr
    assert preview.returncode == 0, preview.stderr
    assert (outputs_dir / "audio" / "narration.wav").is_file()
    assert (outputs_dir / "transcripts" / "narration.json").is_file()
    assert not (project_dir / "audio" / "narration.wav").exists()
    assert not (project_dir / "transcripts" / "narration.json").exists()
    preview_summary = json_object(preview.stdout)
    assert str(outputs_dir / "previews") in cast(str, preview_summary["output_path"])
    status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert object_dict(status, "layout")["slug"] == "episode"
    audio_status = object_dict(object_dict(status, "media"), "audio")
    assert object_dict(audio_status, "narration")["extracted"] is True


def test_layout_alignment_and_chunks_read_outputs_artifacts(tmp_path: Path) -> None:
    project_dir, outputs_dir = prepared_layout_project(tmp_path)

    aligned = run_cli("align", project_dir, "--json")
    chunked = run_cli("create-chunks", project_dir, "--json")

    assert aligned.returncode == 0, aligned.stderr
    assert chunked.returncode == 0, chunked.stderr
    assert (outputs_dir / "alignment" / "script_alignment.json").is_file()
    assert not (project_dir / "alignment" / "script_alignment.json").exists()
    status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert status["state"] == "in_progress"
    assert object_dict(object_dict(status, "freshness"), "alignment")["state"] == "current"
    review = json_object(run_cli("alignment-review", project_dir, "--json").stdout)
    assert review["aligned_count"] == 1


def test_adopt_layout_moves_existing_generated_dirs_and_preserves_freshness(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    input_dir = tmp_path / "inputs" / "Episode"

    result = run_cli("adopt-layout", project_dir, "--input-dir", input_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["slug"] == "episode"
    assert set(cast(list[str], summary["moved"])) == {"audio", "transcripts", "alignment"}
    outputs_dir = tmp_path / "outputs" / "episode"
    assert (outputs_dir / "audio" / "narration.wav").is_file()
    assert (outputs_dir / "transcripts" / "narration.json").is_file()
    assert (outputs_dir / "alignment" / "script_alignment.json").is_file()
    assert not (project_dir / "audio").exists()
    status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert status["state"] == "ready_for_chunks"
    assert object_dict(object_dict(status, "freshness"), "alignment")["state"] == "current"


def test_adopt_layout_conflict_fails_without_moving_source(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    input_dir = tmp_path / "inputs" / "Episode"
    conflict_dir = tmp_path / "outputs" / "episode" / "audio"
    conflict_dir.mkdir(parents=True)
    (conflict_dir / "existing.wav").write_bytes(b"existing")

    result = run_cli("adopt-layout", project_dir, "--input-dir", input_dir, "--json")

    assert result.returncode == 1
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert "Destination already contains generated artifacts" in cast(list[str], summary["errors"])[0]
    assert (project_dir / "audio" / "narration.wav").is_file()
    assert not (tmp_path / "outputs" / "episode" / "transcripts").exists()


def prepared_layout_project(tmp_path: Path) -> tuple[Path, Path]:
    input_dir = tmp_path / "inputs" / "episode"
    input_dir.mkdir(parents=True)
    (input_dir / "script.txt").write_text("Hello world.\n", encoding="utf-8")
    (input_dir / "raw.mp4").write_bytes(b"raw video")
    assert run_cli("init-from-input", input_dir, "--json").returncode == 0
    project_dir = tmp_path / "projects" / "episode"
    outputs_dir = tmp_path / "outputs" / "episode"
    audio_file = outputs_dir / "audio" / "narration.wav"
    transcript_file = outputs_dir / "transcripts" / "narration.json"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"audio")
    atomic_write_json(
        transcript_file,
        {
            "schema_version": 1,
            "source": "audio/narration.wav",
            "provider": "mock",
            "model": "large-v3",
            "language": None,
            "duration_seconds": 1.0,
            "text": "Hello world.",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Hello world.",
                    "words": [
                        {"start": 0.0, "end": 0.4, "word": "Hello", "probability": 0.99},
                        {"start": 0.5, "end": 0.9, "word": "world.", "probability": 0.99},
                    ],
                }
            ],
            "transcribed_at": "2026-06-20T00:00:00Z",
        },
    )
    project = load_project(project_dir)
    media = object_dict(project, "media")
    raw_fingerprint = stat_fingerprint(input_dir / "raw.mp4")
    audio_fingerprint = stat_fingerprint(audio_file)
    media["raw"] = {
        "path": "../../inputs/episode/raw.mp4",
        "source_fingerprint": raw_fingerprint,
        "duration_seconds": 1.0,
    }
    media["audio"] = {
        "narration": {
            "path": "audio/narration.wav",
            "source": "../../inputs/episode/raw.mp4",
            "source_fingerprint": raw_fingerprint,
            "artifact_fingerprint": audio_fingerprint,
        }
    }
    object_dict(project, "transcript")["narration"] = {
        "path": "transcripts/narration.json",
        "source": "audio/narration.wav",
        "status": "transcribed",
        "source_fingerprint": audio_fingerprint,
        "artifact_fingerprint": sha256_fingerprint(transcript_file),
        "word_count": 2,
        "segments": 1,
    }
    write_project(project_dir / "project.json", cast_project(project))
    return project_dir, outputs_dir


def prepared_legacy_project(tmp_path: Path) -> Path:
    input_dir = tmp_path / "inputs" / "Episode"
    input_dir.mkdir(parents=True)
    script_file = input_dir / "script.txt"
    raw_file = input_dir / "raw.mp4"
    script_file.write_text("Hello world.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")
    project_dir = tmp_path / "projects" / "episode"
    project_dir.mkdir(parents=True)
    audio_file = project_dir / "audio" / "narration.wav"
    transcript_file = project_dir / "transcripts" / "narration.json"
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"audio")
    atomic_write_json(transcript_file, {"schema_version": 1, "text": "Hello world.", "segments": []})
    atomic_write_json(alignment_file, {"schema_version": 1, "blocks": []})
    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    state = build_initial_project(project_dir, script_source=script_file, video_source=raw_file)
    state["media"] = {
        "raw": {
            "path": "../../inputs/Episode/raw.mp4",
            "duration_seconds": 1.0,
            "source_fingerprint": raw_fingerprint,
        },
        "audio": {
            "narration": {
                "path": "audio/narration.wav",
                "source": "../../inputs/Episode/raw.mp4",
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
    state["alignment"] = {
        "script": {
            "path": "alignment/script_alignment.json",
            "source_script": "../../inputs/Episode/script.txt",
            "source_transcript": "transcripts/narration.json",
            "method": "sequence_matcher_words_v1",
            "status": "aligned",
            "blocks": 1,
            "aligned_blocks": 1,
            "needs_review_blocks": 0,
            "unmatched_blocks": 0,
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(script_file)),
                "transcript_sha256": digest_value(transcript_fingerprint),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }
    write_project(project_dir / "project.json", state)
    return project_dir


def fake_ffmpeg(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "output = Path(sys.argv[-1])",
                "output.parent.mkdir(parents=True, exist_ok=True)",
                "output.write_bytes(b'fake wav data')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = tmp_path / "fake_ffmpeg.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return wrapper
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def fake_env(ffmpeg: Path) -> dict[str, str]:
    return {"VISUAL_FORGE_FFMPEG": str(ffmpeg)}


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def object_dict(data: ProjectJson | Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def cast_project(data: ProjectJson) -> ProjectState:
    return cast(ProjectState, data)


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value
