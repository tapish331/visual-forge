from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from app.artifacts import stat_fingerprint

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


def test_extract_audio_json_writes_wav_and_project_metadata(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["source_path"] == "raw.mp4"
    assert summary["output_path"] == str(project_dir / "audio" / "narration.wav")
    output_file = project_dir / "audio" / "narration.wav"
    assert output_file.exists()
    assert output_file.read_bytes() == b"fake wav data"

    metadata = object_dict(summary, "metadata")
    assert metadata["path"] == "audio/narration.wav"
    assert metadata["source"] == "raw.mp4"
    assert metadata["format"] == "wav"
    assert metadata["codec"] == "pcm_s16le"
    assert metadata["sample_rate"] == 16000
    assert metadata["channels"] == 1
    assert metadata["duration_seconds"] == 12.345
    assert metadata["size_bytes"] == len(b"fake wav data")
    assert object_dict(metadata, "source_fingerprint")["kind"] == "stat_v1"
    assert object_dict(metadata, "artifact_fingerprint")["kind"] == "stat_v1"

    project = load_project(project_dir)
    media = object_dict(project, "media")
    audio = object_dict(media, "audio")
    narration = object_dict(audio, "narration")
    assert narration["path"] == "audio/narration.wav"
    assert isinstance(narration["extracted_at"], str)


def test_extract_audio_human_output_is_short_and_actionable(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("extract-audio", project_dir, env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    assert "Audio:" in result.stdout
    assert "Source: raw.mp4" in result.stdout
    assert "Status: extracted" in result.stdout
    assert "Sample rate: 16000 Hz" in result.stdout


def test_existing_project_without_audio_remains_valid_and_is_normalized(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    data = load_project(project_dir)
    media = object_dict(data, "media")
    media.pop("audio", None)
    write_project(project_dir, data)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    audio = object_dict(object_dict(load_project(project_dir), "media"), "audio")
    assert "narration" in audio


def test_extract_audio_missing_probe_metadata_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=False)

    result = run_cli("extract-audio", project_dir, "--json")

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert "Missing media.raw probe metadata. Run probe before extract-audio." in string_list(summary, "errors")
    failure = only_failure(project_dir)
    assert failure["stage"] == "audio_extract"
    assert failure["scope"] == "media:audio/narration.wav"
    assert failure["source_path"] == "raw.mp4"
    assert failure["output_path"] == "audio/narration.wav"


def test_extract_audio_missing_raw_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True, with_raw=False)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg))

    assert result.returncode != 0
    assert "Missing media file: raw.mp4" in string_list(json_object(result.stdout), "errors")
    assert only_failure(project_dir)["stage"] == "audio_extract"


def test_extract_audio_missing_ffmpeg_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)

    result = run_cli(
        "extract-audio",
        project_dir,
        "--json",
        env={"VISUAL_FORGE_FFMPEG": str(tmp_path / "missing-ffmpeg.exe")},
    )

    assert result.returncode != 0
    assert "ffmpeg not found" in string_list(json_object(result.stdout), "errors")[0]
    assert only_failure(project_dir)["stage"] == "audio_extract"


def test_extract_audio_ffmpeg_failure_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli(
        "extract-audio",
        project_dir,
        "--json",
        env=fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure"),
    )

    assert result.returncode != 0
    errors = string_list(json_object(result.stdout), "errors")
    assert "ffmpeg failed with exit code 7: simulated ffmpeg failure" in errors[0]
    assert only_failure(project_dir)["stage"] == "audio_extract"


def test_extract_audio_failure_preserves_existing_artifact(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    output_file = project_dir / "audio" / "narration.wav"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(b"previous valid audio")
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli(
        "extract-audio",
        project_dir,
        "--json",
        env=fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure"),
    )

    assert result.returncode == 1
    assert output_file.read_bytes() == b"previous valid audio"
    assert list(output_file.parent.glob(".narration.*.wav")) == []


def test_extract_audio_missing_output_after_success_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg, create_output=False))

    assert result.returncode != 0
    errors = string_list(json_object(result.stdout), "errors")
    assert "FFmpeg completed but did not create audio/narration.wav" in errors
    assert only_failure(project_dir)["stage"] == "audio_extract"


def test_repeated_extract_audio_failure_increments_attempt_count_without_duplication(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)
    env = fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure")

    first = run_cli("extract-audio", project_dir, "--json", env=env)
    second = run_cli("extract-audio", project_dir, "--json", env=env)

    assert first.returncode != 0
    assert second.returncode != 0
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    assert failures[0]["attempt_count"] == 2


def test_successful_extract_audio_retry_resolves_matching_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)
    assert run_cli(
        "extract-audio",
        project_dir,
        "--json",
        env=fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure"),
    ).returncode != 0

    corrected = run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg))

    assert corrected.returncode == 0, corrected.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def test_status_moves_to_ready_for_transcription_after_audio_extraction(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, probed=True)
    ffmpeg = fake_ffmpeg(tmp_path)
    assert run_cli("extract-audio", project_dir, "--json", env=fake_env(ffmpeg)).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "ready_for_transcription"
    assert status["next_action"] == "Transcribe narration audio."
    media = object_dict(status, "media")
    audio = object_dict(media, "audio")
    narration = object_dict(audio, "narration")
    assert narration["extracted"] is True
    assert narration["path"] == "audio/narration.wav"


def init_project(tmp_path: Path, *, probed: bool, with_raw: bool = True) -> Path:
    project_dir = tmp_path / "my-video"
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr
    (project_dir / "script.txt").write_text("Hello world\n", encoding="utf-8")
    if with_raw:
        (project_dir / "raw.mp4").write_bytes(b"fake mp4 bytes")
    if probed:
        data = load_project(project_dir)
        media = object_dict(data, "media")
        media["raw"] = {
            "path": "raw.mp4",
            "source_fingerprint": (
                stat_fingerprint(project_dir / "raw.mp4")
                if with_raw
                else {"kind": "stat_v1", "size_bytes": 0, "modified_ns": 0}
            ),
            "duration_seconds": 12.345,
            "size_bytes": 987654,
            "bit_rate": 8000000,
            "video": {"codec": "h264", "width": 1920, "height": 1080, "frame_rate": 29.97},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
            "probed_at": "2026-06-20T00:00:00Z",
        }
        write_project(project_dir, data)
    return project_dir


def fake_ffmpeg(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "import sys",
                "from pathlib import Path",
                "stderr = os.environ.get('FAKE_FFMPEG_STDERR', '')",
                "if stderr:",
                "    sys.stderr.write(stderr)",
                "if os.environ.get('FAKE_FFMPEG_CREATE_OUTPUT', '1') == '1':",
                "    output = Path(sys.argv[-1])",
                "    output.parent.mkdir(parents=True, exist_ok=True)",
                "    output.write_bytes(b'fake wav data')",
                "raise SystemExit(int(os.environ.get('FAKE_FFMPEG_EXIT', '0')))",
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


def fake_env(
    ffmpeg: Path,
    *,
    exit_code: int = 0,
    stderr: str = "",
    create_output: bool = True,
) -> dict[str, str]:
    return {
        "VISUAL_FORGE_FFMPEG": str(ffmpeg),
        "FAKE_FFMPEG_EXIT": str(exit_code),
        "FAKE_FFMPEG_STDERR": stderr,
        "FAKE_FFMPEG_CREATE_OUTPUT": "1" if create_output else "0",
    }


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
