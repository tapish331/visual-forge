from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

import pytest

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.layout import layout_metadata
from app.project import JsonObject, ProjectState, build_initial_project, write_project
from app.render_freshness import build_visual_plan_fingerprint
from app.timeline import (
    build_chunking_metadata,
    build_full_raw_timeline,
    build_timeline_fingerprint,
    current_chunk_plan_fingerprint,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env["VISUAL_FORGE_LOG_DISABLED"] = "1"
    if env is not None:
        command_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=command_env,
    )


def test_verify_final_legacy_writes_report_metadata_and_completes(tmp_path: Path) -> None:
    project_dir, artifact_root = prepared_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    payload = write_payload(tmp_path, valid_probe())

    result = run_cli("verify-final", project_dir, "--json", env=fake_env(ffprobe, payload))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["passed"] is True
    assert summary["error_count"] == 0
    report_file = artifact_root / "verification" / "final.json"
    assert Path(string_value(summary, "report_path")) == report_file
    report = json_object(report_file.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert object_dict(report, "actual")["stream_counts"] == {"video": 1, "audio": 1}

    project = load_project(project_dir)
    metadata = object_dict(object_dict(project, "verification"), "final")
    assert metadata["status"] == "passed"
    assert object_dict(metadata, "artifact_fingerprint")["kind"] == "sha256_v1"

    status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert status["state"] == "complete"
    assert status["next_action"] == "Review final video."
    verification = object_dict(object_dict(status, "verification"), "final")
    assert verification["current"] is True


def test_verify_final_layout_project_writes_outputs_report(tmp_path: Path) -> None:
    project_dir, artifact_root = prepared_project(tmp_path, layout_v1=True)
    ffprobe = fake_ffprobe(tmp_path)

    result = run_cli(
        "verify-final",
        project_dir,
        "--json",
        env=fake_env(ffprobe, write_payload(tmp_path, valid_probe())),
    )

    assert result.returncode == 0, result.stderr
    assert Path(string_value(json_object(result.stdout), "report_path")) == artifact_root / "verification" / "final.json"
    assert not (project_dir / "verification" / "final.json").exists()


def test_status_resolved_paths_use_layout_outputs_root(tmp_path: Path) -> None:
    project_dir, artifact_root = prepared_project(tmp_path, layout_v1=True)
    ffprobe = fake_ffprobe(tmp_path)
    result = run_cli(
        "verify-final",
        project_dir,
        "--json",
        env=fake_env(ffprobe, write_payload(tmp_path, valid_probe())),
    )
    assert result.returncode == 0, result.stderr

    status = json_object(run_cli("status", project_dir, "--json").stdout)
    script = object_dict(object_dict(status, "inputs"), "script")
    video = object_dict(object_dict(status, "inputs"), "video")
    final = object_dict(object_dict(status, "outputs"), "final")
    media_audio = object_dict(object_dict(object_dict(status, "media"), "audio"), "narration")
    transcript = object_dict(object_dict(status, "transcript"), "narration")
    alignment = object_dict(object_dict(status, "alignment"), "script")
    verification = object_dict(object_dict(status, "verification"), "final")

    assert script["path"] == "../../inputs/episode/script.txt"
    assert script["resolved_path"] == display_path(tmp_path / "inputs" / "episode" / "script.txt")
    assert video["path"] == "../../inputs/episode/raw.mp4"
    assert video["resolved_path"] == display_path(tmp_path / "inputs" / "episode" / "raw.mp4")
    assert final["path"] == "final.mp4"
    assert final["resolved_path"] == display_path(artifact_root / "final.mp4")
    assert media_audio["path"] == "audio/narration.wav"
    assert media_audio["resolved_path"] == display_path(artifact_root / "audio" / "narration.wav")
    assert transcript["path"] == "transcripts/narration.json"
    assert transcript["resolved_path"] == display_path(artifact_root / "transcripts" / "narration.json")
    assert alignment["path"] == "alignment/script_alignment.json"
    assert alignment["resolved_path"] == display_path(artifact_root / "alignment" / "script_alignment.json")
    assert verification["path"] == "verification/final.json"
    assert verification["resolved_path"] == display_path(artifact_root / "verification" / "final.json")


def test_status_resolved_paths_keep_legacy_project_local_outputs(tmp_path: Path) -> None:
    project_dir, _ = prepared_project(tmp_path)

    status = json_object(run_cli("status", project_dir, "--json").stdout)
    final = object_dict(object_dict(status, "outputs"), "final")
    media_audio = object_dict(object_dict(object_dict(status, "media"), "audio"), "narration")
    transcript = object_dict(object_dict(status, "transcript"), "narration")
    alignment = object_dict(object_dict(status, "alignment"), "script")
    verification = object_dict(object_dict(status, "verification"), "final")

    assert final["path"] == "final.mp4"
    assert final["resolved_path"] == display_path(project_dir / "final.mp4")
    assert media_audio["path"] == "audio/narration.wav"
    assert media_audio["resolved_path"] == display_path(project_dir / "audio" / "narration.wav")
    assert transcript["path"] == "transcripts/narration.json"
    assert transcript["resolved_path"] == display_path(project_dir / "transcripts" / "narration.json")
    assert alignment["path"] == "alignment/script_alignment.json"
    assert alignment["resolved_path"] == display_path(project_dir / "alignment" / "script_alignment.json")
    assert verification["path"] == "verification/final.json"
    assert verification["resolved_path"] == display_path(project_dir / "verification" / "final.json")


def test_verify_final_bitrate_and_missing_optional_labels_are_warnings(tmp_path: Path) -> None:
    project_dir, _ = prepared_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    probe = valid_probe()
    video, audio = primary_streams(probe)
    video["bit_rate"] = "4000000"
    audio["bit_rate"] = "100000"
    video.pop("display_aspect_ratio")
    video.pop("sample_aspect_ratio")
    audio.pop("channel_layout")

    result = run_cli(
        "verify-final",
        project_dir,
        "--json",
        env=fake_env(ffprobe, write_payload(tmp_path, probe)),
    )

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["passed"] is True
    assert summary["warning_count"] == 5
    assert record_list(load_project(project_dir), "failures") == []


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("format", "format_name"), "matroska,webm", "Container is not MP4-compatible"),
        (("video", "codec_name"), "hevc", "Video codec mismatch"),
        (("video", "profile"), "Main", "Video profile mismatch"),
        (("video", "width"), 1280, "Video width mismatch"),
        (("video", "height"), 720, "Video height mismatch"),
        (("video", "pix_fmt"), "yuv422p", "Pixel format mismatch"),
        (("video", "field_order"), "tt", "Scan type mismatch"),
        (("video", "avg_frame_rate"), "25/1", "Frame rate mismatch"),
        (("video", "color_space"), "smpte170m", "Color space mismatch"),
        (("video", "color_transfer"), "smpte170m", "Color transfer mismatch"),
        (("video", "color_primaries"), "smpte170m", "Color primaries mismatch"),
        (("video", "display_aspect_ratio"), "4:3", "Display aspect ratio mismatch"),
        (("video", "sample_aspect_ratio"), "2:1", "Sample aspect ratio mismatch"),
        (("audio", "codec_name"), "mp3", "Audio codec mismatch"),
        (("audio", "profile"), "HE-AAC", "Audio profile mismatch"),
        (("audio", "sample_rate"), "44100", "Audio sample rate mismatch"),
        (("audio", "channels"), 1, "Audio channels mismatch"),
        (("audio", "channel_layout"), "mono", "Audio channel layout mismatch"),
        (("format", "duration"), "10.5", "Final duration mismatch"),
        (("audio", "duration"), "9.5", "Video/audio duration drift"),
        (("video", "duration"), None, "Video stream duration is missing or invalid"),
    ],
)
def test_verify_final_hard_rules_fail(
    tmp_path: Path,
    path: tuple[str, str],
    value: object,
    message: str,
) -> None:
    project_dir, _ = prepared_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    probe = valid_probe()
    set_probe_value(probe, path, value)

    result = run_cli(
        "verify-final",
        project_dir,
        "--json",
        env=fake_env(ffprobe, write_payload(tmp_path, probe)),
    )

    assert result.returncode == 1
    assert any(message in error for error in string_list(json_object(result.stdout), "errors"))
    assert only_failure(project_dir)["stage"] == "final_verify"


