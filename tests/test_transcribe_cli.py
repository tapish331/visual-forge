from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias, cast

import pytest

import app.transcribe as transcribe_module
from app.artifacts import stat_fingerprint
from app.transcribe import transcribe_project


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


def test_default_transcribe_uses_faster_whisper_and_writes_transcript(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    fake_package = fake_faster_whisper_package(tmp_path)
    capture_file = tmp_path / "capture.json"

    result = run_cli("transcribe", project_dir, "--json", env=fake_env(fake_package, capture_file=capture_file))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["provider"] == "faster-whisper"
    assert summary["model"] == "large-v3"
    assert summary["device"] == "auto"
    assert summary["compute_type"] == "auto"

    artifact = json_object((project_dir / "transcripts" / "narration.json").read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["source"] == "audio/narration.wav"
    assert artifact["provider"] == "faster-whisper"
    assert artifact["model"] == "large-v3"
    assert artifact["device"] == "auto"
    assert artifact["compute_type"] == "auto"
    assert artifact["language"] is None
    assert artifact["duration_seconds"] == 12.345
    assert artifact["text"] == "Hello world"
    segments = record_list(artifact, "segments")
    assert len(segments) == 1
    words = record_list(segments[0], "words")
    assert len(words) == 2
    assert words[0]["word"] == "Hello"
    assert words[0]["probability"] == 0.98

    metadata = object_dict(summary, "metadata")
    assert metadata["path"] == "transcripts/narration.json"
    assert metadata["provider"] == "faster-whisper"
    assert metadata["model"] == "large-v3"
    assert metadata["device"] == "auto"
    assert metadata["compute_type"] == "auto"
    assert metadata["segments"] == 1
    assert metadata["word_count"] == 2
    assert object_dict(metadata, "source_fingerprint")["kind"] == "stat_v1"
    assert object_dict(metadata, "artifact_fingerprint")["kind"] == "sha256_v1"

    capture = json_object(capture_file.read_text(encoding="utf-8"))
    assert capture["model"] == "large-v3"
    assert capture["device"] == "auto"
    assert capture["compute_type"] == "default"
    assert capture["download_root"] == "models/faster-whisper"
    assert capture["word_timestamps"] is True
    assert capture["vad_filter"] is True
    assert capture["beam_size"] == 5


def test_transcribe_faster_whisper_accepts_explicit_controls(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    fake_package = fake_faster_whisper_package(tmp_path)
    capture_file = tmp_path / "capture.json"

    result = run_cli(
        "transcribe",
        project_dir,
        "--provider",
        "faster-whisper",
        "--model",
        "large-v3-turbo",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        "--language",
        "en",
        "--json",
        env=fake_env(fake_package, capture_file=capture_file),
    )

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["model"] == "large-v3-turbo"
    assert summary["device"] == "cpu"
    assert summary["compute_type"] == "int8"
    artifact = json_object((project_dir / "transcripts" / "narration.json").read_text(encoding="utf-8"))
    assert artifact["language"] == "en"
    capture = json_object(capture_file.read_text(encoding="utf-8"))
    assert capture["model"] == "large-v3-turbo"
    assert capture["device"] == "cpu"
    assert capture["compute_type"] == "int8"
    assert capture["language"] == "en"


def test_transcribe_mock_json_still_writes_transcript_and_project_metadata(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)

    result = run_cli("transcribe", project_dir, "--provider", "mock", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["source_path"] == "audio/narration.wav"
    assert summary["output_path"] == str(project_dir / "transcripts" / "narration.json")
    assert summary["provider"] == "mock"
    assert summary["model"] == "large-v3"
    assert summary["device"] == "auto"
    assert summary["compute_type"] == "auto"

    artifact = json_object((project_dir / "transcripts" / "narration.json").read_text(encoding="utf-8"))
    assert artifact["provider"] == "mock"
    assert artifact["model"] == "large-v3"
    assert artifact["text"] == "Hello world This is the narration script."
    segments = record_list(artifact, "segments")
    assert len(segments) == 1
    assert segments[0]["start"] == 0.0
    assert segments[0]["end"] == 12.345

    project = load_project(project_dir)
    transcript = object_dict(project, "transcript")
    narration = object_dict(transcript, "narration")
    assert narration["path"] == "transcripts/narration.json"
    assert narration["provider"] == "mock"
    assert narration["status"] == "transcribed"
    assert narration["segments"] == 1
    assert narration["word_count"] == 0


def test_transcribe_human_output_is_short_and_actionable(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)

    result = run_cli("transcribe", project_dir, "--provider", "mock")

    assert result.returncode == 0, result.stderr
    assert "Transcript:" in result.stdout
    assert "Source: audio/narration.wav" in result.stdout
    assert "Provider: mock" in result.stdout
    assert "Status: transcribed" in result.stdout
    assert "Segments: 1" in result.stdout
    assert "Words: 0" in result.stdout


def test_existing_project_without_transcript_remains_valid_and_is_normalized(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    data = load_project(project_dir)
    data.pop("transcript", None)
    write_project(project_dir, data)

    result = run_cli("transcribe", project_dir, "--provider", "mock", "--json")

    assert result.returncode == 0, result.stderr
    transcript = object_dict(load_project(project_dir), "transcript")
    assert "narration" in transcript


def test_transcribe_missing_extracted_audio_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=False)

    result = run_cli("transcribe", project_dir, "--provider", "mock", "--json")

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert "Missing media.audio.narration metadata. Run extract-audio before transcribe." in string_list(
        summary, "errors"
    )
    failure = only_failure(project_dir)
    assert failure["stage"] == "transcribe"
    assert failure["scope"] == "transcript:transcripts/narration.json"
    assert failure["source_path"] == "audio/narration.wav"
    assert failure["output_path"] == "transcripts/narration.json"
    assert failure["provider"] == "mock"
    assert failure["model"] == "large-v3"


def test_missing_faster_whisper_import_records_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        if name == "faster_whisper":
            raise ImportError("missing faster-whisper")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = transcribe_project(
        project_dir,
        provider="faster-whisper",
        model="large-v3",
        language=None,
        device="auto",
        compute_type="auto",
    )

    assert result["success"] is False
    assert "faster-whisper is not installed" in result["errors"][0]
    assert only_failure(project_dir)["stage"] == "transcribe"


def test_empty_faster_whisper_transcript_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    fake_package = fake_faster_whisper_package(tmp_path)

    result = run_cli("transcribe", project_dir, "--json", env=fake_env(fake_package, empty=True))

    assert result.returncode != 0
    assert "Transcript response does not contain valid timestamped segments." in string_list(
        json_object(result.stdout), "errors"
    )
    assert only_failure(project_dir)["stage"] == "transcribe"


def test_transcribe_write_failure_preserves_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    output_file = project_dir / "transcripts" / "narration.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text('{"previous":true}\n', encoding="utf-8")

    def fail_write(_path: Path, _payload: Record) -> None:
        raise OSError("simulated transcript replacement failure")

    monkeypatch.setattr(transcribe_module, "atomic_write_json", fail_write)

    result = transcribe_project(
        project_dir,
        provider="mock",
        model="large-v3",
        language=None,
        device="auto",
        compute_type="auto",
    )

    assert result["success"] is False
    assert "simulated transcript replacement failure" in result["errors"][0]
    assert output_file.read_text(encoding="utf-8") == '{"previous":true}\n'


def test_repeated_transcribe_failure_increments_attempt_count_without_duplication(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    fake_package = fake_faster_whisper_package(tmp_path)
    env = fake_env(fake_package, empty=True)

    first = run_cli("transcribe", project_dir, "--json", env=env)
    second = run_cli("transcribe", project_dir, "--json", env=env)

    assert first.returncode != 0
    assert second.returncode != 0
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    assert failures[0]["attempt_count"] == 2


def test_successful_transcribe_retry_resolves_matching_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    fake_package = fake_faster_whisper_package(tmp_path)
    assert run_cli("transcribe", project_dir, "--json", env=fake_env(fake_package, empty=True)).returncode != 0

    corrected = run_cli("transcribe", project_dir, "--json", env=fake_env(fake_package))

    assert corrected.returncode == 0, corrected.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def test_status_moves_to_ready_for_alignment_after_transcription(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_audio=True)
    assert run_cli("transcribe", project_dir, "--provider", "mock", "--json").returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "ready_for_alignment"
    assert status["next_action"] == "Align script.txt to the narration transcript."
    transcript = object_dict(status, "transcript")
    narration = object_dict(transcript, "narration")
    assert narration["transcribed"] is True
    assert narration["path"] == "transcripts/narration.json"
    assert narration["word_count"] == 0


def fake_faster_whisper_package(tmp_path: Path) -> Path:
    package_dir = tmp_path / "fake-packages" / "faster_whisper"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import os",
                "from pathlib import Path",
                "",
                "class Word:",
                "    def __init__(self, start, end, word, probability):",
                "        self.start = start",
                "        self.end = end",
                "        self.word = word",
                "        self.probability = probability",
                "",
                "class Segment:",
                "    def __init__(self, segment_id, start, end, text, words):",
                "        self.id = segment_id",
                "        self.start = start",
                "        self.end = end",
                "        self.text = text",
                "        self.words = words",
                "",
                "class WhisperModel:",
                "    def __init__(self, model_size_or_path, *, device, compute_type, download_root):",
                "        if os.environ.get('FAKE_FASTER_WHISPER_INIT_ERROR'):",
                "            raise RuntimeError(os.environ['FAKE_FASTER_WHISPER_INIT_ERROR'])",
                "        self.model_size_or_path = model_size_or_path",
                "        self.device = device",
                "        self.compute_type = compute_type",
                "        self.download_root = download_root",
                "",
                "    def transcribe(self, audio, *, beam_size, word_timestamps, vad_filter, language=None):",
                "        if os.environ.get('FAKE_FASTER_WHISPER_TRANSCRIBE_ERROR'):",
                "            raise RuntimeError(os.environ['FAKE_FASTER_WHISPER_TRANSCRIBE_ERROR'])",
                "        capture = os.environ.get('FAKE_FASTER_WHISPER_CAPTURE')",
                "        if capture:",
                "            Path(capture).write_text(json.dumps({",
                "                'audio': audio,",
                "                'model': self.model_size_or_path,",
                "                'device': self.device,",
                "                'compute_type': self.compute_type,",
                "                'download_root': self.download_root,",
                "                'beam_size': beam_size,",
                "                'word_timestamps': word_timestamps,",
                "                'vad_filter': vad_filter,",
                "                'language': language,",
                "            }), encoding='utf-8')",
                "        if os.environ.get('FAKE_FASTER_WHISPER_EMPTY') == '1':",
                "            return [], object()",
                "        words = [Word(0.0, 0.5, 'Hello', 0.98), Word(0.55, 1.0, 'world', 0.97)]",
                "        return [Segment(0, 0.0, 2.0, 'Hello world', words)], object()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return package_dir.parent


def fake_env(
    fake_package_root: Path,
    *,
    capture_file: Path | None = None,
    empty: bool = False,
) -> dict[str, str]:
    pythonpath = str(fake_package_root)
    existing = os.environ.get("PYTHONPATH")
    if existing:
        pythonpath = pythonpath + os.pathsep + existing
    env = {
        "PYTHONPATH": pythonpath,
        "FAKE_FASTER_WHISPER_EMPTY": "1" if empty else "0",
    }
    if capture_file is not None:
        env["FAKE_FASTER_WHISPER_CAPTURE"] = str(capture_file)
    return env


def init_project(tmp_path: Path, *, with_audio: bool) -> Path:
    project_dir = tmp_path / "my-video"
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr
    (project_dir / "script.txt").write_text("Hello world\nThis is the narration script.\n", encoding="utf-8")
    (project_dir / "raw.mp4").write_bytes(b"fake mp4 bytes")
    if with_audio:
        audio_file = project_dir / "audio" / "narration.wav"
        audio_file.parent.mkdir(parents=True, exist_ok=True)
        audio_file.write_bytes(b"fake wav data")
        data = load_project(project_dir)
        media = object_dict(data, "media")
        media["raw"] = {
            "path": "raw.mp4",
            "source_fingerprint": stat_fingerprint(project_dir / "raw.mp4"),
            "duration_seconds": 12.345,
            "size_bytes": 987654,
            "bit_rate": 8000000,
            "video": {"codec": "h264", "width": 1920, "height": 1080, "frame_rate": 29.97},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
            "probed_at": "2026-06-20T00:00:00Z",
        }
        media["audio"] = {
            "narration": {
                "path": "audio/narration.wav",
                "source": "raw.mp4",
                "source_fingerprint": stat_fingerprint(project_dir / "raw.mp4"),
                "artifact_fingerprint": stat_fingerprint(audio_file),
                "format": "wav",
                "codec": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "duration_seconds": 12.345,
                "size_bytes": len(b"fake wav data"),
                "extracted_at": "2026-06-20T00:00:01Z",
            }
        }
        write_project(project_dir, data)
    return project_dir


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def only_failure(project_dir: Path) -> Record:
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    return failures[0]


def json_object(raw_json: str) -> ProjectJson:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def object_dict(data: ProjectJson | Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: ProjectJson | Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    records: list[Record] = []
    for item in items:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def string_list(data: ProjectJson | Record, key: str) -> list[str]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    strings: list[str] = []
    for item in items:
        assert isinstance(item, str)
        strings.append(item)
    return strings
