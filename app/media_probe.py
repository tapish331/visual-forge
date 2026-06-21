"""Raw narration video probing with ffprobe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import stat_fingerprint
from .failures import media_failure_scope, record_failure, resolve_matching_failures
from .project import JsonObject, ProjectState, load_project, project_file_for, utc_now_iso, write_project


FFPROBE_ENV_VAR = "VISUAL_FORGE_FFPROBE"
MEDIA_PROBE_STAGE = "media_probe"
MEDIA_PROBE_RECOMMENDED_NEXT_ACTION = "Fix the configured raw video or ffprobe availability, then rerun probe."


class MediaProbeResult(TypedDict):
    project_dir: str
    media_path: str
    success: bool
    metadata: JsonObject | None
    errors: list[str]


def probe_project_media(project_dir: Path) -> MediaProbeResult:
    data = load_project(project_dir)
    media_path = data["project"]["video"]
    raw_path = project_dir / media_path

    metadata, errors = probe_raw_video(raw_path, media_path=media_path)
    result: MediaProbeResult = {
        "project_dir": str(project_dir),
        "media_path": media_path,
        "success": not errors and metadata is not None,
        "metadata": metadata,
        "errors": errors,
    }

    if result["success"] and metadata is not None:
        _record_probe_success(project_dir, data, media_path, metadata)
    else:
        _record_probe_failure(project_dir, data, media_path, errors)
    return result


def probe_raw_video(raw_path: Path, *, media_path: str) -> tuple[JsonObject | None, list[str]]:
    if not raw_path.exists():
        return None, [f"Missing media file: {media_path}"]
    if not raw_path.is_file():
        return None, [f"Media path is not a file: {media_path}"]

    ffprobe, ffprobe_error = resolve_ffprobe()
    if ffprobe is None:
        return None, [ffprobe_error]

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(raw_path),
    ]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, [f"Could not run ffprobe: {exc}"]

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        return None, [f"ffprobe failed with exit code {completed.returncode}{detail}"]

    try:
        raw: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return None, [f"Invalid ffprobe JSON: {exc.msg}"]

    if not isinstance(raw, dict):
        return None, ["ffprobe JSON must be an object"]
    return normalize_ffprobe_output(cast(dict[str, object], raw), raw_path=raw_path, media_path=media_path)


def resolve_ffprobe() -> tuple[str | None, str]:
    configured = os.environ.get(FFPROBE_ENV_VAR)
    if configured is not None and configured.strip():
        configured_path = configured.strip()
        if Path(configured_path).exists():
            return configured_path, ""
        return None, f"ffprobe not found at {FFPROBE_ENV_VAR}: {configured_path}"

    discovered = shutil.which("ffprobe")
    if discovered is None:
        return None, "ffprobe not found. Set VISUAL_FORGE_FFPROBE or add ffprobe to PATH."
    return discovered, ""


def normalize_ffprobe_output(raw: dict[str, object], *, raw_path: Path, media_path: str) -> tuple[JsonObject | None, list[str]]:
    errors: list[str] = []
    format_data = _object_field(raw, "format")
    streams = _stream_list(raw.get("streams"))

    if format_data is None:
        errors.append("ffprobe output is missing format data")
    video_stream = _first_stream(streams, "video")
    audio_stream = _first_stream(streams, "audio")
    if video_stream is None:
        errors.append("ffprobe output is missing a video stream")
    if audio_stream is None:
        errors.append("ffprobe output is missing an audio stream")

    duration = _float_from_field(format_data, "duration") if format_data is not None else None
    if duration is None or duration <= 0:
        errors.append("ffprobe output is missing a valid duration")

    if errors:
        return None, errors
    if format_data is None or video_stream is None or audio_stream is None or duration is None:
        return None, errors or ["ffprobe output is incomplete"]

    metadata: JsonObject = {
        "path": media_path,
        "source_fingerprint": stat_fingerprint(raw_path),
        "duration_seconds": duration,
        "size_bytes": _int_from_field(format_data, "size") or raw_path.stat().st_size,
        "bit_rate": _int_from_field(format_data, "bit_rate"),
        "video": {
            "codec": _string_field(video_stream, "codec_name", "unknown"),
            "width": _int_from_field(video_stream, "width"),
            "height": _int_from_field(video_stream, "height"),
            "frame_rate": _frame_rate(video_stream),
        },
        "audio": {
            "codec": _string_field(audio_stream, "codec_name", "unknown"),
            "sample_rate": _int_from_field(audio_stream, "sample_rate"),
            "channels": _int_from_field(audio_stream, "channels"),
        },
        "probed_at": utc_now_iso(),
    }
    return metadata, []


def format_media_probe_result(result: MediaProbeResult) -> str:
    status = "probed" if result["success"] else "failed"
    lines = [
        f"Media: {result['media_path']}",
        f"Status: {status}",
    ]
    metadata = result["metadata"]
    if result["success"] and metadata is not None:
        duration = metadata.get("duration_seconds")
        lines.append(f"Duration: {duration}s")
        video = metadata.get("video")
        if isinstance(video, dict):
            width = video.get("width")
            height = video.get("height")
            frame_rate = video.get("frame_rate")
            lines.append(f"Video: {width}x{height} at {frame_rate} fps")
        audio = metadata.get("audio")
        if isinstance(audio, dict):
            codec = audio.get("codec")
            sample_rate = audio.get("sample_rate")
            channels = audio.get("channels")
            lines.append(f"Audio: {codec}, {sample_rate} Hz, {channels} channels")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _record_probe_success(project_dir: Path, data: ProjectState, media_path: str, metadata: JsonObject) -> None:
    media = _ensure_media(data)
    media["raw"] = metadata
    resolve_matching_failures(data, stage=MEDIA_PROBE_STAGE, scope=media_failure_scope(media_path))
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_probe_failure(project_dir: Path, data: ProjectState, media_path: str, errors: list[str]) -> None:
    context: JsonObject = {"media_path": media_path}
    record_failure(
        data,
        stage=MEDIA_PROBE_STAGE,
        scope=media_failure_scope(media_path),
        errors=errors,
        recommended_next_action=MEDIA_PROBE_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _ensure_media(data: ProjectState) -> JsonObject:
    media = data.get("media")
    if media is None:
        media = {}
        data["media"] = media
    return media


def _object_field(data: dict[str, object], key: str) -> dict[str, object] | None:
    value = data.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _stream_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    streams: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            streams.append(cast(dict[str, object], item))
    return streams


def _first_stream(streams: list[dict[str, object]], codec_type: str) -> dict[str, object] | None:
    for stream in streams:
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _string_field(data: dict[str, object], key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _int_from_field(data: dict[str, object], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _float_from_field(data: dict[str, object], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _frame_rate(stream: dict[str, object]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if not isinstance(value, str) or not value or value == "0/0":
            continue
        try:
            rate = Fraction(value)
        except ValueError:
            continue
        if rate > 0:
            return round(float(rate), 3)
    return None