@pytest.mark.parametrize("missing_type", ["video", "audio"])
def test_verify_final_missing_stream_fails(tmp_path: Path, missing_type: str) -> None:
    project_dir, _ = prepared_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    probe = valid_probe()
    streams = cast(list[object], probe["streams"])
    probe["streams"] = [
        stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") != missing_type
    ]

    result = run_cli(
        "verify-final",
        project_dir,
        "--json",
        env=fake_env(ffprobe, write_payload(tmp_path, probe)),
    )

    assert result.returncode == 1
    assert any(f"Expected exactly one {missing_type} stream" in error for error in string_list(json_object(result.stdout), "errors"))


def test_verify_final_missing_ffprobe_and_invalid_json_record_failures(tmp_path: Path) -> None:
    project_dir, _ = prepared_project(tmp_path)
    missing = run_cli(
        "verify-final",
        project_dir,
        "--json",
        env={"VISUAL_FORGE_FFPROBE": str(tmp_path / "missing-ffprobe.exe")},
    )
    assert missing.returncode == 1
    assert string_list(json_object(missing.stdout), "errors")[0].startswith("ffprobe not found")

    ffprobe = fake_ffprobe(tmp_path)
    invalid_payload = tmp_path / "invalid.json"
    invalid_payload.write_text("{invalid", encoding="utf-8")
    invalid = run_cli("verify-final", project_dir, "--json", env=fake_env(ffprobe, invalid_payload))
    assert invalid.returncode == 1
    assert any("Invalid ffprobe JSON" in error for error in string_list(json_object(invalid.stdout), "errors"))
    failure = only_failure(project_dir)
    assert failure["attempt_count"] == 2
    assert failure["scope"] == "final:final.mp4"


