from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from app import artifacts
from app.artifacts import (
    atomic_write_json,
    build_pipeline_freshness,
    sha256_fingerprint,
    stat_fingerprint,
    temporary_artifact_path,
)
from app.project import JsonObject, ProjectState, build_initial_project, write_project
from app.status import build_status


def test_verified_pipeline_is_current_and_ready_for_chunks(tmp_path: Path) -> None:
    project_dir = verified_project(tmp_path)
    before = (project_dir / "project.json").read_bytes()

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_chunks"
    assert {name: summary["freshness"][name]["state"] for name in summary["freshness"]} == {
        "raw": "current",
        "audio": "current",
        "transcript": "current",
        "alignment": "current",
    }
    assert (project_dir / "project.json").read_bytes() == before


def test_modified_raw_rolls_pipeline_back_to_probe(tmp_path: Path) -> None:
    project_dir = verified_project(tmp_path)
    (project_dir / "raw.mp4").write_bytes(b"replacement raw video")

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_probe"
    assert summary["freshness"]["raw"] == {
        "state": "stale",
        "reason": "fingerprint_mismatch",
    }
    assert summary["freshness"]["audio"]["state"] == "stale"
    assert summary["freshness"]["transcript"]["state"] == "stale"
    assert summary["freshness"]["alignment"]["state"] == "stale"


@pytest.mark.parametrize("change", ["delete", "modify"])
def test_missing_or_modified_audio_rolls_back_to_audio(tmp_path: Path, change: str) -> None:
    project_dir = verified_project(tmp_path)
    audio_file = project_dir / "audio" / "narration.wav"
    if change == "delete":
        audio_file.unlink()
    else:
        audio_file.write_bytes(b"modified narration")

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_audio"
    assert summary["freshness"]["audio"]["state"] == (
        "missing" if change == "delete" else "stale"
    )
    assert summary["freshness"]["transcript"]["reason"] == "upstream_stale"


@pytest.mark.parametrize("change", ["delete", "modify"])
def test_missing_or_modified_transcript_rolls_back_to_transcription(
    tmp_path: Path,
    change: str,
) -> None:
    project_dir = verified_project(tmp_path)
    transcript_file = project_dir / "transcripts" / "narration.json"
    if change == "delete":
        transcript_file.unlink()
    else:
        transcript_file.write_text("{}\n", encoding="utf-8")

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_transcription"
    assert summary["freshness"]["transcript"]["state"] == (
        "missing" if change == "delete" else "stale"
    )
    assert summary["freshness"]["alignment"]["state"] == "stale"


@pytest.mark.parametrize("change", ["delete", "modify"])
def test_missing_or_modified_alignment_rolls_back_to_alignment(tmp_path: Path, change: str) -> None:
    project_dir = verified_project(tmp_path)
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    if change == "delete":
        alignment_file.unlink()
    else:
        alignment_file.write_text("{}\n", encoding="utf-8")

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_alignment"
    assert summary["freshness"]["alignment"]["state"] == (
        "missing" if change == "delete" else "stale"
    )


def test_legacy_metadata_without_fingerprints_is_unverified(tmp_path: Path) -> None:
    project_dir = verified_project(tmp_path)
    data = load_state(project_dir)
    raw = object_field(object_field(data, "media"), "raw")
    raw.pop("source_fingerprint")
    write_project(project_dir / "project.json", data)

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_probe"
    assert summary["freshness"]["raw"] == {
        "state": "unverified",
        "reason": "fingerprint_missing",
    }


def test_freshness_hashes_only_small_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = verified_project(tmp_path)
    hashed: list[Path] = []
    real_hash = artifacts.sha256_fingerprint

    def record_hash(path: Path) -> JsonObject:
        assert path.suffix not in {".mp4", ".wav"}
        hashed.append(path)
        return real_hash(path)

    monkeypatch.setattr(artifacts, "sha256_fingerprint", record_hash)

    freshness = build_pipeline_freshness(project_dir, load_state(project_dir))

    assert freshness["alignment"]["state"] == "current"
    assert {path.suffix for path in hashed} == {".txt", ".json"}


def test_atomic_json_failure_preserves_target_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"old":true}\n', encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(artifacts.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replacement failure"):
        atomic_write_json(target, {"new": True})

    assert target.read_text(encoding="utf-8") == '{"old":true}\n'
    assert list(tmp_path.glob(".artifact.json.*.tmp")) == []


def test_temporary_artifact_path_is_removed_after_error(tmp_path: Path) -> None:
    target = tmp_path / "narration.wav"
    temporary: Path | None = None

    with pytest.raises(RuntimeError, match="stop"):
        with temporary_artifact_path(target) as path:
            temporary = path
            path.write_bytes(b"partial")
            raise RuntimeError("stop")

    assert temporary is not None
    assert not temporary.exists()


def verified_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    script_file = project_dir / "script.txt"
    raw_file = project_dir / "raw.mp4"
    audio_file = project_dir / "audio" / "narration.wav"
    transcript_file = project_dir / "transcripts" / "narration.json"
    alignment_file = project_dir / "alignment" / "script_alignment.json"

    script_file.write_text("Hello world.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"narration audio")
    atomic_write_json(
        transcript_file,
        {
            "schema_version": 1,
            "text": "Hello world.",
            "segments": [],
        },
    )
    atomic_write_json(alignment_file, {"schema_version": 1, "blocks": []})

    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    state = build_initial_project(project_dir)
    state["media"] = {
        "raw": {
            "path": "raw.mp4",
            "duration_seconds": 12.0,
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
    state["alignment"] = {
        "script": {
            "path": "alignment/script_alignment.json",
            "source_script": "script.txt",
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


def load_state(project_dir: Path) -> ProjectState:
    value: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(ProjectState, value)


def object_field(data: JsonObject | ProjectState, key: str) -> JsonObject:
    value = data.get(key)
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value
