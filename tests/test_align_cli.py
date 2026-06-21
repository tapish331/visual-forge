from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

import pytest

import app.align as align_module
from app.artifacts import sha256_fingerprint, stat_fingerprint
from app.align import align_project

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


def test_align_json_writes_artifact_and_project_metadata(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["method"] == "sequence_matcher_words_v1"
    assert summary["output_path"] == str(project_dir / "alignment" / "script_alignment.json")
    metadata = object_dict(summary, "metadata")
    assert metadata["status"] == "aligned"
    assert metadata["blocks"] == 2
    assert metadata["aligned_blocks"] == 2
    assert metadata["coverage"] == 1.0
    assert metadata["needs_review_blocks"] == 0
    assert metadata["unmatched_blocks"] == 0
    assert object_dict(metadata, "artifact_fingerprint")["kind"] == "sha256_v1"
    fingerprints = object_dict(metadata, "source_fingerprints")
    assert len(cast(str, fingerprints["script_sha256"])) == 64
    assert len(cast(str, fingerprints["transcript_sha256"])) == 64

    artifact = json_object((project_dir / "alignment" / "script_alignment.json").read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["source_script"] == "script.txt"
    assert artifact["source_transcript"] == "transcripts/narration.json"
    assert artifact["script_token_count"] == 7
    assert artifact["transcript_word_count"] == 7
    assert artifact["matched_script_tokens"] == 7
    artifact_fingerprints = object_dict(artifact, "source_fingerprints")
    assert artifact_fingerprints == fingerprints
    blocks = record_list(artifact, "blocks")
    assert len(blocks) == 2
    assert blocks[0]["id"] == "block_001"
    assert blocks[0]["start"] == 0.0
    assert blocks[0]["end"] == 0.9
    assert blocks[0]["status"] == "aligned"
    assert blocks[1]["start"] == 1.0
    assert blocks[1]["end"] == 2.9

    project = load_project(project_dir)
    alignment = object_dict(project, "alignment")
    script_alignment = object_dict(alignment, "script")
    assert script_alignment["path"] == "alignment/script_alignment.json"
    assert script_alignment["source_script"] == "script.txt"
    assert script_alignment["status"] == "aligned"


def test_align_human_output_is_short_and_actionable(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)

    result = run_cli("align", project_dir)

    assert result.returncode == 0, result.stderr
    assert "Alignment:" in result.stdout
    assert "Script: script.txt" in result.stdout
    assert "Transcript: transcripts/narration.json" in result.stdout
    assert "Status: aligned" in result.stdout
    assert "Blocks: 2" in result.stdout
    assert "Coverage: 1.0" in result.stdout
    assert "Needs review: 0" in result.stdout


def test_unmatched_block_has_no_canonical_or_candidate_timing(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, script_text="Hello world.\n\nCompletely absent phrase.\n")

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    metadata = object_dict(json_object(result.stdout), "metadata")
    assert metadata["needs_review_blocks"] == 0
    assert metadata["unmatched_blocks"] == 1
    artifact = json_object((project_dir / "alignment" / "script_alignment.json").read_text(encoding="utf-8"))
    blocks = record_list(artifact, "blocks")
    assert blocks[0]["status"] == "aligned"
    assert blocks[1]["status"] == "unmatched"
    assert blocks[1]["start"] is None
    assert blocks[1]["end"] is None
    assert blocks[1]["candidate_start"] is None
    assert blocks[1]["candidate_end"] is None


def test_moderate_partial_match_uses_candidate_timing_only(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, script_text="Hello world.\n\nThis missing narration extra unknown.\n")

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    metadata = object_dict(json_object(result.stdout), "metadata")
    assert metadata["needs_review_blocks"] == 1
    assert metadata["unmatched_blocks"] == 0
    artifact = json_object((project_dir / "alignment" / "script_alignment.json").read_text(encoding="utf-8"))
    blocks = record_list(artifact, "blocks")
    assert blocks[1]["status"] == "needs_review"
    assert blocks[1]["confidence"] == 0.4
    assert blocks[1]["start"] is None
    assert blocks[1]["end"] is None
    assert blocks[1]["candidate_start"] == 1.0
    assert blocks[1]["candidate_end"] == 2.5


def test_annotation_header_is_metadata_and_not_alignment_content(tmp_path: Path) -> None:
    script = "[00:00:00.25] - Speaker 1\nHello world. This is the narration script.\n"
    project_dir = prepare_project(tmp_path, script_text=script)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    metadata = object_dict(json_object(result.stdout), "metadata")
    assert metadata["coverage"] == 1.0
    assert metadata["needs_review_blocks"] == 0
    artifact = json_object((project_dir / "alignment" / "script_alignment.json").read_text(encoding="utf-8"))
    assert artifact["script_token_count"] == 7
    blocks = record_list(artifact, "blocks")
    assert len(blocks) == 2
    assert blocks[0]["text"] == "Hello world."
    assert blocks[1]["text"] == "This is the narration script."
    for block in blocks:
        assert block["status"] == "aligned"
        assert block["speaker"] == "Speaker 1"
        assert block["section_timestamp_hint_seconds"] == 0.25


def test_representative_headers_align_while_unspoken_sentence_stays_unmatched(tmp_path: Path) -> None:
    script = (
        "[00:00:01.15] - Speaker 1\n"
        "Hi, this is Naha Mach.\n\n"
        "[00:01:11.07] - Speaker 1\n"
        "Give me one second. Yeah, what have Why done?\n"
    )
    project_dir = prepare_project(tmp_path, script_text=script)
    write_custom_transcript(
        project_dir,
        ["Hi", "this", "is", "Neha", "Much", "Give", "me", "one", "second", "Where", "do", "I", "go"],
    )

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    artifact = json_object((project_dir / "alignment" / "script_alignment.json").read_text(encoding="utf-8"))
    blocks = record_list(artifact, "blocks")
    assert blocks[0]["status"] == "aligned"
    assert blocks[0]["confidence"] == 0.6
    assert blocks[0]["section_timestamp_hint_seconds"] == 1.15
    assert blocks[1]["status"] == "aligned"
    assert blocks[1]["confidence"] == 1.0
    assert blocks[1]["section_timestamp_hint_seconds"] == 71.07
    assert blocks[2]["status"] == "unmatched"
    assert blocks[2]["start"] is None
    assert blocks[2]["end"] is None


def test_existing_project_without_alignment_is_normalized_on_success(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    data = load_project(project_dir)
    data.pop("alignment", None)
    write_project(project_dir, data)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    assert "script" in object_dict(load_project(project_dir), "alignment")


def test_missing_script_records_alignment_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    (project_dir / "script.txt").unlink()

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    summary = json_object(result.stdout)
    assert "Missing narration script: script.txt" in string_list(summary, "errors")
    assert_alignment_failure(project_dir)


def test_missing_transcript_metadata_records_alignment_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, with_metadata=False)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    assert "Missing transcript.narration metadata. Run transcribe before align." in string_list(
        json_object(result.stdout), "errors"
    )
    assert_alignment_failure(project_dir)


def test_missing_transcript_artifact_records_alignment_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, with_artifact=False)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    assert "Missing narration transcript file: transcripts/narration.json" in string_list(
        json_object(result.stdout), "errors"
    )
    assert_alignment_failure(project_dir)


def test_transcript_without_word_timestamps_records_alignment_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, with_words=False)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    assert "Narration transcript does not contain usable word timestamps." in string_list(
        json_object(result.stdout), "errors"
    )
    assert_alignment_failure(project_dir)