def test_verify_final_missing_final_records_failure_and_report(tmp_path: Path) -> None:
    project_dir, artifact_root = prepared_project(tmp_path)
    (artifact_root / "final.mp4").unlink()

    result = run_cli("verify-final", project_dir, "--json")

    assert result.returncode == 1
    assert any("Final video is not current" in error for error in string_list(json_object(result.stdout), "errors"))
    assert (artifact_root / "verification" / "final.json").is_file()
    assert only_failure(project_dir)["stage"] == "final_verify"


def test_verify_final_successful_retry_resolves_failure(tmp_path: Path) -> None:
    project_dir, _ = prepared_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    bad = valid_probe()
    set_probe_value(bad, ("video", "codec_name"), "hevc")
    first = run_cli("verify-final", project_dir, "--json", env=fake_env(ffprobe, write_payload(tmp_path, bad)))
    second = run_cli("verify-final", project_dir, "--json", env=fake_env(ffprobe, write_payload(tmp_path, valid_probe())))

    assert first.returncode == 1
    assert second.returncode == 0, second.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def test_recomposition_makes_previous_verification_stale(tmp_path: Path) -> None:
    project_dir, artifact_root = prepared_project(tmp_path)
    ffprobe = fake_ffprobe(tmp_path)
    assert run_cli(
        "verify-final",
        project_dir,
        "--json",
        env=fake_env(ffprobe, write_payload(tmp_path, valid_probe())),
    ).returncode == 0

    final_file = artifact_root / "final.mp4"
    final_file.write_bytes(b"recomposed final")
    project = load_project(project_dir)
    final_metadata = object_dict(object_dict(project, "renders"), "final")
    final_metadata["artifact_fingerprint"] = stat_fingerprint(final_file)
    final_metadata["composed_at"] = "2026-06-21T00:00:00Z"
    write_project_json(project_dir, project)

    status = json_object(run_cli("status", project_dir, "--json").stdout)
    assert status["state"] == "ready_for_verification"
    verification = object_dict(object_dict(status, "verification"), "final")
    assert verification["current"] is False


