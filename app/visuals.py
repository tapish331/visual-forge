"""Project-level visual plan items."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

from .failures import record_failure, resolve_matching_failures, template_failure_scope, visual_failure_scope
from .project import JsonObject, JsonValue, ProjectError, ProjectState, load_project, project_file_for, utc_now_iso, write_project
from .render_template import parse_params_json, validate_template_params


VISUAL_STATUS_PLANNED = "planned"
VISUAL_STATUS_PREVIEWED = "previewed"
ADD_VISUAL_RECOMMENDED_NEXT_ACTION = "Fix the visual timing, template reference, or params, then rerun add-visual."
UPDATE_VISUAL_RECOMMENDED_NEXT_ACTION = "Fix the visual update fields, then rerun update-visual."


class AddVisualResult(TypedDict):
    project_dir: str
    visual_id: str | None
    template_ref: str
    template_id: str | None
    chunk_id: str | None
    start: float
    end: float
    success: bool
    errors: list[str]


class UpdateVisualResult(TypedDict):
    project_dir: str
    visual_id: str
    chunk_id: str | None
    success: bool
    changes: list[str]
    errors: list[str]


class VisualsSummary(TypedDict):
    project_dir: str
    chunk_id: str | None
    total: int
    by_status: dict[str, int]
    visuals: list[JsonObject]


class VisualForPreview(TypedDict):
    index: int | None
    visual: JsonObject | None
    template_ref: str | None
    params: dict[str, object]
    errors: list[str]


class VisualForUpdate(TypedDict):
    index: int | None
    visual: JsonObject | None
    template_ref: str | None
    chunk_id: str | None
    params: dict[str, object]
    start: float | None
    end: float | None
    errors: list[str]


def add_visual_from_json(
    project_dir: Path,
    template_ref: str,
    start: float,
    end: float,
    params_json: str,
    chunk_id: str | None = None,
) -> AddVisualResult:
    data = load_project(project_dir)
    params, errors = parse_params_json(params_json)
    errors.extend(validate_time_range(start, end))
    errors.extend(validate_chunk_reference(data, chunk_id, start, end))
    if errors:
        result = _add_visual_result(project_dir, None, template_ref, None, chunk_id, start, end, False, errors)
        _record_add_visual_failure(project_dir, data, result)
        return result

    validation = validate_template_params(template_ref, params)
    if not validation["valid"]:
        result = _add_visual_result(
            project_dir,
            None,
            template_ref,
            validation["template_id"],
            chunk_id,
            start,
            end,
            False,
            validation["errors"],
        )
        _record_add_visual_failure(project_dir, data, result)
        return result

    visual_id = build_visual_id(template_ref, params, start, end, chunk_id=chunk_id)
    result = _add_visual_result(
        project_dir,
        visual_id,
        template_ref,
        validation["template_id"],
        chunk_id,
        start,
        end,
        True,
        [],
    )
    _record_visual_success(project_dir, data, result, params)
    return result


def update_visual_from_json(
    project_dir: Path,
    visual_id: str,
    template_ref: str | None,
    start: float | None,
    end: float | None,
    params_json: str | None,
    chunk_id: str | None = None,
) -> UpdateVisualResult:
    data = load_project(project_dir)
    lookup = find_visual_for_update(data, visual_id)

    errors: list[str] = []
    if template_ref is None and start is None and end is None and params_json is None and chunk_id is None:
        errors.append("At least one update option is required")
    errors.extend(lookup["errors"])

    parsed_params: dict[str, object] = {}
    if params_json is not None:
        parsed_params, params_errors = parse_params_json(params_json)
        errors.extend(params_errors)

    if errors:
        result = _update_visual_result(project_dir, visual_id, chunk_id, False, [], errors)
        _record_update_visual_failure(project_dir, data, visual_id, errors, chunk_id=chunk_id)
        return result

    existing_visual = lookup["visual"]
    existing_template_ref = lookup["template_ref"]
    existing_chunk_id = lookup["chunk_id"]
    existing_start = lookup["start"]
    existing_end = lookup["end"]
    if (
        existing_visual is None
        or existing_template_ref is None
        or existing_start is None
        or existing_end is None
    ):
        fallback_errors = ["Visual record is incomplete"]
        result = _update_visual_result(project_dir, visual_id, chunk_id, False, [], fallback_errors)
        _record_update_visual_failure(project_dir, data, visual_id, fallback_errors, chunk_id=chunk_id)
        return result

    next_template_ref = template_ref if template_ref is not None else existing_template_ref
    next_chunk_id = chunk_id if chunk_id is not None else existing_chunk_id
    next_params = parsed_params if params_json is not None else lookup["params"]
    next_start = start if start is not None else existing_start
    next_end = end if end is not None else existing_end

    validation_errors = validate_time_range(next_start, next_end)
    validation_errors.extend(validate_chunk_reference(data, next_chunk_id, next_start, next_end))
    validation = validate_template_params(next_template_ref, next_params)
    if not validation["valid"]:
        validation_errors.extend(validation["errors"])
    if validation_errors:
        result = _update_visual_result(project_dir, visual_id, next_chunk_id, False, [], validation_errors)
        _record_update_visual_failure(project_dir, data, visual_id, validation_errors, chunk_id=next_chunk_id)
        return result

    changes, correction_changes = _build_visual_changes(
        existing_template_ref,
        existing_chunk_id,
        lookup["params"],
        existing_start,
        existing_end,
        next_template_ref,
        next_chunk_id,
        next_params,
        next_start,
        next_end,
    )

    if changes:
        _record_visual_update_success(
            project_dir,
            data,
            lookup["index"],
            existing_visual,
            visual_id,
            next_template_ref,
            validation["template_id"],
            next_chunk_id,
            next_params,
            next_start,
            next_end,
            correction_changes,
        )
    else:
        resolved_ids = resolve_matching_failures(
            data,
            stage="update_visual",
            scope=visual_failure_scope(visual_id),
        )
        if resolved_ids:
            data["project"]["updated_at"] = utc_now_iso()
            write_project(project_file_for(project_dir), data)

    return _update_visual_result(project_dir, visual_id, next_chunk_id, True, changes, [])


def build_visual_id(
    template_ref: str,
    params: dict[str, object],
    start: float,
    end: float,
    *,
    chunk_id: str | None = None,
) -> str:
    payload: dict[str, object] = {"template_ref": template_ref, "params": params, "start": start, "end": end}
    if chunk_id is not None:
        payload["chunk_id"] = chunk_id
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"visual_{digest}"


def validate_time_range(start: float, end: float) -> list[str]:
    errors: list[str] = []
    if start < 0:
        errors.append("start must be greater than or equal to 0")
    if end <= start:
        errors.append("end must be greater than start")
    return errors


def validate_chunk_reference(data: ProjectState, chunk_id: str | None, start: float, end: float) -> list[str]:
    if chunk_id is None:
        return []
    if not chunk_id.strip():
        return ["chunk_id must be a non-empty string"]
    chunk = find_chunk(data, chunk_id)
    if chunk is None:
        return [f"Chunk not found: {chunk_id}"]
    chunk_start = _number_field(chunk, "start")
    chunk_end = _number_field(chunk, "end")
    if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
        return [f"Chunk record is invalid: {chunk_id}"]
    if start < chunk_start or end > chunk_end:
        return [
            f"visual timing must fit within chunk {chunk_id} "
            f"({format_seconds(chunk_start)} -> {format_seconds(chunk_end)})"
        ]
    return []


def find_chunk(data: ProjectState, chunk_id: str) -> JsonObject | None:
    for chunk in data["chunks"]:
        if chunk.get("id") == chunk_id:
            return chunk
    return None


def build_visuals_summary(project_dir: Path, chunk_id: str | None = None) -> VisualsSummary:
    data = load_project(project_dir)
    if chunk_id is not None and find_chunk(data, chunk_id) is None:
        raise ProjectError(f"Chunk not found: {chunk_id}")
    visuals = _visuals_for_read(data)
    if chunk_id is not None:
        visuals = [visual for visual in visuals if visual.get("chunk_id") == chunk_id]
    statuses: Counter[str] = Counter(_visual_status(visual) for visual in visuals)
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "total": len(visuals),
        "by_status": dict(sorted(statuses.items())),
        "visuals": visuals,
    }


def find_visual_for_preview(data: ProjectState, visual_id: str) -> VisualForPreview:
    visuals = _ensure_visuals(data)
    for index, visual in enumerate(visuals):
        if visual.get("id") == visual_id:
            return _visual_for_preview(index, visual)

    return {
        "index": None,
        "visual": None,
        "template_ref": None,
        "params": {},
        "errors": [f"Visual not found: {visual_id}"],
    }


def find_visual_for_update(data: ProjectState, visual_id: str) -> VisualForUpdate:
    visuals = _ensure_visuals(data)
    for index, visual in enumerate(visuals):
        if visual.get("id") == visual_id:
            return _visual_for_update(index, visual)

    return {
        "index": None,
        "visual": None,
        "template_ref": None,
        "chunk_id": None,
        "params": {},
        "start": None,
        "end": None,
        "errors": [f"Visual not found: {visual_id}"],
    }


def mark_visual_previewed(data: ProjectState, index: int, preview_id: str, updated_at: str) -> None:
    visual = _ensure_visuals(data)[index]
    visual["preview_id"] = preview_id
    visual["status"] = VISUAL_STATUS_PREVIEWED
    visual["updated_at"] = updated_at


def format_add_visual_result(result: AddVisualResult) -> str:
    status = "planned" if result["success"] else "failed"
    lines = [
        f"Visual: {result['visual_id'] or 'not-created'}",
        f"Template: {result['template_id'] or result['template_ref']}",
        f"Chunk: {result['chunk_id'] or 'none'}",
        f"Time: {format_seconds(result['start'])} -> {format_seconds(result['end'])}",
        f"Status: {status}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_update_visual_result(result: UpdateVisualResult) -> str:
    status = "updated" if result["success"] else "failed"
    changes = ", ".join(result["changes"]) if result["changes"] else "none"
    lines = [
        f"Visual: {result['visual_id']}",
        f"Chunk: {result['chunk_id'] or 'none'}",
        f"Status: {status}",
        f"Changes: {changes}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_visuals_summary(summary: VisualsSummary) -> str:
    suffix = f" for {summary['chunk_id']}" if summary["chunk_id"] is not None else ""
    lines = [f"Visuals{suffix}: {summary['total']}"]
    for visual in summary["visuals"]:
        visual_id = _string_field(visual, "id", "unknown")
        template_id = _string_field(visual, "template_id", _string_field(visual, "template_ref", "unknown"))
        chunk_id = _string_field(visual, "chunk_id", "none")
        status = _string_field(visual, "status", "unknown")
        start = _float_field(visual, "start", 0.0)
        end = _float_field(visual, "end", 0.0)
        lines.append(
            f"- {visual_id} {format_seconds(start)} -> {format_seconds(end)} "
            f"{status} {template_id} chunk={chunk_id}"
        )
    return "\n".join(lines)


def format_seconds(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _record_visual_success(
    project_dir: Path,
    data: ProjectState,
    result: AddVisualResult,
    params: dict[str, object],
) -> None:
    visual_id = result["visual_id"]
    if visual_id is None:
        raise ValueError("Cannot record visual success without a visual ID")

    now = utc_now_iso()
    visuals = _ensure_visuals(data)
    existing_index = _find_record_index(visuals, visual_id)
    existing_created_at = _created_at_for(visuals, existing_index, now)
    record: JsonObject = {
        "id": visual_id,
        "template_ref": result["template_ref"],
        "template_id": result["template_id"],
        "params": cast(JsonValue, params),
        "start": result["start"],
        "end": result["end"],
        "status": VISUAL_STATUS_PLANNED,
        "preview_id": None,
        "created_at": existing_created_at,
        "updated_at": now,
    }
    if result["chunk_id"] is not None:
        record["chunk_id"] = result["chunk_id"]

    if existing_index is None:
        visuals.append(record)
    else:
        visuals[existing_index] = record

    _invalidate_chunk(data, result["chunk_id"], now)

    resolve_matching_failures(
        data,
        stage="add_visual",
        scope=template_failure_scope(result["template_ref"]),
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_visual_update_success(
    project_dir: Path,
    data: ProjectState,
    index: int | None,
    existing_visual: JsonObject,
    visual_id: str,
    template_ref: str,
    template_id: str | None,
    chunk_id: str | None,
    params: dict[str, object],
    start: float,
    end: float,
    correction_changes: JsonObject,
) -> None:
    if index is None:
        raise ValueError("Cannot record visual update without a visual index")

    now = utc_now_iso()
    created_at = _string_field(existing_visual, "created_at", now)
    record: JsonObject = {
        "id": visual_id,
        "template_ref": template_ref,
        "template_id": template_id,
        "params": cast(JsonValue, params),
        "start": start,
        "end": end,
        "status": VISUAL_STATUS_PLANNED,
        "preview_id": None,
        "created_at": created_at,
        "updated_at": now,
    }
    if chunk_id is not None:
        record["chunk_id"] = chunk_id

    visuals = _ensure_visuals(data)
    visuals[index] = record

    previous_chunk_id = _optional_string_field(existing_visual, "chunk_id")
    _invalidate_chunk(data, previous_chunk_id, now)
    if chunk_id != previous_chunk_id:
        _invalidate_chunk(data, chunk_id, now)

    correction: JsonObject = {
        "stage": "visual",
        "visual_id": visual_id,
        "changes": correction_changes,
        "created_at": now,
    }
    data["corrections"].append(correction)
    resolve_matching_failures(
        data,
        stage="update_visual",
        scope=visual_failure_scope(visual_id),
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _invalidate_chunk(data: ProjectState, chunk_id: str | None, updated_at: str) -> None:
    if chunk_id is None:
        return
    chunk = find_chunk(data, chunk_id)
    if chunk is None:
        return
    has_visuals = any(visual.get("chunk_id") == chunk_id for visual in data.get("visuals", []))
    chunk["visual_mode"] = "visuals" if has_visuals else "undecided"
    if has_visuals:
        chunk.pop("camera_only_at", None)
        chunk.pop("visual_planning", None)
    chunk["status"] = "new"
    chunk["updated_at"] = updated_at


def _record_add_visual_failure(project_dir: Path, data: ProjectState, result: AddVisualResult) -> None:
    now = utc_now_iso()
    _ensure_visuals(data)
    context: JsonObject = {
        "template_ref": result["template_ref"],
        "visual_id": result["visual_id"],
    }
    if result["chunk_id"] is not None:
        context["chunk_id"] = result["chunk_id"]
    record_failure(
        data,
        stage="add_visual",
        scope=template_failure_scope(result["template_ref"]),
        errors=result["errors"],
        recommended_next_action=ADD_VISUAL_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_update_visual_failure(
    project_dir: Path,
    data: ProjectState,
    visual_id: str,
    errors: list[str],
    *,
    chunk_id: str | None = None,
) -> None:
    now = utc_now_iso()
    _ensure_visuals(data)
    context: JsonObject = {
        "visual_id": visual_id,
    }
    if chunk_id is not None:
        context["chunk_id"] = chunk_id
    record_failure(
        data,
        stage="update_visual",
        scope=visual_failure_scope(visual_id),
        errors=errors,
        recommended_next_action=UPDATE_VISUAL_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _ensure_visuals(data: ProjectState) -> list[JsonObject]:
    visuals = data.get("visuals")
    if visuals is None:
        visuals = []
        data["visuals"] = visuals
    return visuals


def _visuals_for_read(data: ProjectState) -> list[JsonObject]:
    visuals = data.get("visuals")
    if visuals is None:
        return []
    return visuals


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


def _visual_status(visual: JsonObject) -> str:
    return _string_field(visual, "status", "unknown")


def _visual_for_preview(index: int, visual: JsonObject) -> VisualForPreview:
    errors: list[str] = []

    visual_id = visual.get("id")
    if not isinstance(visual_id, str) or not visual_id.strip():
        errors.append("visual.id must be a non-empty string")

    template_ref_value = visual.get("template_ref")
    template_ref: str | None = None
    if isinstance(template_ref_value, str) and template_ref_value.strip():
        template_ref = template_ref_value
    else:
        errors.append("visual.template_ref must be a non-empty string")

    params_value = visual.get("params")
    params: dict[str, object] = {}
    if isinstance(params_value, dict):
        raw_params = cast(dict[object, object], params_value)
        for key, value in raw_params.items():
            if not isinstance(key, str):
                errors.append("visual.params keys must be strings")
                continue
            params[key] = value
    else:
        errors.append("visual.params must be an object")

    return {
        "index": index,
        "visual": visual,
        "template_ref": template_ref,
        "params": params,
        "errors": errors,
    }


def _visual_for_update(index: int, visual: JsonObject) -> VisualForUpdate:
    preview = _visual_for_preview(index, visual)
    errors = list(preview["errors"])
    chunk_id = _optional_string_field(visual, "chunk_id")
    if chunk_id is None and "chunk_id" in visual:
        errors.append("visual.chunk_id must be a non-empty string when present")

    start = _number_field(visual, "start")
    if start is None:
        errors.append("visual.start must be a number")

    end = _number_field(visual, "end")
    if end is None:
        errors.append("visual.end must be a number")

    return {
        "index": preview["index"],
        "visual": preview["visual"],
        "template_ref": preview["template_ref"],
        "chunk_id": chunk_id,
        "params": preview["params"],
        "start": start,
        "end": end,
        "errors": errors,
    }


def _build_visual_changes(
    before_template_ref: str,
    before_chunk_id: str | None,
    before_params: dict[str, object],
    before_start: float,
    before_end: float,
    after_template_ref: str,
    after_chunk_id: str | None,
    after_params: dict[str, object],
    after_start: float,
    after_end: float,
) -> tuple[list[str], JsonObject]:
    changes: list[str] = []
    correction_changes: JsonObject = {}

    if before_template_ref != after_template_ref:
        changes.append("template_ref")
        correction_changes["template_ref"] = _change_record(before_template_ref, after_template_ref)
    if before_chunk_id != after_chunk_id:
        changes.append("chunk_id")
        correction_changes["chunk_id"] = _change_record(before_chunk_id, after_chunk_id)
    if before_params != after_params:
        changes.append("params")
        correction_changes["params"] = _change_record(cast(JsonValue, before_params), cast(JsonValue, after_params))
    if before_start != after_start:
        changes.append("start")
        correction_changes["start"] = _change_record(before_start, after_start)
    if before_end != after_end:
        changes.append("end")
        correction_changes["end"] = _change_record(before_end, after_end)

    return changes, correction_changes


def _change_record(before: JsonValue, after: JsonValue) -> JsonObject:
    return {"before": before, "after": after}


def _string_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _optional_string_field(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _float_field(data: JsonObject, key: str, default: float) -> float:
    value = data.get(key)
    if isinstance(value, int | float):
        return float(value)
    return default


def _add_visual_result(
    project_dir: Path,
    visual_id: str | None,
    template_ref: str,
    template_id: str | None,
    chunk_id: str | None,
    start: float,
    end: float,
    success: bool,
    errors: list[str],
) -> AddVisualResult:
    return {
        "project_dir": str(project_dir),
        "visual_id": visual_id,
        "template_ref": template_ref,
        "template_id": template_id,
        "chunk_id": chunk_id,
        "start": start,
        "end": end,
        "success": success,
        "errors": errors,
    }


def _update_visual_result(
    project_dir: Path,
    visual_id: str,
    chunk_id: str | None,
    success: bool,
    changes: list[str],
    errors: list[str],
) -> UpdateVisualResult:
    return {
        "project_dir": str(project_dir),
        "visual_id": visual_id,
        "chunk_id": chunk_id,
        "success": success,
        "changes": changes,
        "errors": errors,
    }
