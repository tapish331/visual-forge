"""Render previewed chunks into MP4 video segments."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypedDict, cast

from PIL import Image
from PIL import UnidentifiedImageError

from .artifacts import build_pipeline_freshness, is_current, replace_artifact, stat_fingerprint, temporary_artifact_path
from .audio import resolve_ffmpeg
from .failures import chunk_failure_scope, record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for, utc_now_iso, write_project
from .render_freshness import (
    build_preview_freshness,
    build_visual_plan_fingerprint,
    preview_artifact_fingerprint,
)
from .timeline import build_timeline_chunk_freshness, chunk_visual_mode, current_chunk_plan_fingerprint


CHUNK_RENDER_STAGE = "chunk_render"
CHUNK_RENDER_RECOMMENDED_NEXT_ACTION = (
    "Fix the chunk preview, visual previews, media freshness, or FFmpeg availability, then rerun render-chunk."
)
CHUNK_STATUS_PREVIEWED = "previewed"
CHUNK_STATUS_RENDERED = "rendered"
CHUNK_RENDERABLE_STATUSES = {CHUNK_STATUS_PREVIEWED, CHUNK_STATUS_RENDERED}
RENDERS_DIR = Path("renders") / "chunks"
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
LOW_FPS_BITRATE = "8M"
HIGH_FPS_BITRATE = "12M"
FPS_THRESHOLD = 30.0


class RenderChunkResult(TypedDict):
    project_dir: str
    chunk_id: str
    output_path: str | None
    success: bool
    metadata: JsonObject | None
    errors: list[str]


class ChunkVisualForRender(TypedDict):
    visual_id: str
    preview_id: str
    start: float
    end: float
    relative_start: float
    relative_end: float
    preview_output: str
    preview_path: Path
    preview_fingerprint: JsonObject


def render_project_chunk(project_dir: Path, chunk_id: str) -> RenderChunkResult:
    data = load_project(project_dir)
    relative_output = chunk_render_output_for(chunk_id)
    output_file = artifact_path(project_dir, data, relative_output)

    chunk = _find_chunk(data, chunk_id)
    visuals: list[ChunkVisualForRender] = []
    errors: list[str] = []
    chunk_start: float | None = None
    chunk_end: float | None = None
    visual_mode: str | None = None

    if chunk is None:
        errors.append(f"Chunk not found: {chunk_id}")
    else:
        chunk_start = _number_field(chunk, "start")
        chunk_end = _number_field(chunk, "end")
        visual_mode = chunk_visual_mode(chunk, data.get("visuals", []))
        if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
            errors.append(f"Chunk record is invalid: {chunk_id}")
        if _string_field(chunk, "status") not in CHUNK_RENDERABLE_STATUSES:
            errors.append(f"Chunk must be previewed before render: {chunk_id}")
        if visual_mode == "undecided":
            errors.append(f"Chunk visual mode is undecided: {chunk_id}")

    errors.extend(_pipeline_errors(project_dir, data))
    raw_file = project_dir / data["project"]["video"]
    if not raw_file.is_file():
        errors.append(f"Missing media file: {data['project']['video']}")

    if not errors and chunk_start is not None and chunk_end is not None:
        visuals, visual_errors = _visuals_for_render(data, project_dir, chunk_id, chunk_start, chunk_end)
        errors.extend(visual_errors)
        if visual_mode == "visuals" and not visuals:
            errors.append(f"No previewed visuals found for chunk: {chunk_id}")
        if visual_mode == "camera_only" and visuals:
            errors.append(f"Camera-only chunk contains visual records: {chunk_id}")

    ffmpeg: str | None = None
    if not errors:
        ffmpeg, ffmpeg_error = resolve_ffmpeg()
        if ffmpeg is None:
            errors.append(ffmpeg_error)

    metadata: JsonObject | None = None
    if not errors and chunk is not None and chunk_start is not None and chunk_end is not None and ffmpeg is not None:
        duration = round(chunk_end - chunk_start, 6)
        render_errors = _run_ffmpeg_render(
            ffmpeg,
            raw_file,
            output_file,
            chunk_start,
            duration,
            visuals,
            frame_rate=_frame_rate(data),
        )
        errors.extend(render_errors)
        if not errors:
            metadata = _render_metadata(
                data,
                chunk_id,
                relative_output,
                duration,
                visuals,
                raw_file,
                output_file,
            )

    result: RenderChunkResult = {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "output_path": str(output_file),
        "success": not errors and metadata is not None,
        "metadata": metadata,
        "errors": errors,
    }
    if result["success"] and metadata is not None and chunk is not None:
        _record_render_success(project_dir, data, chunk, metadata)
    else:
        _record_render_failure(project_dir, data, chunk_id, str(output_file), errors)
    return result


def chunk_render_output_for(chunk_id: str) -> Path:
    return RENDERS_DIR / f"{chunk_id}.mp4"


def format_render_chunk_result(result: RenderChunkResult) -> str:
    status = "rendered" if result["success"] else "failed"
    lines = [
        f"Chunk: {result['chunk_id']}",
        f"Status: {status}",
    ]
    if result["output_path"] is not None:
        lines.append(f"Output: {result['output_path']}")
    metadata = result["metadata"]
    if result["success"] and metadata is not None:
        lines.append(f"Duration: {metadata.get('duration_seconds')}s")
        lines.append(f"Visuals: {len(_string_list(metadata.get('visual_ids')))}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _run_ffmpeg_render(
    ffmpeg: str,
    raw_file: Path,
    output_file: Path,
    chunk_start: float,
    duration: float,
    visuals: list[ChunkVisualForRender],
    *,
    frame_rate: float | None,
) -> list[str]:
    bitrate = HIGH_FPS_BITRATE if (frame_rate or FPS_THRESHOLD) > FPS_THRESHOLD else LOW_FPS_BITRATE
    filter_complex, final_label, audio_label = _filter_complex(visuals, duration)
    command = [
        ffmpeg,
        "-y",
        "-ss",
        _ffmpeg_seconds(chunk_start),
        "-i",
        str(raw_file),
    ]
    for visual in visuals:
        command.extend(["-loop", "1"])
        if frame_rate is not None and frame_rate > 0:
            command.extend(["-framerate", _ffmpeg_seconds(frame_rate)])
        command.extend(["-i", str(visual["preview_path"])])
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            final_label,
            "-map",
            audio_label,
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-b:v",
            bitrate,
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-b:a",
            "384k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-t",
            _ffmpeg_seconds(duration),
        ]
    )
    if frame_rate is not None and frame_rate > 0:
        command.extend(["-r", _ffmpeg_seconds(frame_rate)])

    with temporary_artifact_path(output_file) as temporary_output:
        command.append(str(temporary_output))
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
            return [f"Could not run ffmpeg: {exc}"]

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            return [f"ffmpeg failed with exit code {completed.returncode}{detail}"]
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            return [f"FFmpeg completed but did not create {output_file.name}"]
        try:
            replace_artifact(temporary_output, output_file)
        except OSError as exc:
            return [f"Could not replace {output_file.name}: {exc}"]
    return []


def _filter_complex(visuals: list[ChunkVisualForRender], duration: float) -> tuple[str, str, str]:
    duration_value = _ffmpeg_seconds(duration)
    parts = [
        (
            f"[0:v]trim=duration={duration_value},setpts=PTS-STARTPTS,"
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v0]"
        ),
        f"[0:a]atrim=duration={duration_value},asetpts=PTS-STARTPTS[a0]",
    ]
    current_label = "[v0]"
    for index, visual in enumerate(visuals, start=1):
        overlay_label = f"[ov{index}]"
        next_label = f"[v{index}]"
        parts.append(
            f"[{index}:v]setpts=PTS-STARTPTS,format=rgba,scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            f"force_original_aspect_ratio=decrease{overlay_label}"
        )
        parts.append(
            f"{current_label}{overlay_label}overlay=(W-w)/2:(H-h)/2:"
            f"enable='between(t,{_ffmpeg_seconds(visual['relative_start'])},"
            f"{_ffmpeg_seconds(visual['relative_end'])})'{next_label}"
        )
        current_label = next_label
    return ";".join(parts), current_label, "[a0]"


def _pipeline_errors(project_dir: Path, data: ProjectState) -> list[str]:
    freshness = build_pipeline_freshness(project_dir, data)
    errors: list[str] = []
    for stage in ("raw", "audio", "transcript", "alignment"):
        result = freshness[stage]
        if not is_current(result):
            reason = result["reason"]
            detail = f" ({reason})" if reason is not None else ""
            errors.append(f"{stage} freshness is not current: {result['state']}{detail}")
    timeline_freshness = build_timeline_chunk_freshness(project_dir, data, freshness)
    for stage in ("timeline", "chunking"):
        result = timeline_freshness[stage]
        if not is_current(result):
            reason = result["reason"]
            detail = f" ({reason})" if reason is not None else ""
            errors.append(f"{stage} freshness is not current: {result['state']}{detail}")
    return errors


def _visuals_for_render(
    data: ProjectState,
    project_dir: Path,
    chunk_id: str,
    chunk_start: float,
    chunk_end: float,
) -> tuple[list[ChunkVisualForRender], list[str]]:
    visuals: list[ChunkVisualForRender] = []
    errors: list[str] = []
    for visual in data.get("visuals", []):
        if visual.get("chunk_id") != chunk_id:
            continue
        visual_id = _string_field(visual, "id")
        preview_id = _string_field(visual, "preview_id")
        start = _number_field(visual, "start")
        end = _number_field(visual, "end")
        if visual_id is None:
            errors.append("Chunk visual record is missing a non-empty id.")
            continue
        if _string_field(visual, "status") != "previewed":
            errors.append(f"Visual must be previewed before chunk render: {visual_id}")
            continue
        if preview_id is None:
            errors.append(f"Visual is missing preview_id: {visual_id}")
            continue
        if start is None or end is None or end <= start:
            errors.append(f"Visual has invalid timing: {visual_id}")
            continue
        if start < chunk_start or end > chunk_end:
            errors.append(f"Visual timing is outside chunk range: {visual_id}")
            continue

        preview = _find_preview(data, preview_id)
        if preview is None:
            errors.append(f"Preview record not found for visual {visual_id}: {preview_id}")
            continue
        preview_output = _string_field(preview, "output")
        if preview_output is None:
            errors.append(f"Preview record is missing output: {preview_id}")
            continue
        if Path(preview_output).suffix.casefold() != ".png":
            errors.append(f"Only PNG visual previews are supported in V0: {preview_output}")
            continue
        preview_path = artifact_path(project_dir, data, preview_output)
        preview_errors = _validate_preview_png(preview_path, preview_id)
        if preview_errors:
            errors.extend(preview_errors)
            continue
        preview_freshness = build_preview_freshness(project_dir, data, preview)
        if not is_current(preview_freshness):
            reason = preview_freshness["reason"]
            detail = f" ({reason})" if reason is not None else ""
            errors.append(
                f"Preview is not current for {visual_id}: {preview_freshness['state']}{detail}"
            )
            continue
        preview_fingerprint = preview_artifact_fingerprint(preview)
        if preview_fingerprint is None:
            errors.append(f"Preview record is missing artifact fingerprint: {preview_id}")
            continue
        visuals.append(
            {
                "visual_id": visual_id,
                "preview_id": preview_id,
                "start": start,
                "end": end,
                "relative_start": round(start - chunk_start, 3),
                "relative_end": round(end - chunk_start, 3),
                "preview_output": preview_output,
                "preview_path": preview_path,
                "preview_fingerprint": preview_fingerprint,
            }
        )
    visuals.sort(key=lambda item: (item["start"], item["end"], item["visual_id"]))
    return visuals, errors


def _validate_preview_png(path: Path, preview_id: str) -> list[str]:
    if not path.is_file():
        return [f"Missing preview PNG for {preview_id}: {path}"]
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        return [f"Could not read preview PNG for {preview_id}: {exc}"]
    return []


def _render_metadata(
    data: ProjectState,
    chunk_id: str,
    relative_output: Path,
    duration: float,
    visuals: list[ChunkVisualForRender],
    raw_file: Path,
    output_file: Path,
) -> JsonObject:
    visual_plan_fingerprint = build_visual_plan_fingerprint(data, chunk_id)
    if visual_plan_fingerprint is None:
        raise ValueError(f"Could not fingerprint visual plan for chunk: {chunk_id}")
    preview_fingerprints: JsonObject = {}
    for visual in visuals:
        preview_fingerprints[visual["preview_id"]] = visual["preview_fingerprint"]
    chunk_plan_fingerprint = current_chunk_plan_fingerprint(data)
    if chunk_plan_fingerprint is None:
        raise ValueError("Could not read current chunk-plan fingerprint")
    return {
        "path": relative_output.as_posix(),
        "chunk_id": chunk_id,
        "source": data["project"]["video"],
        "source_fingerprint": stat_fingerprint(raw_file),
        "duration_seconds": duration,
        "visual_ids": _json_string_list([visual["visual_id"] for visual in visuals]),
        "preview_ids": _json_string_list([visual["preview_id"] for visual in visuals]),
        "visual_plan_fingerprint": visual_plan_fingerprint,
        "preview_fingerprints": preview_fingerprints,
        "chunk_plan_fingerprint": chunk_plan_fingerprint,
        "status": CHUNK_STATUS_RENDERED,
        "rendered_at": utc_now_iso(),
        "artifact_fingerprint": stat_fingerprint(output_file),
    }


def _record_render_success(
    project_dir: Path,
    data: ProjectState,
    chunk: JsonObject,
    metadata: JsonObject,
) -> None:
    now = utc_now_iso()
    chunk["status"] = CHUNK_STATUS_RENDERED
    chunk["updated_at"] = now
    renders = _ensure_renders(data)
    chunks = _ensure_render_chunks(renders)
    chunk_id = metadata["chunk_id"]
    if isinstance(chunk_id, str):
        chunks[chunk_id] = metadata
        resolve_matching_failures(data, stage=CHUNK_RENDER_STAGE, scope=chunk_failure_scope(chunk_id))
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_render_failure(
    project_dir: Path,
    data: ProjectState,
    chunk_id: str,
    output_path: str,
    errors: list[str],
) -> None:
    context: JsonObject = {
        "chunk_id": chunk_id,
        "output_path": output_path,
    }
    record_failure(
        data,
        stage=CHUNK_RENDER_STAGE,
        scope=chunk_failure_scope(chunk_id),
        errors=errors,
        recommended_next_action=CHUNK_RENDER_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _ensure_renders(data: ProjectState) -> JsonObject:
    renders = data.get("renders")
    if renders is None:
        renders = {}
        data["renders"] = renders
    return renders


def _ensure_render_chunks(renders: JsonObject) -> JsonObject:
    chunks = renders.get("chunks")
    if not isinstance(chunks, dict):
        chunks = {}
        renders["chunks"] = chunks
    return cast(JsonObject, chunks)


def _find_chunk(data: ProjectState, chunk_id: str) -> JsonObject | None:
    for chunk in data["chunks"]:
        if chunk.get("id") == chunk_id:
            return chunk
    return None


def _find_preview(data: ProjectState, preview_id: str) -> JsonObject | None:
    for preview in data.get("previews", []):
        if preview.get("id") == preview_id:
            return preview
    return None


def _frame_rate(data: ProjectState) -> float | None:
    media = data.get("media")
    if not isinstance(media, dict):
        return None
    raw = media.get("raw")
    if not isinstance(raw, dict):
        return None
    video = raw.get("video")
    if not isinstance(video, dict):
        return None
    value = video.get("frame_rate")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return None


def _string_field(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _json_string_list(values: list[str]) -> list[JsonValue]:
    output: list[JsonValue] = []
    for value in values:
        output.append(value)
    return output


def _string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _ffmpeg_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