def test_verify_final_rejects_composed_duration_shorter_than_timeline(tmp_path: Path) -> None:
    project_dir, _ = prepared_project(tmp_path)
    project = load_project(project_dir)
    final_metadata = object_dict(object_dict(project, "renders"), "final")
    final_metadata["duration_seconds"] = 9.0
    write_project_json(project_dir, project)

    result = run_cli("verify-final", project_dir, "--json")

    assert result.returncode == 1
    assert any(
        "Composed duration does not cover canonical timeline" in error
        for error in string_list(json_object(result.stdout), "errors")
    )


def prepared_project(tmp_path: Path, *, layout_v1: bool = False) -> tuple[Path, Path]:
    if layout_v1:
        input_dir = tmp_path / "inputs" / "episode"
        project_dir = tmp_path / "projects" / "episode"
        artifact_root = tmp_path / "outputs" / "episode"
        project_dir.mkdir(parents=True)
        layout = layout_metadata(project_dir, slug="episode", input_dir=input_dir.resolve(), outputs_dir=artifact_root)
    else:
        project_dir = tmp_path / "my-video"
        artifact_root = project_dir
        project_dir.mkdir()
        input_dir = project_dir
        layout = None

    script_file = input_dir / "script.txt"
    raw_file = input_dir / "raw.mp4"
    script_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text("Hello world.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")
    audio_file = artifact_root / "audio" / "narration.wav"
    transcript_file = artifact_root / "transcripts" / "narration.json"
    alignment_file = artifact_root / "alignment" / "script_alignment.json"
    chunk_file = artifact_root / "renders" / "chunks" / "chunk_001.mp4"
    preview_file = artifact_root / "previews" / "preview_001.png"
    final_file = artifact_root / "final.mp4"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"audio")
    atomic_write_json(transcript_file, {"schema_version": 1, "text": "Hello world.", "segments": []})
    atomic_write_json(alignment_file, {"schema_version": 1, "blocks": []})
    chunk_file.parent.mkdir(parents=True, exist_ok=True)
    chunk_file.write_bytes(b"rendered chunk")
    preview_file.parent.mkdir(parents=True, exist_ok=True)
    preview_file.write_bytes(b"rendered preview")
    final_file.write_bytes(b"composed final")

    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    chunk_fingerprint = stat_fingerprint(chunk_file)
    state = build_initial_project(project_dir, script_source=script_file, video_source=raw_file, layout=layout)
    state["media"] = {
        "raw": {
            "path": state["project"]["video"],
            "duration_seconds": 10.0,
            "source_fingerprint": raw_fingerprint,
            "video": {"codec": "h264", "width": 1920, "height": 1080, "frame_rate": 30.0},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
        },
        "audio": {
            "narration": {
                "path": "audio/narration.wav",
                "source_fingerprint": raw_fingerprint,
                "artifact_fingerprint": audio_fingerprint,
            }
        },
    }
    state["transcript"] = {
        "narration": {
            "path": "transcripts/narration.json",
            "status": "transcribed",
            "source_fingerprint": audio_fingerprint,
            "artifact_fingerprint": transcript_fingerprint,
        }
    }
    state["alignment"] = {
        "script": {
            "path": "alignment/script_alignment.json",
            "status": "aligned",
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(script_file)),
                "transcript_sha256": digest_value(transcript_fingerprint),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }
    state["chunks"] = [
        {
            "id": "chunk_001",
            "start": 0.0,
            "end": 10.0,
            "status": "rendered",
            "visual_mode": "visuals",
            "alignment_block_ids": [],
            "warning_block_ids": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
    ]
    template_file = REPO_ROOT / "templates" / "simple_card.py"
    preview_fingerprint = sha256_fingerprint(preview_file)
    state["visuals"] = [
        {
            "id": "visual_001",
            "chunk_id": "chunk_001",
            "template_ref": "simple_card",
            "template_id": "simple_card",
            "params": {"title": "Key idea"},
            "start": 0.0,
            "end": 10.0,
            "status": "previewed",
            "preview_id": "preview_001",
        }
    ]
    state["previews"] = [
        {
            "id": "preview_001",
            "template_ref": "simple_card",
            "template_id": "simple_card",
            "params": {"title": "Key idea"},
            "output": "previews/preview_001.png",
            "status": "rendered",
            "template_version": "1.0.0",
            "template_fingerprint": sha256_fingerprint(template_file),
            "artifact_fingerprint": preview_fingerprint,
        }
    ]
    visual_plan_fingerprint = build_visual_plan_fingerprint(state, "chunk_001")
    assert visual_plan_fingerprint is not None
    timeline = build_full_raw_timeline(project_dir, state)
    options: JsonObject = {"target_seconds": 180.0, "min_seconds": 90.0, "max_seconds": 240.0}
    state["timeline"] = timeline
    state["chunking"] = build_chunking_metadata(state, timeline, state["chunks"], options)
    chunk_plan_fingerprint = current_chunk_plan_fingerprint(state)
    assert chunk_plan_fingerprint is not None
    state["renders"] = {
        "chunks": {
            "chunk_001": {
                "path": "renders/chunks/chunk_001.mp4",
                "chunk_id": "chunk_001",
                "source_fingerprint": raw_fingerprint,
                "duration_seconds": 10.0,
                "status": "rendered",
                "visual_plan_fingerprint": visual_plan_fingerprint,
                "chunk_plan_fingerprint": chunk_plan_fingerprint,
                "preview_fingerprints": {"preview_001": preview_fingerprint},
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
            "timeline_fingerprint": build_timeline_fingerprint(timeline),
            "chunk_plan_fingerprint": chunk_plan_fingerprint,
            "artifact_fingerprint": stat_fingerprint(final_file),
        },
    }
    write_project(project_dir / "project.json", state)
    return project_dir, artifact_root


def valid_probe() -> dict[str, object]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "profile": "High",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "16:9",
                "pix_fmt": "yuv420p",
                "color_space": "bt709",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
                "field_order": "progressive",
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
                "duration": "10.000",
                "bit_rate": "8000000",
            },
            {
                "index": 1,
                "codec_name": "aac",
                "profile": "LC",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "duration": "10.000",
                "bit_rate": "384000",
            },
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "10.000",
            "size": "10000000",
            "bit_rate": "8384000",
        },
    }


