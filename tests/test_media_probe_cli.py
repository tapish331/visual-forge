from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast


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


def test_probe_json_writes_compact_media_metadata_and_updates_status(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)

    result = run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, valid_ffprobe_json()))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    metadata = object_dict(summary, "metadata")
    assert metadata["duration_seconds"] == 12.345
    assert metadata["size_bytes"] == 987654
    assert metadata["bit_rate"] == 8000000
    assert object_dict(metadata, "source_fingerprint")["kind"] == "stat_v1"
    video = object_dict(metadata, "video")
    assert video["codec"] == "h264"
    assert video["width"] == 1920
    assert video["height"] == 1080
    assert video["frame_rate"] == 29.97
    audio = object_dict(metadata, "audio")
    assert audio["codec"] == "aac"
    assert audio["sample_rate"] == 48000
    assert audio["channels"] == 2

    project = load_project(project_dir)
    media = object_dict(project, "media")
    raw = object_dict(media, "raw")
    assert raw["path"] == "raw.mp4"
    assert isinstance(raw["probed_at"], str)

    status = run_cli("status", project_dir, "--json")
    assert status.returncode == 0, status.stderr
    status_summary = json_object(status.stdout)
    assert status_summary["state"] == "ready_for_audio"
    assert status_summary["next_action"] == "Extract audio from raw.mp4."
    status_media = object_dict(status_summary, "media")
    status_raw = object_dict(status_media, "raw")
    assert status_raw["probed"] is True


def test_probe_reads_video_referenced_outside_project_directory(tmp_path: Path) -> None:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    script_source = inputs_dir / "episode.mp4.txt"
    video_source = inputs_dir / "episode.mp4"
    script_source.write_text("Hello world.\n", encoding="utf-8")
    video_source.write_bytes(b"external fake mp4")
    project_dir = tmp_path / "projects" / "episode"
    initialized = run_cli("init", project_dir, "--script", script_source, "--video", video_source)
    assert initialized.returncode == 0, initialized.stderr
    ffprobe = fake_ffprobe(tmp_path)

    result = run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, valid_ffprobe_json()))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["media_path"] == "../../inputs/episode.mp4"
    raw = object_dict(object_dict(load_project(project_dir), "media"), "raw")
    assert raw["path"] == "../../inputs/episode.mp4"


def test_probe_human_output_is_short_and_actionable(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)

    result = run_cli("probe", project_dir, env=fake_env(ffprobe, valid_ffprobe_json()))

    assert result.returncode == 0, result.stderr
    assert "Media: raw.mp4" in result.stdout
    assert "Status: probed" in result.stdout
    assert "Duration: 12.345s" in result.stdout
    assert "Video: 1920x1080 at 29.97 fps" in result.stdout


def test_existing_project_without_media_remains_valid_and_is_normalized(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    data = load_project(project_dir)
    del data["media"]
    write_project(project_dir, data)
    ffprobe = fake_ffprobe(tmp_path)

    result = run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, valid_ffprobe_json()))

    assert result.returncode == 0, result.stderr
    media = object_dict(load_project(project_dir), "media")
    assert "raw" in media


def test_probe_missing_raw_records_active_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path, with_raw=False)

    result = run_cli("probe", project_dir, "--json")

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert "Missing media file: raw.mp4" in string_list(summary, "errors")
    failure = only_failure(project_dir)
    assert failure["stage"] == "media_probe"
    assert failure["scope"] == "media:raw.mp4"
    assert failure["status"] == "active"
    assert failure["media_path"] == "raw.mp4"


def test_probe_missing_ffprobe_records_active_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)

    result = run_cli(
        "probe",
        project_dir,
        "--json",
        env={"VISUAL_FORGE_FFPROBE": str(tmp_path / "missing-ffprobe.exe")},
    )

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert "ffprobe not found" in string_list(summary, "errors")[0]
    failure = only_failure(project_dir)
    assert failure["stage"] == "media_probe"


def test_probe_invalid_ffprobe_json_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)

    result = run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, "{not-json"))

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert "Invalid ffprobe JSON" in string_list(summary, "errors")[0]
    assert only_failure(project_dir)["stage"] == "media_probe"


def test_probe_missing_stream_records_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    output = {
        "format": {"duration": "12.345"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}],
    }

    result = run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, json.dumps(output)))

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert "ffprobe output is missing an audio stream" in string_list(summary, "errors")
    assert only_failure(project_dir)["stage"] == "media_probe"


def test_repeated_probe_failure_increments_attempt_count_without_duplication(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    env = fake_env(ffprobe, "{not-json")

    first = run_cli("probe", project_dir, "--json", env=env)
    second = run_cli("probe", project_dir, "--json", env=env)

    assert first.returncode != 0
    assert second.returncode != 0
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    assert failures[0]["attempt_count"] == 2


def test_successful_probe_retry_resolves_matching_failure(tmp_path: Path) -> None:
    project_dir = init_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    assert run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, "{not-json")).returncode != 0

    corrected = run_cli("probe", project_dir, "--json", env=fake_env(ffprobe, valid_ffprobe_json()))

    assert corrected.returncode == 0, corrected.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)
    status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert status["state"] == "ready_for_audio"


def init_project(tmp_path: Path, *, with_raw: bool = True) -> Path:
    project_dir = tmp_path / "my-video"
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr
    (project_dir / "script.txt").write_text("Hello world\n", encoding="utf-8")
    if with_raw:
        (project_dir / "raw.mp4").write_bytes(b"fake mp4 bytes")
    return project_dir


def fake_ffprobe(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ffprobe.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "import sys",
                "stderr = os.environ.get('FAKE_FFPROBE_STDERR', '')",
                "if stderr:",
                "    sys.stderr.write(stderr)",
                "sys.stdout.write(os.environ.get('FAKE_FFPROBE_OUTPUT', '{}'))",
                "raise SystemExit(int(os.environ.get('FAKE_FFPROBE_EXIT', '0')))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = tmp_path / "fake_ffprobe.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return wrapper

    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def fake_env(ffprobe: Path, output: str, *, exit_code: int = 0, stderr: str = "") -> dict[str, str]:
    return {
        "VISUAL_FORGE_FFPROBE": str(ffprobe),
        "FAKE_FFPROBE_OUTPUT": output,
        "FAKE_FFPROBE_EXIT": str(exit_code),
        "FAKE_FFPROBE_STDERR": stderr,
    }


def valid_ffprobe_json() -> str:
    return json.dumps(
        {
            "format": {
                "duration": "12.345",
                "size": "987654",
                "bit_rate": "8000000",
            },
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }
    )


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