def test_invalid_transcript_json_records_alignment_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    (project_dir / "transcripts" / "narration.json").write_text("{invalid", encoding="utf-8")
    refresh_transcript_fingerprints(project_dir)

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    assert "Invalid JSON in narration transcript" in string_list(json_object(result.stdout), "errors")[0]
    assert_alignment_failure(project_dir)


def test_empty_script_records_alignment_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, script_text=" \n\n")

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    assert "Narration script is empty." in string_list(json_object(result.stdout), "errors")
    assert_alignment_failure(project_dir)


def test_no_usable_alignment_records_failure(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, script_text="Unrelated vocabulary only.\n")

    result = run_cli("align", project_dir, "--json")

    assert result.returncode == 1
    assert "No script words could be aligned to the timestamped transcript." in string_list(
        json_object(result.stdout), "errors"
    )
    assert_alignment_failure(project_dir)


def test_alignment_write_failure_preserves_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = prepare_project(tmp_path)
    output_file = project_dir / "alignment" / "script_alignment.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text('{"previous":true}\n', encoding="utf-8")

    def fail_write(_path: Path, _payload: Record) -> None:
        raise OSError("simulated alignment replacement failure")

    monkeypatch.setattr(align_module, "atomic_write_json", fail_write)

    result = align_project(project_dir)

    assert result["success"] is False
    assert "simulated alignment replacement failure" in result["errors"][0]
    assert output_file.read_text(encoding="utf-8") == '{"previous":true}\n'


