"""Create and inspect resumable video chunks from alignment output."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import build_pipeline_freshness, is_current
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState, load_project
from .project import project_file_for, utc_now_iso, write_project
from .timeline import (
    build_chunking_metadata,
    build_full_raw_timeline,
    build_timeline_chunk_freshness,
)


CHUNKS_STAGE = "chunks"
CHUNKS_SCOPE = "chunks:project"
CHUNKS_RECOMMENDED_NEXT_ACTION = (
    "Fix the alignment checkpoint or chunking options, then rerun create-chunks."
)
DEFAULT_TARGET_SECONDS = 180.0
DEFAULT_MIN_SECONDS = 90.0
DEFAULT_MAX_SECONDS = 240.0
ALIGNMENT_DEFAULT_PATH = "alignment/script_alignment.json"
CHUNK_STATUS_NEW = "new"
CAMERA_ONLY_STAGE = "camera_only"
CAMERA_ONLY_RECOMMENDED_NEXT_ACTION = (
    "Remove or move chunk visuals, ensure chunking is current, then rerun approve-camera-only."
)


class CreateChunksResult(TypedDict):
    project_dir: str
    success: bool
    chunks_created: int
    chunks: list[JsonObject]
    timeline: JsonObject | None
    chunking: JsonObject | None
    errors: list[str]


class ChunksSummary(TypedDict):
    project_dir: str
    total: int
    by_status: dict[str, int]
    chunks: list[JsonObject]
    timeline: JsonObject
    chunking: JsonObject
    freshness: JsonObject


class CameraOnlyResult(TypedDict):
    project_dir: str
    chunk_id: str
    success: bool
    visual_mode: str | None
    status: str | None
    errors: list[str]


def create_chunks(
    project_dir: Path,
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    force: bool = False,
) -> CreateChunksResult:
    data = load_project(project_dir)
    alignment_path = _alignment_path(data)
    errors = validate_chunk_options(target_seconds, min_seconds, max_seconds)

    if data["chunks"] and not force:
        errors.append("Project already has chunks. Use --force to replace them.")

    pipeline_freshness = build_pipeline_freshness(project_dir, data)
    raw_freshness = pipeline_freshness["raw"]
    if not is_current(raw_freshness):
        state = raw_freshness["state"]
        reason = raw_freshness["reason"]
        detail = f" ({reason})" if reason is not None else ""
        errors.append(f"Raw media is not current: {state}{detail}. Rerun probe before create-chunks.")

    alignment_freshness = pipeline_freshness["alignment"]
    if not is_current(alignment_freshness):
        state = alignment_freshness["state"]
        reason = alignment_freshness["reason"]
        detail = f" ({reason})" if reason is not None else ""
        errors.append(f"Alignment is not current: {state}{detail}. Rerun align before create-chunks.")

    artifact: JsonObject | None = None
    if not errors:
        artifact, read_errors = _read_alignment_artifact(artifact_path(project_dir, data, alignment_path))
        errors.extend(read_errors)

    chunks: list[JsonObject] = []
    timeline: JsonObject | None = None
    chunking: JsonObject | None = None
    visual_validation_failed = False
    options: JsonObject = {
        "target_seconds": target_seconds,
        "min_seconds": min_seconds,
        "max_seconds": max_seconds,
    }
    if not errors and artifact is not None:
        try:
            timeline = build_full_raw_timeline(project_dir, data)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
        raw_blocks = artifact.get("blocks")
        if not errors and timeline is not None and isinstance(raw_blocks, list):
            chunks, errors = build_chunk_records(
                raw_blocks,
                target_seconds=target_seconds,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
                timeline_start=_required_number(timeline, "start"),
                timeline_end=_required_number(timeline, "end"),
                created_at=utc_now_iso(),
            )
        elif not errors and not isinstance(raw_blocks, list):
            errors.append(f"Alignment artifact {alignment_path} must contain a blocks list.")

    if not errors and timeline is not None:
        _apply_visual_modes(chunks, data.get("visuals", []))
        visual_errors = _existing_visual_errors(data, chunks)
        visual_validation_failed = bool(visual_errors)
        errors.extend(visual_errors)
    if not errors and timeline is not None:
        try:
            chunking = build_chunking_metadata(data, timeline, chunks, options)
        except ValueError as exc:
            errors.append(str(exc))

    result: CreateChunksResult = {
        "project_dir": str(project_dir),
        "success": not errors,
        "chunks_created": len(chunks) if not errors else 0,
        "chunks": chunks if not errors else [],
        "timeline": timeline if not errors else None,
        "chunking": chunking if not errors else None,
        "errors": errors,
    }
    if result["success"] and timeline is not None and chunking is not None:
        _record_chunks_success(project_dir, data, chunks, timeline, chunking)
    elif not visual_validation_failed:
        _record_chunks_failure(
            project_dir,
            data,
            errors,
            alignment_path=alignment_path,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
        )
    return result


def build_chunks_summary(project_dir: Path) -> ChunksSummary:
    data = load_project(project_dir)
    statuses: Counter[str] = Counter(_chunk_status(chunk) for chunk in data["chunks"])
    pipeline = build_pipeline_freshness(project_dir, data)
    timeline_freshness = build_timeline_chunk_freshness(project_dir, data, pipeline)
    return {
        "project_dir": str(project_dir),
        "total": len(data["chunks"]),
        "by_status": dict(sorted(statuses.items())),
        "chunks": data["chunks"],
        "timeline": data.get("timeline", {}),
        "chunking": data.get("chunking", {}),
        "freshness": cast(JsonObject, timeline_freshness),
    }


def approve_camera_only(project_dir: Path, chunk_id: str) -> CameraOnlyResult:
    data = load_project(project_dir)
    pipeline = build_pipeline_freshness(project_dir, data)
    timeline_freshness = build_timeline_chunk_freshness(project_dir, data, pipeline)
    errors: list[str] = []
    chunk = _find_chunk(data, chunk_id)
    if chunk is None:
        errors.append(f"Chunk not found: {chunk_id}")
    if not is_current(timeline_freshness["chunking"]):
        freshness_result = timeline_freshness["chunking"]
        reason = freshness_result["reason"]
        detail = f" ({reason})" if reason is not None else ""
        errors.append(f"Chunking is not current: {freshness_result['state']}{detail}")
    chunk_visuals = [visual for visual in data.get("visuals", []) if visual.get("chunk_id") == chunk_id]
    if chunk_visuals:
        errors.append(f"Chunk {chunk_id} has {len(chunk_visuals)} visual record(s); it cannot be camera-only.")

    if errors or chunk is None:
        result: CameraOnlyResult = {
            "project_dir": str(project_dir),
            "chunk_id": chunk_id,
            "success": False,
            "visual_mode": None,
            "status": None,
            "errors": errors,
        }
        _record_camera_only_failure(project_dir, data, result)
        return result

    now = utc_now_iso()
    chunk["visual_mode"] = "camera_only"
    chunk["status"] = "previewed"
    chunk["camera_only_at"] = now
    chunk.pop("visual_planning", None)
    chunk["updated_at"] = now
    resolve_matching_failures(data, stage=CAMERA_ONLY_STAGE, scope=f"chunk:{chunk_id}")
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "success": True,
        "visual_mode": "camera_only",
        "status": "previewed",
        "errors": [],
    }


def format_camera_only_result(result: CameraOnlyResult) -> str:
    lines = [
        f"Chunk: {result['chunk_id']}",
        f"Visual mode: {result['visual_mode'] or 'unchanged'}",
        f"Status: {result['status'] or 'failed'}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def validate_chunk_options(target_seconds: float, min_seconds: float, max_seconds: float) -> list[str]:
    errors: list[str] = []
    if not math.isfinite(target_seconds) or target_seconds <= 0:
        errors.append("target-seconds must be a finite number greater than 0")
    if not math.isfinite(min_seconds) or min_seconds <= 0:
        errors.append("min-seconds must be a finite number greater than 0")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        errors.append("max-seconds must be a finite number greater than 0")
    if errors:
        return errors
    if min_seconds > target_seconds:
        errors.append("min-seconds must be less than or equal to target-seconds")
    if target_seconds > max_seconds:
        errors.append("target-seconds must be less than or equal to max-seconds")
    return errors


def build_chunk_records(
    raw_blocks: Sequence[object],
    *,
    target_seconds: float,
    min_seconds: float,
    max_seconds: float,
    timeline_start: float,
    timeline_end: float,
    created_at: str,
) -> tuple[list[JsonObject], list[str]]:
    chunks: list[JsonObject] = []
    errors: list[str] = []
    current_block_ids: list[str] = []
    current_warning_ids: list[str] = []
    leading_warning_ids: list[str] = []
    current_start: float | None = None
    current_end: float | None = None
    last_aligned_end: float | None = None

    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            errors.append("Alignment artifact contains a non-object block.")
            continue
        block = cast(dict[str, object], raw_block)
        block_id = _block_id(block)
        if block_id is None:
            errors.append("Alignment block is missing a non-empty id.")
            continue
        status = block.get("status")
        if status == "aligned":
            start = _number_value(block.get("start"))
            end = _number_value(block.get("end"))
            if start is None or end is None or end <= start:
                errors.append(f"Aligned block {block_id} has invalid canonical timing.")
                continue
            if last_aligned_end is not None and start < last_aligned_end:
                errors.append(f"Aligned block {block_id} starts before the previous aligned block ends.")
                continue

            if current_start is not None and current_end is not None:
                current_duration = current_end - current_start
                projected_duration = end - current_start
                if projected_duration > max_seconds and current_duration >= min_seconds:
                    chunks.append(
                        _chunk_record(
                            len(chunks) + 1,
                            current_start,
                            current_end,
                            current_block_ids,
                            current_warning_ids,
                            created_at,
                        )
                    )
                    current_block_ids = []
                    current_warning_ids = []
                    current_start = None
                    current_end = None

            if current_start is None:
                current_start = start
                current_warning_ids = leading_warning_ids
                leading_warning_ids = []
            current_block_ids.append(block_id)
            current_end = end
            last_aligned_end = end

            duration = current_end - current_start
            if duration >= target_seconds and duration >= min_seconds:
                chunks.append(
                    _chunk_record(
                        len(chunks) + 1,
                        current_start,
                        current_end,
                        current_block_ids,
                        current_warning_ids,
                        created_at,
                    )
                )
                current_block_ids = []
                current_warning_ids = []
                current_start = None
                current_end = None
        elif status in {"needs_review", "unmatched"}:
            if current_block_ids:
                current_warning_ids.append(block_id)
            elif chunks:
                _append_warning_id(chunks[-1], block_id)
            else:
                leading_warning_ids.append(block_id)
        else:
            errors.append(f"Alignment block {block_id} has unsupported status: {status!r}.")

    if current_start is not None and current_end is not None and current_block_ids:
        chunks.append(
            _chunk_record(
                len(chunks) + 1,
                current_start,
                current_end,
                current_block_ids,
                current_warning_ids,
                created_at,
            )
        )

    if errors:
        return [], errors
    if not chunks:
        return [], ["Alignment artifact does not contain any aligned blocks with canonical timing."]
    first_start = _number_value(chunks[0].get("start"))
    last_end = _number_value(chunks[-1].get("end"))
    if first_start is None or last_end is None:
        return [], ["Generated chunk boundaries are invalid."]
    if first_start < timeline_start or last_end > timeline_end:
        return [], ["Aligned speech timing falls outside the canonical raw-video timeline."]
    _apply_contiguous_boundaries(chunks, timeline_start, timeline_end)
    return chunks, []


def format_create_chunks_result(result: CreateChunksResult) -> str:
    status = "created" if result["success"] else "failed"
    lines = [
        f"Chunks: {result['chunks_created']}",
        f"Status: {status}",
    ]
    for chunk in result["chunks"]:
        lines.append(_format_chunk_line(chunk))
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_chunks_summary(summary: ChunksSummary) -> str:
    lines = [f"Chunks: {summary['total']}"]
    for chunk in summary["chunks"]:
        lines.append(_format_chunk_line(chunk))
    return "\n".join(lines)


def format_seconds(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _record_chunks_success(
    project_dir: Path,
    data: ProjectState,
    chunks: list[JsonObject],
    timeline: JsonObject,
    chunking: JsonObject,
) -> None:
    data["chunks"] = chunks
    data["timeline"] = timeline
    data["chunking"] = chunking
    resolve_matching_failures(data, stage=CHUNKS_STAGE, scope=CHUNKS_SCOPE)
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_chunks_failure(
    project_dir: Path,
    data: ProjectState,
    errors: list[str],
    *,
    alignment_path: str,
    target_seconds: float,
    min_seconds: float,
    max_seconds: float,
) -> None:
    context: JsonObject = {
        "alignment_path": alignment_path,
        "target_seconds": target_seconds,
        "min_seconds": min_seconds,
        "max_seconds": max_seconds,
    }
    record_failure(
        data,
        stage=CHUNKS_STAGE,
        scope=CHUNKS_SCOPE,
        errors=errors,
        recommended_next_action=CHUNKS_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _read_alignment_artifact(path: Path) -> tuple[JsonObject | None, list[str]]:
    if not path.is_file():
        return None, [f"Missing alignment artifact: {path}"]
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON in alignment artifact: {exc.msg}"]
    except OSError as exc:
        return None, [f"Could not read alignment artifact: {exc}"]
    if not isinstance(payload, dict):
        return None, ["Alignment artifact must be a JSON object."]
    return cast(JsonObject, payload), []


def _alignment_path(data: ProjectState) -> str:
    alignment = data.get("alignment")
    if isinstance(alignment, dict):
        script = alignment.get("script")
        if isinstance(script, dict):
            value = script.get("path")
            if isinstance(value, str) and value:
                return value
    return ALIGNMENT_DEFAULT_PATH


def _chunk_record(
    index: int,
    start: float,
    end: float,
    block_ids: list[str],
    warning_ids: list[str],
    created_at: str,
) -> JsonObject:
    return {
        "id": f"chunk_{index:03d}",
        "start": round(start, 6),
        "end": round(end, 6),
        "status": CHUNK_STATUS_NEW,
        "visual_mode": "undecided",
        "alignment_block_ids": _json_string_list(block_ids),
        "warning_block_ids": _json_string_list(warning_ids),
        "created_at": created_at,
        "updated_at": created_at,
    }


def _append_warning_id(chunk: JsonObject, block_id: str) -> None:
    warning_ids = chunk.get("warning_block_ids")
    if isinstance(warning_ids, list):
        warning_ids.append(block_id)


def _format_chunk_line(chunk: JsonObject) -> str:
    chunk_id = _string_field(chunk, "id", "unknown")
    status = _string_field(chunk, "status", "unknown")
    start = _number_value(chunk.get("start")) or 0.0
    end = _number_value(chunk.get("end")) or 0.0
    aligned_count = _list_count(chunk.get("alignment_block_ids"))
    warning_count = _list_count(chunk.get("warning_block_ids"))
    return (
        f"- {chunk_id} {format_seconds(start)} -> {format_seconds(end)} "
        f"{status} mode={_string_field(chunk, 'visual_mode', 'undecided')} "
        f"aligned={aligned_count} warnings={warning_count}"
    )


def _apply_contiguous_boundaries(chunks: list[JsonObject], timeline_start: float, timeline_end: float) -> None:
    if len(chunks) == 1:
        chunks[0]["start"] = round(timeline_start, 6)
        chunks[0]["end"] = round(timeline_end, 6)
        return
    speech_starts = [_required_number(chunk, "start") for chunk in chunks]
    speech_ends = [_required_number(chunk, "end") for chunk in chunks]
    chunks[0]["start"] = round(timeline_start, 6)
    for index in range(len(chunks) - 1):
        boundary = round((speech_ends[index] + speech_starts[index + 1]) / 2.0, 6)
        chunks[index]["end"] = boundary
        chunks[index + 1]["start"] = boundary
    chunks[-1]["end"] = round(timeline_end, 6)


def _apply_visual_modes(chunks: list[JsonObject], visuals: list[JsonObject]) -> None:
    visual_chunk_ids = {
        chunk_id
        for visual in visuals
        if isinstance((chunk_id := visual.get("chunk_id")), str) and chunk_id
    }
    for chunk in chunks:
        chunk_id = chunk.get("id")
        chunk["visual_mode"] = "visuals" if chunk_id in visual_chunk_ids else "undecided"


def _existing_visual_errors(data: ProjectState, chunks: list[JsonObject]) -> list[str]:
    proposed = {
        chunk_id: chunk
        for chunk in chunks
        if isinstance((chunk_id := chunk.get("id")), str) and chunk_id
    }
    errors: list[str] = []
    for visual in data.get("visuals", []):
        chunk_id = visual.get("chunk_id")
        if chunk_id is None:
            continue
        visual_id = visual.get("id") if isinstance(visual.get("id"), str) else "unknown"
        if not isinstance(chunk_id, str) or chunk_id not in proposed:
            errors.append(f"Visual {visual_id} would reference missing chunk {chunk_id!r}.")
            continue
        start = _number_value(visual.get("start"))
        end = _number_value(visual.get("end"))
        chunk_start = _number_value(proposed[chunk_id].get("start"))
        chunk_end = _number_value(proposed[chunk_id].get("end"))
        if (
            start is None
            or end is None
            or chunk_start is None
            or chunk_end is None
            or start < chunk_start
            or end > chunk_end
        ):
            errors.append(f"Visual {visual_id} would fall outside proposed chunk {chunk_id}.")
    return errors


def _required_number(data: JsonObject, key: str) -> float:
    value = _number_value(data.get(key))
    if value is None:
        raise ValueError(f"Missing numeric {key}")
    return value


def _chunk_status(chunk: JsonObject) -> str:
    return _string_field(chunk, "status", "unknown")


def _find_chunk(data: ProjectState, chunk_id: str) -> JsonObject | None:
    for chunk in data["chunks"]:
        if chunk.get("id") == chunk_id:
            return chunk
    return None


def _record_camera_only_failure(
    project_dir: Path,
    data: ProjectState,
    result: CameraOnlyResult,
) -> None:
    record_failure(
        data,
        stage=CAMERA_ONLY_STAGE,
        scope=f"chunk:{result['chunk_id']}",
        errors=result["errors"],
        recommended_next_action=CAMERA_ONLY_RECOMMENDED_NEXT_ACTION,
        context={"chunk_id": result["chunk_id"]},
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _json_string_list(values: list[str]) -> list[JsonValue]:
    output: list[JsonValue] = []
    for value in values:
        output.append(value)
    return output


def _block_id(block: dict[str, object]) -> str | None:
    value = block.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def _number_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _string_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _list_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    return 0
