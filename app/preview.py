"""Project-level preview rendering."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict, cast

from PIL import Image, ImageDraw, ImageFont
from PIL import UnidentifiedImageError

from .artifacts import atomic_write_json, replace_artifact, temporary_artifact_path
from .failures import chunk_failure_scope, record_failure, resolve_matching_failures, template_failure_scope, visual_failure_scope
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for, utc_now_iso, write_project
from .render_freshness import build_preview_provenance
from .render_template import parse_params_json, render_template
from .visuals import find_visual_for_preview, format_seconds, mark_visual_previewed


PREVIEWS_DIR = "previews"
CHUNK_PREVIEWS_DIR = "chunk-previews"
PREVIEW_RECOMMENDED_NEXT_ACTION = "Fix the template reference or params, then rerun preview."
PREVIEW_VISUAL_RECOMMENDED_NEXT_ACTION = "Fix the visual record or template, then rerun preview-visual."
CHUNK_PREVIEW_RECOMMENDED_NEXT_ACTION = "Fix the chunk visuals or templates, then rerun preview --chunk."
STORYBOARD_SIZE = (1920, 1080)
CHUNK_STATUS_PREVIEWED = "previewed"


class PreviewResult(TypedDict):
    project_dir: str
    template_ref: str
    template_id: str | None
    preview_id: str | None
    output_path: str | None
    success: bool
    errors: list[str]


class PreviewVisualResult(TypedDict):
    project_dir: str
    visual_id: str
    preview_id: str | None
    output_path: str | None
    success: bool
    errors: list[str]


class ChunkPreviewResult(TypedDict):
    project_dir: str
    chunk_id: str
    chunk_preview_id: str | None
    output_path: str | None
    manifest_path: str | None
    visual_count: int
    success: bool
    errors: list[str]


class ChunkVisualCandidate(TypedDict):
    index: int
    visual_id: str
    start: float
    end: float


class RenderedChunkVisual(TypedDict):
    index: int
    visual_id: str
    template_ref: str
    template_id: str | None
    params: dict[str, object]
    start: float
    end: float
    preview_id: str
    relative_output: Path
    output_path: Path


def render_project_preview_from_json(project_dir: Path, template_ref: str, params_json: str) -> PreviewResult:
    data = load_project(project_dir)
    params, errors = parse_params_json(params_json)
    if errors:
        result = _preview_result(project_dir, template_ref, None, None, None, False, errors)
        _record_preview_failure(project_dir, data, result)
        return result

    preview_id = build_preview_id(template_ref, params)
    relative_output = preview_output_for(preview_id)
    output_path = artifact_path(project_dir, data, relative_output)
    render_result = render_template(template_ref, output_path, params)
    result = _preview_result(
        project_dir,
        template_ref,
        render_result["template_id"],
        preview_id,
        output_path,
        render_result["success"],
        render_result["errors"],
    )

    if result["success"]:
        _record_preview_success(project_dir, data, result, params, relative_output)
    else:
        _record_preview_failure(project_dir, data, result)
    return result


def render_project_preview_for_chunk(project_dir: Path, chunk_id: str) -> ChunkPreviewResult:
    data = load_project(project_dir)
    chunk = _find_chunk(data, chunk_id)
    errors: list[str] = []
    chunk_start: float | None = None
    chunk_end: float | None = None
    if chunk is None:
        errors.append(f"Chunk not found: {chunk_id}")
    else:
        chunk_start = _number_field(chunk, "start")
        chunk_end = _number_field(chunk, "end")
        if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
            errors.append(f"Chunk record is invalid: {chunk_id}")

    candidates: list[ChunkVisualCandidate] = []
    if not errors:
        candidates, candidate_errors = _chunk_visual_candidates(data, chunk_id)
        errors.extend(candidate_errors)
        if not candidates:
            errors.append(f"No visuals found for chunk: {chunk_id}")

    rendered: list[RenderedChunkVisual] = []
    if not errors:
        for candidate in candidates:
            lookup = find_visual_for_preview(data, candidate["visual_id"])
            template_ref = lookup["template_ref"]
            if lookup["errors"] or template_ref is None:
                errors.extend(f"{candidate['visual_id']}: {error}" for error in lookup["errors"])
                continue
            preview_id = build_preview_id(template_ref, lookup["params"])
            relative_output = preview_output_for(preview_id)
            output_path = artifact_path(project_dir, data, relative_output)
            render_result = render_template(template_ref, output_path, lookup["params"])
            if not render_result["success"]:
                errors.extend(f"{candidate['visual_id']}: {error}" for error in render_result["errors"])
                continue
            rendered.append(
                {
                    "index": candidate["index"],
                    "visual_id": candidate["visual_id"],
                    "template_ref": template_ref,
                    "template_id": render_result["template_id"],
                    "params": lookup["params"],
                    "start": candidate["start"],
                    "end": candidate["end"],
                    "preview_id": preview_id,
                    "relative_output": relative_output,
                    "output_path": output_path,
                }
            )

    preview_id = build_chunk_preview_id(chunk_id)
    relative_output = chunk_preview_output_for(chunk_id)
    relative_manifest = chunk_preview_manifest_for(chunk_id)
    output_path = artifact_path(project_dir, data, relative_output)
    manifest_path = artifact_path(project_dir, data, relative_manifest)

    if not errors and chunk is not None and chunk_start is not None and chunk_end is not None:
        manifest = _chunk_preview_manifest(
            chunk_id,
            relative_output,
            chunk_start,
            chunk_end,
            rendered,
            utc_now_iso(),
        )
        try:
            _write_storyboard_png(output_path, chunk_id, chunk_start, chunk_end, rendered)
            atomic_write_json(manifest_path, manifest)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            errors.append(f"Could not write chunk preview artifacts: {exc}")

    result = _chunk_preview_result(
        project_dir,
        chunk_id,
        preview_id,
        output_path,
        manifest_path,
        len(rendered),
        not errors,
        errors,
    )
    if result["success"] and chunk is not None:
        _record_chunk_preview_success(
            project_dir,
            data,
            result,
            chunk,
            rendered,
            relative_output,
            relative_manifest,
        )
    else:
        _record_chunk_preview_failure(project_dir, data, result)
    return result


def render_project_preview_for_visual(project_dir: Path, visual_id: str) -> PreviewVisualResult:
    data = load_project(project_dir)
    lookup = find_visual_for_preview(data, visual_id)
    template_ref = lookup["template_ref"]
    visual_index = lookup["index"]

    if lookup["errors"] or template_ref is None or visual_index is None:
        result = _preview_visual_result(project_dir, visual_id, None, None, False, lookup["errors"])
        _record_preview_visual_failure(project_dir, data, result, template_ref)
        return result

    preview_id = build_preview_id(template_ref, lookup["params"])
    relative_output = preview_output_for(preview_id)
    output_path = artifact_path(project_dir, data, relative_output)
    render_result = render_template(template_ref, output_path, lookup["params"])
    result = _preview_visual_result(
        project_dir,
        visual_id,
        preview_id,
        output_path,
        render_result["success"],
        render_result["errors"],
    )

    if result["success"]:
        now = utc_now_iso()
        _record_preview_success_without_write(
            project_dir,
            data,
            template_ref,
            render_result["template_id"],
            preview_id,
            lookup["params"],
            relative_output,
            now,
        )
        mark_visual_previewed(data, visual_index, preview_id, now)
        resolve_matching_failures(
            data,
            stage="preview_visual",
            scope=visual_failure_scope(visual_id),
        )
        data["project"]["updated_at"] = now
        write_project(project_file_for(project_dir), data)
    else:
        _record_preview_visual_failure(project_dir, data, result, template_ref)
    return result


def build_preview_id(template_ref: str, params: dict[str, object]) -> str:
    canonical = json.dumps(
        {"template_ref": template_ref, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"preview_{digest}"


def preview_output_for(preview_id: str) -> Path:
    return Path(PREVIEWS_DIR) / f"{preview_id}.png"


def build_chunk_preview_id(chunk_id: str) -> str:
    return f"chunk_preview_{chunk_id}"


def chunk_preview_output_for(chunk_id: str) -> Path:
    return Path(CHUNK_PREVIEWS_DIR) / f"{chunk_id}.png"


def chunk_preview_manifest_for(chunk_id: str) -> Path:
    return Path(CHUNK_PREVIEWS_DIR) / f"{chunk_id}.json"


def format_preview_result(result: PreviewResult) -> str:
    status = "rendered" if result["success"] else "failed"
    lines = [
        f"Preview: {result['preview_id'] or 'not-created'}",
        f"Template: {result['template_id'] or result['template_ref']}",
        f"Status: {status}",
    ]
    if result["output_path"] is not None:
        lines.append(f"Output: {result['output_path']}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_chunk_preview_result(result: ChunkPreviewResult) -> str:
    status = "rendered" if result["success"] else "failed"
    lines = [
        f"Chunk: {result['chunk_id']}",
        f"Chunk preview: {result['chunk_preview_id'] or 'not-created'}",
        f"Visuals: {result['visual_count']}",
        f"Status: {status}",
    ]
    if result["output_path"] is not None:
        lines.append(f"Output: {result['output_path']}")
    if result["manifest_path"] is not None:
        lines.append(f"Manifest: {result['manifest_path']}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_preview_visual_result(result: PreviewVisualResult) -> str:
    status = "rendered" if result["success"] else "failed"
    lines = [
        f"Visual: {result['visual_id']}",
        f"Preview: {result['preview_id'] or 'not-created'}",
        f"Status: {status}",
    ]
    if result["output_path"] is not None:
        lines.append(f"Output: {result['output_path']}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _record_preview_success(
    project_dir: Path,
    data: ProjectState,
    result: PreviewResult,
    params: dict[str, object],
    relative_output: Path,
) -> None:
    preview_id = result["preview_id"]
    if preview_id is None:
        raise ValueError("Cannot record preview success without a preview ID")

    now = utc_now_iso()
    _record_preview_success_without_write(
        project_dir,
        data,
        result["template_ref"],
        result["template_id"],
        preview_id,
        params,
        relative_output,
        now,
    )
    resolve_matching_failures(
        data,
        stage="preview",
        scope=template_failure_scope(result["template_ref"]),
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_preview_success_without_write(
    project_dir: Path,
    data: ProjectState,
    template_ref: str,
    template_id: str | None,
    preview_id: str,
    params: dict[str, object],
    relative_output: Path,
    created_or_updated_at: str,
) -> None:
    previews = _ensure_previews(data)
    existing_index = _find_record_index(previews, preview_id)
    existing_created_at = _created_at_for(previews, existing_index, created_or_updated_at)
    provenance = build_preview_provenance(project_dir, data, template_ref, relative_output)
    record: JsonObject = {
        "id": preview_id,
        "template_ref": template_ref,
        "template_id": template_id,
        "params": cast(JsonValue, params),
        "output": relative_output.as_posix(),
        "status": "rendered",
        "created_at": existing_created_at,
        "updated_at": created_or_updated_at,
        **provenance,
    }

    if existing_index is None:
        previews.append(record)
    else:
        previews[existing_index] = record


def _record_chunk_preview_success(
    project_dir: Path,
    data: ProjectState,
    result: ChunkPreviewResult,
    chunk: JsonObject,
    rendered: list[RenderedChunkVisual],
    relative_output: Path,
    relative_manifest: Path,
) -> None:
    now = utc_now_iso()
    for item in rendered:
        _record_preview_success_without_write(
            project_dir,
            data,
            item["template_ref"],
            item["template_id"],
            item["preview_id"],
            item["params"],
            item["relative_output"],
            now,
        )
        mark_visual_previewed(data, item["index"], item["preview_id"], now)

    chunk["status"] = CHUNK_STATUS_PREVIEWED
    chunk["visual_mode"] = "visuals"
    chunk.pop("camera_only_at", None)
    chunk["updated_at"] = now
    _record_chunk_preview_without_write(
        data,
        result,
        rendered,
        relative_output,
        relative_manifest,
        now,
    )
    resolve_matching_failures(
        data,
        stage="chunk_preview",
        scope=chunk_failure_scope(result["chunk_id"]),
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_chunk_preview_without_write(
    data: ProjectState,
    result: ChunkPreviewResult,
    rendered: list[RenderedChunkVisual],
    relative_output: Path,
    relative_manifest: Path,
    now: str,
) -> None:
    chunk_preview_id = result["chunk_preview_id"]
    if chunk_preview_id is None:
        raise ValueError("Cannot record chunk preview success without an ID")
    records = _ensure_chunk_previews(data)
    existing_index = _find_record_index(records, chunk_preview_id)
    existing_created_at = _created_at_for(records, existing_index, now)
    record: JsonObject = {
        "id": chunk_preview_id,
        "chunk_id": result["chunk_id"],
        "output": relative_output.as_posix(),
        "manifest": relative_manifest.as_posix(),
        "visual_ids": _json_string_list([item["visual_id"] for item in rendered]),
        "preview_ids": _json_string_list([item["preview_id"] for item in rendered]),
        "status": "rendered",
        "created_at": existing_created_at,
        "updated_at": now,
    }
    if existing_index is None:
        records.append(record)
    else:
        records[existing_index] = record


def _record_preview_failure(project_dir: Path, data: ProjectState, result: PreviewResult) -> None:
    now = utc_now_iso()
    _ensure_previews(data)
    context: JsonObject = {
        "template_ref": result["template_ref"],
        "preview_id": result["preview_id"],
    }
    record_failure(
        data,
        stage="preview",
        scope=template_failure_scope(result["template_ref"]),
        errors=result["errors"],
        recommended_next_action=PREVIEW_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_preview_visual_failure(
    project_dir: Path,
    data: ProjectState,
    result: PreviewVisualResult,
    template_ref: str | None,
) -> None:
    now = utc_now_iso()
    _ensure_previews(data)
    context: JsonObject = {
        "visual_id": result["visual_id"],
        "template_ref": template_ref,
        "preview_id": result["preview_id"],
    }
    record_failure(
        data,
        stage="preview_visual",
        scope=visual_failure_scope(result["visual_id"]),
        errors=result["errors"],
        recommended_next_action=PREVIEW_VISUAL_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_chunk_preview_failure(project_dir: Path, data: ProjectState, result: ChunkPreviewResult) -> None:
    now = utc_now_iso()
    _ensure_chunk_previews(data)
    context: JsonObject = {
        "chunk_id": result["chunk_id"],
        "chunk_preview_id": result["chunk_preview_id"],
        "output_path": result["output_path"],
        "manifest_path": result["manifest_path"],
    }
    record_failure(
        data,
        stage="chunk_preview",
        scope=chunk_failure_scope(result["chunk_id"]),
        errors=result["errors"],
        recommended_next_action=CHUNK_PREVIEW_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _ensure_previews(data: ProjectState) -> list[JsonObject]:
    previews = data.get("previews")
    if previews is None:
        previews = []
        data["previews"] = previews
    return previews


def _ensure_chunk_previews(data: ProjectState) -> list[JsonObject]:
    chunk_previews = data.get("chunk_previews")
    if chunk_previews is None:
        chunk_previews = []
        data["chunk_previews"] = chunk_previews
    return chunk_previews


def _find_record_index(records: list[JsonObject], record_id: str) -> int | None:
    for index, record in enumerate(records):
        if record.get("id") == record_id:
            return index
    return None


def _created_at_for(records: list[JsonObject], index: int | None, default: str) -> str:
    if index is None:
        return default
    value = records[index].get("created_at")
    if isinstance(value, str) and value.strip():
        return value
    return default


def _find_chunk(data: ProjectState, chunk_id: str) -> JsonObject | None:
    for chunk in data["chunks"]:
        if chunk.get("id") == chunk_id:
            return chunk
    return None


def _chunk_visual_candidates(data: ProjectState, chunk_id: str) -> tuple[list[ChunkVisualCandidate], list[str]]:
    visuals = data.get("visuals", [])
    candidates: list[ChunkVisualCandidate] = []
    errors: list[str] = []
    for index, visual in enumerate(visuals):
        if visual.get("chunk_id") != chunk_id:
            continue
        visual_id = _string_field(visual, "id")
        start = _number_field(visual, "start")
        end = _number_field(visual, "end")
        if visual_id is None:
            errors.append("Chunk visual record is missing a non-empty id.")
            continue
        if start is None or end is None or end <= start:
            errors.append(f"Chunk visual {visual_id} has invalid timing.")
            continue
        candidates.append({"index": index, "visual_id": visual_id, "start": start, "end": end})
    candidates.sort(key=lambda item: (item["start"], item["end"], item["visual_id"]))
    return candidates, errors


def _chunk_preview_manifest(
    chunk_id: str,
    relative_output: Path,
    chunk_start: float,
    chunk_end: float,
    rendered: list[RenderedChunkVisual],
    generated_at: str,
) -> JsonObject:
    visual_records: list[JsonValue] = []
    for item in rendered:
        visual_records.append(
            {
                "visual_id": item["visual_id"],
                "preview_id": item["preview_id"],
                "template_ref": item["template_ref"],
                "template_id": item["template_id"],
                "start": item["start"],
                "end": item["end"],
                "preview_output": item["relative_output"].as_posix(),
            }
        )
    return {
        "schema_version": 1,
        "chunk_id": chunk_id,
        "output": relative_output.as_posix(),
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
        "visual_count": len(rendered),
        "visuals": visual_records,
        "generated_at": generated_at,
    }


def _write_storyboard_png(
    output_path: Path,
    chunk_id: str,
    chunk_start: float,
    chunk_end: float,
    rendered: list[RenderedChunkVisual],
) -> None:
    if not rendered:
        raise ValueError("Cannot write a chunk preview without visuals.")

    canvas = Image.new("RGB", STORYBOARD_SIZE, (246, 246, 242))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    title = (
        f"{chunk_id}  {format_seconds(chunk_start)} -> {format_seconds(chunk_end)}  "
        f"{len(rendered)} visual(s)"
    )
    draw.text((48, 34), title, fill=(20, 24, 28), font=font)

    padding = 48
    gap = 28
    top = 110
    columns = min(3, max(1, len(rendered)))
    rows = (len(rendered) + columns - 1) // columns
    cell_width = (STORYBOARD_SIZE[0] - padding * 2 - gap * (columns - 1)) // columns
    cell_height = (STORYBOARD_SIZE[1] - top - padding - gap * (rows - 1)) // rows

    for index, item in enumerate(rendered):
        row = index // columns
        column = index % columns
        left = padding + column * (cell_width + gap)
        upper = top + row * (cell_height + gap)
        right = left + cell_width
        lower = upper + cell_height
        draw.rectangle((left, upper, right, lower), outline=(204, 204, 196), width=2)

        label = (
            f"{item['visual_id']}  {format_seconds(item['start'])} -> {format_seconds(item['end'])}"
        )
        draw.text((left + 12, upper + 10), label, fill=(32, 36, 40), font=font)

        thumb_top = upper + 36
        thumb_height = max(1, cell_height - 48)
        thumb_width = max(1, cell_width - 24)
        with Image.open(item["output_path"]) as source_image:
            preview = source_image.convert("RGB")
        preview.thumbnail((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        paste_left = left + (cell_width - preview.width) // 2
        paste_top = thumb_top + (thumb_height - preview.height) // 2
        canvas.paste(preview, (paste_left, paste_top))

    with temporary_artifact_path(output_path) as temporary:
        canvas.save(temporary, format="PNG")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        replace_artifact(temporary, output_path)


def _json_string_list(values: list[str]) -> list[JsonValue]:
    output: list[JsonValue] = []
    for value in values:
        output.append(value)
    return output


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


def _preview_result(
    project_dir: Path,
    template_ref: str,
    template_id: str | None,
    preview_id: str | None,
    output_path: Path | None,
    success: bool,
    errors: list[str],
) -> PreviewResult:
    return {
        "project_dir": str(project_dir),
        "template_ref": template_ref,
        "template_id": template_id,
        "preview_id": preview_id,
        "output_path": str(output_path) if output_path is not None else None,
        "success": success,
        "errors": errors,
    }


def _chunk_preview_result(
    project_dir: Path,
    chunk_id: str,
    preview_id: str | None,
    output_path: Path | None,
    manifest_path: Path | None,
    visual_count: int,
    success: bool,
    errors: list[str],
) -> ChunkPreviewResult:
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "chunk_preview_id": preview_id,
        "output_path": str(output_path) if output_path is not None else None,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "visual_count": visual_count,
        "success": success,
        "errors": errors,
    }


def _preview_visual_result(
    project_dir: Path,
    visual_id: str,
    preview_id: str | None,
    output_path: Path | None,
    success: bool,
    errors: list[str],
) -> PreviewVisualResult:
    return {
        "project_dir": str(project_dir),
        "visual_id": visual_id,
        "preview_id": preview_id,
        "output_path": str(output_path) if output_path is not None else None,
        "success": success,
        "errors": errors,
    }