def test_repeated_failure_increments_attempt_count_and_retry_resolves_it(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, with_artifact=False)

    first = run_cli("align", project_dir, "--json")
    second = run_cli("align", project_dir, "--json")

    assert first.returncode == 1
    assert second.returncode == 1
    failure = only_failure(project_dir)
    assert failure["attempt_count"] == 2
    assert failure["status"] == "active"

    write_transcript_artifact(project_dir, with_words=True)
    refresh_transcript_fingerprints(project_dir)
    corrected = run_cli("align", project_dir, "--json")

    assert corrected.returncode == 0, corrected.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def test_status_moves_to_ready_for_chunks_after_alignment(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    assert run_cli("align", project_dir, "--json").returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "ready_for_chunks"
    assert status["next_action"] == "Create resumable video chunks."
    alignment = object_dict(status, "alignment")
    script_alignment = object_dict(alignment, "script")
    assert script_alignment["aligned"] is True
    assert script_alignment["stale"] is False
    assert script_alignment["path"] == "alignment/script_alignment.json"
    assert script_alignment["coverage"] == 1.0
    assert script_alignment["aligned_blocks"] == 2
    assert script_alignment["needs_review_blocks"] == 0
    assert script_alignment["unmatched_blocks"] == 0

    human_result = run_cli("status", project_dir)
    assert human_result.returncode == 0, human_result.stderr
    assert "Alignment:" in human_result.stdout
    assert "script_alignment.json (current)" in human_result.stdout


def test_script_edit_marks_alignment_stale_and_rerun_clears_it(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    assert run_cli("align", project_dir, "--json").returncode == 0
    script_file = project_dir / "script.txt"
    script_file.write_text(script_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    stale_result = run_cli("status", project_dir, "--json")

    assert stale_result.returncode == 0, stale_result.stderr
    stale_status = json_object(stale_result.stdout)
    assert stale_status["state"] == "ready_for_alignment"
    stale_alignment = object_dict(object_dict(stale_status, "alignment"), "script")
    assert stale_alignment["aligned"] is False
    assert stale_alignment["stale"] is True

    rerun = run_cli("align", project_dir, "--json")
    assert rerun.returncode == 0, rerun.stderr
    current_status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert current_status["state"] == "ready_for_chunks"
    assert object_dict(object_dict(current_status, "alignment"), "script")["stale"] is False


def test_transcript_edit_marks_alignment_stale(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    assert run_cli("align", project_dir, "--json").returncode == 0
    transcript_file = project_dir / "transcripts" / "narration.json"
    transcript_file.write_text(transcript_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "ready_for_transcription"
    alignment = object_dict(object_dict(status, "alignment"), "script")
    assert alignment["stale"] is True
    assert alignment["aligned_blocks"] == 2
    assert alignment["needs_review_blocks"] == 0
    assert alignment["unmatched_blocks"] == 0


def test_legacy_alignment_without_fingerprints_is_stale(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)
    assert run_cli("align", project_dir, "--json").returncode == 0
    project = load_project(project_dir)
    script_alignment = object_dict(object_dict(project, "alignment"), "script")
    script_alignment.pop("source_fingerprints", None)
    write_project(project_dir, project)

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "ready_for_alignment"
    alignment = object_dict(object_dict(status, "alignment"), "script")
    assert alignment["stale"] is True
    assert alignment["aligned_blocks"] == 2
    assert alignment["needs_review_blocks"] == 0
    assert alignment["unmatched_blocks"] == 0


def test_alignment_review_json_lists_only_warning_items_without_mutating_project(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path, script_text="Hello world.\n\nCompletely absent phrase.\n")
    assert run_cli("align", project_dir, "--json").returncode == 0
    project_before = (project_dir / "project.json").read_text(encoding="utf-8")

    result = run_cli("alignment-review", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["stale"] is False
    assert summary["aligned_count"] == 1
    assert summary["needs_review_count"] == 0
    assert summary["unmatched_count"] == 1
    items = record_list(summary, "items")
    assert len(items) == 1
    assert items[0]["id"] == "block_002"
    assert items[0]["status"] == "unmatched"
    assert items[0]["start"] is None
    assert items[0]["candidate_start"] is None
    assert (project_dir / "project.json").read_text(encoding="utf-8") == project_before

    status_result = run_cli("status", project_dir, "--json")
    assert status_result.returncode == 0, status_result.stderr
    status = json_object(status_result.stdout)
    assert status["state"] == "ready_for_chunks"
    status_alignment = object_dict(object_dict(status, "alignment"), "script")
    assert status_alignment["unmatched_blocks"] == 1
    assert status_alignment["stale"] is False

    human_status = run_cli("status", project_dir)
    assert human_status.returncode == 0, human_status.stderr
    assert "Alignment warnings: 1" in human_status.stdout


def test_alignment_review_human_output_includes_metadata_and_candidate_timing(tmp_path: Path) -> None:
    script = "[00:00:00.25] - Narrator\nHello world. This missing narration extra unknown.\n"
    project_dir = prepare_project(tmp_path, script_text=script)
    assert run_cli("align", project_dir, "--json").returncode == 0

    result = run_cli("alignment-review", project_dir)

    assert result.returncode == 0, result.stderr
    assert "Stale: no" in result.stdout
    assert "Needs review: 1" in result.stdout
    assert "block_002 [needs_review] confidence=0.4" in result.stdout
    assert "speaker=Narrator, timestamp_hint=0.25" in result.stdout
    assert "candidate=1.0-2.5" in result.stdout


def test_alignment_review_missing_or_invalid_artifact_returns_nonzero(tmp_path: Path) -> None:
    project_dir = prepare_project(tmp_path)

    missing = run_cli("alignment-review", project_dir, "--json")

    assert missing.returncode == 1
    assert "error: Missing alignment artifact" in missing.stderr

    alignment_dir = project_dir / "alignment"
    alignment_dir.mkdir()
    (alignment_dir / "script_alignment.json").write_text("{invalid", encoding="utf-8")
    invalid = run_cli("alignment-review", project_dir, "--json")
    assert invalid.returncode == 1
    assert "error: Invalid JSON" in invalid.stderr


def prepare_project(
    tmp_path: Path,
    *,
    script_text: str = "Hello world.\n\nThis is the narration script.\n",
    with_metadata: bool = True,
    with_artifact: bool = True,
    with_words: bool = True,
) -> Path:
    project_dir = tmp_path / "my-video"
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr
    (project_dir / "script.txt").write_text(script_text, encoding="utf-8")
    (project_dir / "raw.mp4").write_bytes(b"fake mp4")
    audio_file = project_dir / "audio" / "narration.wav"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"fake wav")

    if with_metadata:
        data = load_project(project_dir)
        media = object_dict(data, "media")
        media["raw"] = {
            "path": "raw.mp4",
            "source_fingerprint": stat_fingerprint(project_dir / "raw.mp4"),
            "duration_seconds": 3.0,
        }
        media["audio"] = {
            "narration": {
                "path": "audio/narration.wav",
                "source": "raw.mp4",
                "source_fingerprint": stat_fingerprint(project_dir / "raw.mp4"),
                "artifact_fingerprint": stat_fingerprint(audio_file),
            }
        }
        transcript = object_dict(data, "transcript")
        transcript["narration"] = {
            "path": "transcripts/narration.json",
            "source": "audio/narration.wav",
            "provider": "faster-whisper",
            "model": "large-v3",
            "status": "transcribed",
            "duration_seconds": 3.0,
            "text_length": 41,
            "segments": 1,
            "word_count": 7 if with_words else 0,
            "transcribed_at": "2026-06-20T00:00:00Z",
        }
        write_project(project_dir, data)
    if with_artifact:
        write_transcript_artifact(project_dir, with_words=with_words)
        if with_metadata:
            refresh_transcript_fingerprints(project_dir)
    return project_dir


def write_transcript_artifact(project_dir: Path, *, with_words: bool) -> None:
    transcript_dir = project_dir / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    words = [
        {"start": 0.0, "end": 0.4, "word": "Hello", "probability": 0.99},
        {"start": 0.5, "end": 0.9, "word": "world.", "probability": 0.99},
        {"start": 1.0, "end": 1.3, "word": "This", "probability": 0.99},
        {"start": 1.4, "end": 1.6, "word": "is", "probability": 0.99},
        {"start": 1.7, "end": 1.9, "word": "the", "probability": 0.99},
        {"start": 2.0, "end": 2.5, "word": "narration", "probability": 0.99},
        {"start": 2.6, "end": 2.9, "word": "script.", "probability": 0.99},
    ]
    segment: Record = {
        "id": 0,
        "start": 0.0,
        "end": 3.0,
        "text": "Hello world. This is the narration script.",
    }
    if with_words:
        segment["words"] = words
    artifact: Record = {
        "schema_version": 1,
        "source": "audio/narration.wav",
        "provider": "faster-whisper",
        "model": "large-v3",
        "language": "en",
        "duration_seconds": 3.0,
        "text": "Hello world. This is the narration script.",
        "segments": [segment],
        "transcribed_at": "2026-06-20T00:00:00Z",
    }
    (transcript_dir / "narration.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")


def write_custom_transcript(project_dir: Path, word_values: list[str]) -> None:
    words: list[Record] = []
    for index, word in enumerate(word_values):
        start = round(index * 0.5, 2)
        words.append({"start": start, "end": round(start + 0.4, 2), "word": word, "probability": 0.99})
    transcript_dir = project_dir / "transcripts"
    artifact: Record = {
        "schema_version": 1,
        "source": "audio/narration.wav",
        "provider": "faster-whisper",
        "model": "large-v3",
        "language": "en",
        "duration_seconds": len(word_values) * 0.5,
        "text": " ".join(word_values),
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": len(word_values) * 0.5,
                "text": " ".join(word_values),
                "words": words,
            }
        ],
        "transcribed_at": "2026-06-20T00:00:00Z",
    }
    (transcript_dir / "narration.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    project = load_project(project_dir)
    narration = object_dict(object_dict(project, "transcript"), "narration")
    narration["duration_seconds"] = len(word_values) * 0.5
    narration["text_length"] = len(cast(str, artifact["text"]))
    narration["word_count"] = len(word_values)
    narration["artifact_fingerprint"] = sha256_fingerprint(transcript_dir / "narration.json")
    write_project(project_dir, project)


def refresh_transcript_fingerprints(project_dir: Path) -> None:
    project = load_project(project_dir)
    media = object_dict(project, "media")
    audio = object_dict(object_dict(media, "audio"), "narration")
    narration = object_dict(object_dict(project, "transcript"), "narration")
    narration["source_fingerprint"] = audio["artifact_fingerprint"]
    narration["artifact_fingerprint"] = sha256_fingerprint(
        project_dir / "transcripts" / "narration.json"
    )
    write_project(project_dir, project)


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def assert_alignment_failure(project_dir: Path) -> None:
    failure = only_failure(project_dir)
    assert failure["stage"] == "align"
    assert failure["scope"] == "alignment:alignment/script_alignment.json"
    assert failure["status"] == "active"
    assert failure["source_script"] == "script.txt"
    assert failure["source_transcript"] == "transcripts/narration.json"
    assert failure["method"] == "sequence_matcher_words_v1"


def only_failure(project_dir: Path) -> Record:
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    return failures[0]


def json_object(text: str) -> Record:
    raw: object = json.loads(text)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def object_dict(data: Record, key: str) -> Record:
    value = data.get(key)
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: Record, key: str) -> list[Record]:
    value = data.get(key)
    assert isinstance(value, list)
    records: list[Record] = []
    for item in value:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def string_list(data: Record, key: str) -> list[str]:
    value = data.get(key)
    assert isinstance(value, list)
    strings: list[str] = []
    for item in value:
        assert isinstance(item, str)
        strings.append(item)
    return strings