def primary_streams(probe: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    streams = cast(list[dict[str, object]], probe["streams"])
    return streams[0], streams[1]


def set_probe_value(probe: dict[str, object], path: tuple[str, str], value: object) -> None:
    section, key = path
    if section == "format":
        target = cast(dict[str, object], probe["format"])
    else:
        video, audio = primary_streams(probe)
        target = video if section == "video" else audio
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def fake_ffprobe(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ffprobe.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "import sys",
                "from pathlib import Path",
                "payload = Path(os.environ['FAKE_FFPROBE_PAYLOAD'])",
                "sys.stdout.write(payload.read_text(encoding='utf-8'))",
                "stderr = os.environ.get('FAKE_FFPROBE_STDERR', '')",
                "if stderr:",
                "    sys.stderr.write(stderr)",
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


def fake_env(ffprobe: Path, payload: Path, *, exit_code: int = 0, stderr: str = "") -> dict[str, str]:
    return {
        "VISUAL_FORGE_FFPROBE": str(ffprobe),
        "FAKE_FFPROBE_PAYLOAD": str(payload),
        "FAKE_FFPROBE_EXIT": str(exit_code),
        "FAKE_FFPROBE_STDERR": stderr,
    }


def write_payload(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / f"probe-{len(list(tmp_path.glob('probe-*.json')))}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project_json(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def only_failure(project_dir: Path) -> Record:
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    return failures[0]


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


def string_list(data: Record, key: str) -> list[str]:
    value = data[key]
    assert isinstance(value, list)
    strings: list[str] = []
    for item in value:
        assert isinstance(item, str)
        strings.append(item)
    return strings


def string_value(data: Record, key: str) -> str:
    value = data[key]
    assert isinstance(value, str)
    return value


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value
