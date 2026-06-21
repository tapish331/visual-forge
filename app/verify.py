"""Verify the composed final video against Visual Forge output targets."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import (
    FRESHNESS_CURRENT,
    FRESHNESS_MISSING,
    FRESHNESS_NOT_CREATED,
    FRESHNESS_STALE,
    FRESHNESS_UNVERIFIED,
    REASON_ARTIFACT_MISSING,
    REASON_FINGERPRINT_MISMATCH,
    REASON_FINGERPRINT_MISSING,
    REASON_METADATA_MISSING,
    REASON_UPSTREAM_STALE,
    FreshnessResult,
    atomic_write_json,
    fingerprint_from_json,
    freshness,
    is_current,
    sha256_fingerprint,
    stat_fingerprint,
)
from .compose import build_final_freshness
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .media_probe import resolve_ffprobe
from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for, utc_now_iso, write_project


VERIFY_FINAL_STAGE = "final_verify"
VERIFY_FINAL_SCOPE = "final:final.mp4"
VERIFY_FINAL_RECOMMENDED_NEXT_ACTION = (
    "Fix final composition or output encoding, then rerun verify-final."
)
VERIFY_REPORT_PATH = "verification/final.json"
VERIFY_METHOD = "ffprobe_youtube_1080p_sdr_v0"
FRAME_RATE_TOLERANCE = 0.01
DURATION_TOLERANCE_SECONDS = 0.25
BITRATE_WARNING_FRACTION = 0.25
LOW_FPS_VIDEO_BITRATE = 8_000_000
HIGH_FPS_VIDEO_BITRATE = 12_000_000
AUDIO_BITRATE = 384_000


class VerifyFinalResult(TypedDict):
    project_dir: str
    source_path: str
    report_path: str
    success: bool
    passed: bool
    error_count: int
    warning_count: int
    errors: list[str]
    warnings: list[str]


def verify_project_final(project_dir: Path) -> VerifyFinalResult:
    data = load_project(project_dir)
    source_path = data["project"]["final"]
    source_file = artifact_path(project_dir, data, source_path)
    report_file = artifact_path(project_dir, data, VERIFY_REPORT_PATH)
    errors: list[str] = []
    warnings: list[str] = []
    actual: JsonObject = {}

    final_freshness = build_final_freshness(project_dir, data)
    if not is_current(final_freshness):
        reason = final_freshness["reason"]
        detail = f" ({reason})" if reason is not None else ""
        errors.append(f"Final video is not current: {final_freshness['state']}{detail}")

    expected, expected_errors = _expected_output(data)
    errors.extend(expected_errors)

    if not errors:
        raw, probe_errors = _probe_final(source_file)
        errors.extend(probe_errors)
        if raw is not None:
            actual, normalize_errors = _normalize_probe(raw)
            errors.extend(normalize_errors)
            if not normalize_errors:
                validation_errors, validation_warnings = _validate_output(expected, actual)
                errors.extend(validation_errors)
                warnings.extend(validation_warnings)

    verified_at = utc_now_iso()
    source_fingerprint = _source_fingerprint(source_file)
    report: JsonObject = {
        "schema_version": 1,
        "source": source_path,
        "source_fingerprint": source_fingerprint,
        "method": VERIFY_METHOD,
        "passed": not errors,
        "errors": _json_string_list(errors),
        "warnings": _json_string_list(warnings),
        "expected": expected,
        "actual": actual,
        "verified_at": verified_at,
    }

    report_written = False
    try:
        atomic_write_json(report_file, report)
        report_written = True
    except (OSError, TypeError, ValueError) as exc:
        errors.append(f"Could not write final verification report: {exc}")

    success = not errors and report_written
    if report_written:
        metadata = _verification_metadata(
            source_path,
            source_fingerprint,
            report_file,
            success,
            len(errors),
            len(warnings),
            verified_at,
        )
        _record_verification_result(project_dir, data, metadata, errors, success)
    else:
        _record_verification_failure(project_dir, data, errors)

    return {
        "project_dir": str(project_dir),
        "source_path": str(source_file),
        "report_path": str(report_file),
        "success": success,
        "passed": success,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def build_verification_freshness(project_dir: Path, data: ProjectState) -> FreshnessResult:
    metadata = _final_verification_metadata(data)
    if metadata is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)

    report_path = _string_field(metadata, "path") or VERIFY_REPORT_PATH
    report_file = artifact_path(project_dir, data, report_path)
    if not report_file.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    if _string_field(metadata, "status") != "passed":
        return freshness(FRESHNESS_STALE, "verification_failed")

    source_expected = fingerprint_from_json(metadata.get("source_fingerprint"))
    artifact_expected = fingerprint_from_json(metadata.get("artifact_fingerprint"))
    if source_expected is None or artifact_expected is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    if not is_current(build_final_freshness(project_dir, data)):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)

    final_file = artifact_path(project_dir, data, data["project"]["final"])
    try:
        if source_expected != stat_fingerprint(final_file):
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
        if artifact_expected != sha256_fingerprint(report_file):
            return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    return freshness(FRESHNESS_CURRENT, None)


def format_verify_final_result(result: VerifyFinalResult) -> str:
    lines = [
        f"Final: {result['source_path']}",
        f"Verification: {'passed' if result['passed'] else 'failed'}",
        f"Errors: {result['error_count']}",
        f"Warnings: {result['warning_count']}",
        f"Report: {result['report_path']}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    for warning in result["warnings"]:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def _probe_final(source_file: Path) -> tuple[dict[str, object] | None, list[str]]:
    if not source_file.is_file():
        return None, [f"Missing final video: {source_file}"]
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
        str(source_file),
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
    return cast(dict[str, object], raw), []


def _normalize_probe(raw: dict[str, object]) -> tuple[JsonObject, list[str]]:
    format_data = _object_field_raw(raw, "format")
    streams = _stream_list(raw.get("streams"))
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    errors: list[str] = []
    if format_data is None:
        errors.append("ffprobe output is missing format data")
    if len(video_streams) != 1:
        errors.append(f"Expected exactly one video stream, found {len(video_streams)}")
    if len(audio_streams) != 1:
        errors.append(f"Expected exactly one audio stream, found {len(audio_streams)}")

    video = video_streams[0] if len(video_streams) == 1 else {}
    audio = audio_streams[0] if len(audio_streams) == 1 else {}
    actual: JsonObject = {
        "format": {
            "name": _string_or_none(format_data, "format_name"),
            "duration_seconds": _float_from_field(format_data, "duration"),
            "size_bytes": _int_from_field(format_data, "size"),
            "bit_rate": _int_from_field(format_data, "bit_rate"),
        },
        "stream_counts": {
            "video": len(video_streams),
            "audio": len(audio_streams),
        },
        "video": {
            "codec": _string_or_none(video, "codec_name"),
            "profile": _string_or_none(video, "profile"),
            "width": _int_from_field(video, "width"),
            "height": _int_from_field(video, "height"),
            "pixel_format": _string_or_none(video, "pix_fmt"),
            "field_order": _string_or_none(video, "field_order"),
            "frame_rate": _frame_rate(video),
            "display_aspect_ratio": _string_or_none(video, "display_aspect_ratio"),
            "sample_aspect_ratio": _string_or_none(video, "sample_aspect_ratio"),
            "color_space": _string_or_none(video, "color_space"),
            "color_transfer": _string_or_none(video, "color_transfer"),
            "color_primaries": _string_or_none(video, "color_primaries"),
            "duration_seconds": _float_from_field(video, "duration"),
            "bit_rate": _int_from_field(video, "bit_rate"),
        },
        "audio": {
            "codec": _string_or_none(audio, "codec_name"),
            "profile": _string_or_none(audio, "profile"),
            "sample_rate": _int_from_field(audio, "sample_rate"),
            "channels": _int_from_field(audio, "channels"),
            "channel_layout": _string_or_none(audio, "channel_layout"),
            "duration_seconds": _float_from_field(audio, "duration"),
            "bit_rate": _int_from_field(audio, "bit_rate"),
        },
    }
    return actual, errors


def _expected_output(data: ProjectState) -> tuple[JsonObject, list[str]]:
    errors: list[str] = []
    frame_rate = _source_frame_rate(data)
    composed_duration = _composed_duration(data)
    timeline = data.get("timeline")
    chunking = data.get("chunking")
    duration = _json_number(timeline.get("duration_seconds")) if timeline is not None else None
    coverage = chunking.get("coverage") if chunking is not None else None
    if frame_rate is None or frame_rate <= 0:
        errors.append("Missing valid source frame rate metadata")
    if duration is None or duration <= 0:
        errors.append("Missing valid canonical timeline duration metadata")
    if composed_duration is None or composed_duration <= 0:
        errors.append("Missing valid composed duration metadata")
    elif duration is not None and abs(composed_duration - duration) > DURATION_TOLERANCE_SECONDS:
        errors.append(
            f"Composed duration does not cover canonical timeline: expected {duration}s, got {composed_duration}s"
        )
    if not isinstance(coverage, dict) or coverage.get("complete") is not True:
        errors.append("Chunk timeline coverage is incomplete")
    video_bitrate = HIGH_FPS_VIDEO_BITRATE if (frame_rate or 0) > 30 else LOW_FPS_VIDEO_BITRATE
    expected: JsonObject = {
        "container": "mp4",
        "duration_seconds": duration,
        "composed_duration_seconds": composed_duration,
        "timeline": {
            "policy": timeline.get("policy") if timeline is not None else None,
            "start": timeline.get("start") if timeline is not None else None,
            "end": timeline.get("end") if timeline is not None else None,
            "coverage": coverage,
        },
        "video": {
            "codec": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "pixel_format": "yuv420p",
            "field_order": "progressive",
            "frame_rate": frame_rate,
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "bit_rate": video_bitrate,
        },
        "audio": {
            "codec": "aac",
            "profile": "LC",
            "sample_rate": 48000,
            "channels": 2,
            "channel_layout": "stereo",
            "bit_rate": AUDIO_BITRATE,
        },
        "tolerances": {
            "frame_rate": FRAME_RATE_TOLERANCE,
            "duration_seconds": DURATION_TOLERANCE_SECONDS,
            "bit_rate_fraction": BITRATE_WARNING_FRACTION,
        },
    }
    return expected, errors


def _validate_output(expected: JsonObject, actual: JsonObject) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    expected_video = _json_object(expected, "video")
    expected_audio = _json_object(expected, "audio")
    actual_format = _json_object(actual, "format")
    actual_video = _json_object(actual, "video")
    actual_audio = _json_object(actual, "audio")

    format_name = _string_field(actual_format, "name") or ""
    if "mp4" not in {part.casefold() for part in format_name.split(",")}:
        errors.append(f"Container is not MP4-compatible: {format_name or 'missing'}")

    _require_equal(errors, "Video codec", actual_video.get("codec"), expected_video.get("codec"))
    _require_equal(errors, "Video profile", actual_video.get("profile"), expected_video.get("profile"))
    _require_equal(errors, "Video width", actual_video.get("width"), expected_video.get("width"))
    _require_equal(errors, "Video height", actual_video.get("height"), expected_video.get("height"))
    _require_equal(errors, "Pixel format", actual_video.get("pixel_format"), expected_video.get("pixel_format"))
    _require_equal(errors, "Scan type", actual_video.get("field_order"), expected_video.get("field_order"))
    _require_equal(errors, "Color space", actual_video.get("color_space"), expected_video.get("color_space"))
    _require_equal(errors, "Color transfer", actual_video.get("color_transfer"), expected_video.get("color_transfer"))
    _require_equal(errors, "Color primaries", actual_video.get("color_primaries"), expected_video.get("color_primaries"))

    actual_frame_rate = _json_number(actual_video.get("frame_rate"))
    expected_frame_rate = _json_number(expected_video.get("frame_rate"))
    if actual_frame_rate is None or expected_frame_rate is None:
        errors.append("Frame rate is missing or invalid")
    elif abs(actual_frame_rate - expected_frame_rate) > FRAME_RATE_TOLERANCE:
        errors.append(f"Frame rate mismatch: expected {expected_frame_rate}, got {actual_frame_rate}")

    _optional_label(
        errors,
        warnings,
        "Display aspect ratio",
        actual_video.get("display_aspect_ratio"),
        "16:9",
    )
    _optional_label(
        errors,
        warnings,
        "Sample aspect ratio",
        actual_video.get("sample_aspect_ratio"),
        "1:1",
    )

    _require_equal(errors, "Audio codec", actual_audio.get("codec"), expected_audio.get("codec"))
    _require_equal(errors, "Audio profile", actual_audio.get("profile"), expected_audio.get("profile"))
    _require_equal(errors, "Audio sample rate", actual_audio.get("sample_rate"), expected_audio.get("sample_rate"))
    _require_equal(errors, "Audio channels", actual_audio.get("channels"), expected_audio.get("channels"))
    _optional_label(errors, warnings, "Audio channel layout", actual_audio.get("channel_layout"), "stereo")

    expected_duration = _json_number(expected.get("duration_seconds"))
    format_duration = _positive_duration(errors, "Final format duration", actual_format.get("duration_seconds"))
    video_duration = _positive_duration(errors, "Video stream duration", actual_video.get("duration_seconds"))
    audio_duration = _positive_duration(errors, "Audio stream duration", actual_audio.get("duration_seconds"))
    if expected_duration is not None and format_duration is not None:
        if abs(format_duration - expected_duration) > DURATION_TOLERANCE_SECONDS:
            errors.append(
                f"Final duration mismatch: expected {expected_duration}s, got {format_duration}s"
            )
    if video_duration is not None and audio_duration is not None:
        drift = abs(video_duration - audio_duration)
        if drift > DURATION_TOLERANCE_SECONDS:
            errors.append(f"Video/audio duration drift is {round(drift, 3)}s")

    _bitrate_warning(
        warnings,
        "Video bitrate",
        actual_video.get("bit_rate"),
        expected_video.get("bit_rate"),
    )
    _bitrate_warning(
        warnings,
        "Audio bitrate",
        actual_audio.get("bit_rate"),
        expected_audio.get("bit_rate"),
    )
    return errors, warnings


def _verification_metadata(
    source_path: str,
    source_fingerprint: JsonObject,
    report_file: Path,
    success: bool,
    error_count: int,
    warning_count: int,
    verified_at: str,
) -> JsonObject:
    return {
        "path": VERIFY_REPORT_PATH,
        "source": source_path,
        "method": VERIFY_METHOD,
        "status": "passed" if success else "failed",
        "error_count": error_count,
        "warning_count": warning_count,
        "verified_at": verified_at,
        "source_fingerprint": source_fingerprint,
        "artifact_fingerprint": sha256_fingerprint(report_file),
    }


def _record_verification_result(
    project_dir: Path,
    data: ProjectState,
    metadata: JsonObject,
    errors: list[str],
    success: bool,
) -> None:
    verification = _ensure_verification(data)
    verification["final"] = metadata
    if success:
        resolve_matching_failures(data, stage=VERIFY_FINAL_STAGE, scope=VERIFY_FINAL_SCOPE)
    else:
        _add_verification_failure(data, errors)
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_verification_failure(project_dir: Path, data: ProjectState, errors: list[str]) -> None:
    _add_verification_failure(data, errors)
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _add_verification_failure(data: ProjectState, errors: list[str]) -> None:
    record_failure(
        data,
        stage=VERIFY_FINAL_STAGE,
        scope=VERIFY_FINAL_SCOPE,
        errors=errors,
        recommended_next_action=VERIFY_FINAL_RECOMMENDED_NEXT_ACTION,
        context={"source_path": data["project"]["final"], "report_path": VERIFY_REPORT_PATH},
    )


def _ensure_verification(data: ProjectState) -> JsonObject:
    verification = data.get("verification")
    if verification is None:
        verification = {}
        data["verification"] = verification
    return verification


def _final_verification_metadata(data: ProjectState) -> JsonObject | None:
    verification = data.get("verification")
    if not isinstance(verification, dict):
        return None
    final = verification.get("final")
    if isinstance(final, dict):
        return cast(JsonObject, final)
    return None


def _source_frame_rate(data: ProjectState) -> float | None:
    media = data.get("media")
    if not isinstance(media, dict):
        return None
    raw = media.get("raw")
    if not isinstance(raw, dict):
        return None
    video = raw.get("video")
    if not isinstance(video, dict):
        return None
    return _json_number(video.get("frame_rate"))


def _composed_duration(data: ProjectState) -> float | None:
    renders = data.get("renders")
    if not isinstance(renders, dict):
        return None
    final = renders.get("final")
    if not isinstance(final, dict):
        return None
    return _json_number(final.get("duration_seconds"))


def _source_fingerprint(source_file: Path) -> JsonObject:
    try:
        return stat_fingerprint(source_file) if source_file.is_file() else {}
    except OSError:
        return {}


def _require_equal(errors: list[str], label: str, actual: JsonValue | None, expected: JsonValue | None) -> None:
    if actual != expected:
        errors.append(f"{label} mismatch: expected {expected}, got {actual}")


def _optional_label(
    errors: list[str],
    warnings: list[str],
    label: str,
    actual: JsonValue | None,
    expected: str,
) -> None:
    if actual is None:
        warnings.append(f"{label} is not reported")
    elif actual != expected:
        errors.append(f"{label} mismatch: expected {expected}, got {actual}")


def _positive_duration(errors: list[str], label: str, value: JsonValue | None) -> float | None:
    number = _json_number(value)
    if number is None or number <= 0:
        errors.append(f"{label} is missing or invalid")
        return None
    return number


def _bitrate_warning(
    warnings: list[str],
    label: str,
    actual_value: JsonValue | None,
    expected_value: JsonValue | None,
) -> None:
    actual = _json_number(actual_value)
    expected = _json_number(expected_value)
    if actual is None or expected is None or expected <= 0:
        warnings.append(f"{label} is not reported")
        return
    difference = abs(actual - expected) / expected
    if difference > BITRATE_WARNING_FRACTION:
        warnings.append(f"{label} differs from target: expected {int(expected)}, got {int(actual)}")


def _json_object(data: JsonObject, key: str) -> JsonObject:
    value = data.get(key)
    if isinstance(value, dict):
        return cast(JsonObject, value)
    return {}


def _json_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _object_field_raw(data: dict[str, object], key: str) -> dict[str, object] | None:
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


def _string_or_none(data: dict[str, object] | None, key: str) -> str | None:
    if data is None:
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _string_field(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _int_from_field(data: dict[str, object] | None, key: str) -> int | None:
    if data is None:
        return None
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


def _float_from_field(data: dict[str, object] | None, key: str) -> float | None:
    if data is None:
        return None
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
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return round(float(rate), 3)
    return None


def _json_string_list(values: list[str]) -> list[JsonValue]:
    output: list[JsonValue] = []
    for value in values:
        output.append(value)
    return output
