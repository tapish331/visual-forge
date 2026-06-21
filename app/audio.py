"""Narration audio extraction with FFmpeg."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import (
    build_pipeline_freshness,
    is_current,
    replace_artifact,
    stat_fingerprint,
    temporary_artifact_path,
)
from .failures import media_failure_scope, record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, ProjectState, load_project, project_file_for, utc_now_iso, write_project


FFMPEG_ENV_VAR = "VISUAL_FORGE_FFMPEG"
AUDIO_DIR = "audio"
NARRATION_AUDIO = "narration.wav"
AUDIO_EXTRACT_STAGE = "audio_extract"
AUDIO_EXTRACT_RECOMMENDED_NEXT_ACTION = (
    "Fix the configured raw video, media probe metadata, or FFmpeg availability, then rerun extract-audio."
)


class AudioExtractResult(TypedDict):
    project_dir: str
    source_path: str
    output_path: str
    success: bool
    metadata: JsonObject | None
    errors: list[str]


def extract_project_audio(project_dir: Path) -> AudioExtractResult:
    data = load_project(project_dir)
    source_path = data["project"]["video"]
    output_path = Path(AUDIO_DIR) / NARRATION_AUDIO
    source_file = project_dir / source_path
    output_file = artifact_path(project_dir, data, output_path)

    metadata, errors = extract_narration_audio(
        data,
        project_dir,
        source_file,
        output_file,
        source_path,
        output_path.as_posix(),
    )
    result: AudioExtractResult = {
        "project_dir": str(project_dir),
        "source_path": source_path,
        "output_path": str(output_file),
        "success": not errors and metadata is not None,
        "metadata": metadata,
        "errors": errors,
    }

    if result["success"] and metadata is not None:
        _record_audio_success(project_dir, data, output_path.as_posix(), metadata)
    else:
        _record_audio_failure(project_dir, data, source_path, output_path.as_posix(), errors)
    return result


def extract_narration_audio(
    data: ProjectState,
    project_dir: Path,
    source_file: Path,
    output_file: Path,
    source_path: str,
    output_path: str,
) -> tuple[JsonObject | None, list[str]]:
    errors = validate_audio_prerequisites(data, project_dir, source_file)
    if errors:
        return None, errors

    ffmpeg, ffmpeg_error = resolve_ffmpeg()
    if ffmpeg is None:
        return None, [ffmpeg_error]

    with temporary_artifact_path(output_file) as temporary_output:
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(source_file),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(temporary_output),
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
            return None, [f"Could not run ffmpeg: {exc}"]

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            return None, [f"ffmpeg failed with exit code {completed.returncode}{detail}"]

        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            return None, [f"FFmpeg completed but did not create {output_path}"]
        try:
            replace_artifact(temporary_output, output_file)
        except OSError as exc:
            return None, [f"Could not replace {output_path}: {exc}"]

    raw_media = _raw_media(data)
    duration = _number_field(raw_media, "duration_seconds")
    metadata: JsonObject = {
        "path": output_path,
        "source": source_path,
        "source_fingerprint": stat_fingerprint(source_file),
        "artifact_fingerprint": stat_fingerprint(output_file),
        "format": "wav",
        "codec": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
        "duration_seconds": duration,
        "size_bytes": output_file.stat().st_size,
        "extracted_at": utc_now_iso(),
    }
    return metadata, []


def validate_audio_prerequisites(data: ProjectState, project_dir: Path, source_file: Path) -> list[str]:
    errors: list[str] = []
    raw_media = _raw_media(data)
    if raw_media is None:
        errors.append("Missing media.raw probe metadata. Run probe before extract-audio.")
    elif _number_field(raw_media, "duration_seconds") is None:
        errors.append("media.raw is missing duration_seconds. Rerun probe.")
    elif not is_current(build_pipeline_freshness(project_dir, data)["raw"]):
        errors.append("media.raw is stale or unverified. Rerun probe before extract-audio.")

    if not source_file.exists():
        errors.append(f"Missing media file: {source_file.name}")
    elif not source_file.is_file():
        errors.append(f"Media path is not a file: {source_file.name}")
    return errors


def resolve_ffmpeg() -> tuple[str | None, str]:
    configured = os.environ.get(FFMPEG_ENV_VAR)
    if configured is not None and configured.strip():
        configured_path = configured.strip()
        if Path(configured_path).exists():
            return configured_path, ""
        return None, f"ffmpeg not found at {FFMPEG_ENV_VAR}: {configured_path}"

    discovered = shutil.which("ffmpeg")
    if discovered is None:
        return None, "ffmpeg not found. Set VISUAL_FORGE_FFMPEG or add ffmpeg to PATH."
    return discovered, ""


def format_audio_extract_result(result: AudioExtractResult) -> str:
    status = "extracted" if result["success"] else "failed"
    lines = [
        f"Audio: {result['output_path']}",
        f"Source: {result['source_path']}",
        f"Status: {status}",
    ]
    metadata = result["metadata"]
    if result["success"] and metadata is not None:
        lines.append(f"Format: {metadata.get('format')}")
        lines.append(f"Sample rate: {metadata.get('sample_rate')} Hz")
        lines.append(f"Channels: {metadata.get('channels')}")
        lines.append(f"Size: {metadata.get('size_bytes')} bytes")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _record_audio_success(
    project_dir: Path,
    data: ProjectState,
    output_path: str,
    metadata: JsonObject,
) -> None:
    media = _ensure_media(data)
    audio = _ensure_audio(media)
    audio["narration"] = metadata
    resolve_matching_failures(data, stage=AUDIO_EXTRACT_STAGE, scope=media_failure_scope(output_path))
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_audio_failure(
    project_dir: Path,
    data: ProjectState,
    source_path: str,
    output_path: str,
    errors: list[str],
) -> None:
    context: JsonObject = {
        "source_path": source_path,
        "output_path": output_path,
    }
    record_failure(
        data,
        stage=AUDIO_EXTRACT_STAGE,
        scope=media_failure_scope(output_path),
        errors=errors,
        recommended_next_action=AUDIO_EXTRACT_RECOMMENDED_NEXT_ACTION,
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


def _ensure_audio(media: JsonObject) -> JsonObject:
    audio = media.get("audio")
    if not isinstance(audio, dict):
        audio = {}
        media["audio"] = audio
    return cast(JsonObject, audio)


def _raw_media(data: ProjectState) -> JsonObject | None:
    media = data.get("media")
    if media is None:
        return None
    raw = media.get("raw")
    if isinstance(raw, dict):
        return cast(JsonObject, raw)
    return None


def _number_field(data: JsonObject | None, key: str) -> int | float | None:
    if data is None:
        return None
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None
