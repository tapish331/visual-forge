"""Deterministic chunk-level visual planning."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import build_pipeline_freshness, is_current
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for
from .project import utc_now_iso, write_project
from .render_template import validate_template_params
from .timeline import build_timeline_chunk_freshness
from .visuals import build_visual_id, find_chunk, format_seconds


PLANNER_ID = "auto_v0"
PLAN_VISUALS_STAGE = "plan_visuals"
PLAN_VISUALS_RECOMMENDED_NEXT_ACTION = (
    "Fix chunk freshness, template availability, or visual overwrite safety, then rerun plan-visuals."
)
DEFAULT_TEMPLATE_REF = "simple_card"
DEFAULT_MAX_VISUALS = 3
MIN_VISUAL_TOKENS = 5
MIN_VISUAL_SECONDS = 2.0
PREFERRED_VISUAL_SECONDS = 6.0
MAX_VISUAL_SECONDS = 8.0

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9']*")
WHITESPACE_PATTERN = re.compile(r"\s+")


class PlanVisualsResult(TypedDict):
    project_dir: str
    chunk_id: str
    success: bool
    planned_count: int
    visual_ids: list[str]
    requires_human_decision: bool
    reused_existing: bool
    warnings: list[str]
    errors: list[str]


class PlannedVisual(TypedDict):
    id: str
    template_ref: str
    template_id: str
    params: dict[str, object]
    start: float
    end: float
    source_block_ids: list[str]
    planning_reason: str


def plan_visuals_for_chunk(
    project_dir: Path,
    chunk_id: str,
    *,
    max_visuals: int = DEFAULT_MAX_VISUALS,
    force_generated: bool = False,
) -> PlanVisualsResult:
    data = load_project(project_dir)
    errors = _validate_max_visuals(max_visuals)
    chunk = find_chunk(data, chunk_id)
    if chunk is None:
        errors.append(f"Chunk not found: {chunk_id}")

    errors.extend(_freshness_errors(project_dir, data))

    artifact: JsonObject | None = None
    if not errors:
        artifact, read_errors = _read_alignment_artifact(project_dir, data)
        errors.extend(read_errors)

    proposed: list[PlannedVisual] = []
    if not errors and chunk is not None and artifact is not None:
        proposed, errors = _build_planned_visuals(data, chunk, artifact, max_visuals)

    existing_visuals = _chunk_visuals(data, chunk_id)
    manual_visuals = [visual for visual in existing_visuals if visual.get("planner") != PLANNER_ID]
    generated_visuals = [visual for visual in existing_visuals if visual.get("planner") == PLANNER_ID]

    if manual_visuals:
        errors.append(
            f"Chunk {chunk_id} already has {len(manual_visuals)} human-created visual record(s); "
            "automatic planning will not overwrite them."
        )

    proposed_ids = [visual["id"] for visual in proposed]
    generated_ids = _visual_ids(generated_visuals)
    if generated_visuals and not force_generated and set(proposed_ids) != set(generated_ids):
        errors.append(
            f"Chunk {chunk_id} already has generated visual record(s). "
            "Use --force-generated to replace them."
        )

    if errors:
        result = _plan_visuals_result(project_dir, chunk_id, False, [], False, False, [], errors)
        _record_plan_failure(project_dir, data, chunk_id, errors)
        return result

    if chunk is None:
        raise ValueError("Chunk cannot be missing after validation succeeds")

    if generated_visuals and not force_generated and set(proposed_ids) == set(generated_ids):
        resolved = resolve_matching_failures(data, stage=PLAN_VISUALS_STAGE, scope=_chunk_scope(chunk_id))
        if resolved:
            data["project"]["updated_at"] = utc_now_iso()
            write_project(project_file_for(project_dir), data)
        return _plan_visuals_result(project_dir, chunk_id, True, generated_ids, False, True, [], [])

    now = utc_now_iso()
    if proposed:
        _replace_generated_visuals(data, chunk_id, proposed, now)
        _mark_chunk_with_visuals(chunk, now)
        resolve_matching_failures(data, stage=PLAN_VISUALS_STAGE, scope=_chunk_scope(chunk_id))
        data["project"]["updated_at"] = now
        write_project(project_file_for(project_dir), data)
        return _plan_visuals_result(project_dir, chunk_id, True, proposed_ids, False, False, [], [])

    warnings = ["No useful visual candidates found; add a manual visual or approve the chunk as camera-only."]
    _remove_generated_visuals(data, chunk_id)
    _mark_chunk_no_candidates(chunk, now)
    resolve_matching_failures(data, stage=PLAN_VISUALS_STAGE, scope=_chunk_scope(chunk_id))
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)
    return _plan_visuals_result(project_dir, chunk_id, True, [], True, False, warnings, [])


def format_plan_visuals_result(result: PlanVisualsResult) -> str:
    status = "planned" if result["success"] else "failed"
    lines = [
        f"Chunk: {result['chunk_id']}",
        f"Status: {status}",
        f"Visuals planned: {result['planned_count']}",
    ]
    if result["visual_ids"]:
        lines.append("Visual IDs: " + ", ".join(result["visual_ids"]))
    if result["requires_human_decision"]:
        lines.append("Human decision required: yes")
    if result["reused_existing"]:
        lines.append("Reused existing generated visuals: yes")
    for warning in result["warnings"]:
        lines.append(f"Warning: {warning}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _build_planned_visuals(
    data: ProjectState,
    chunk: JsonObject,
    artifact: JsonObject,
    max_visuals: int,
) -> tuple[list[PlannedVisual], list[str]]:
    chunk_id = _required_string(chunk, "id")
    chunk_start = _required_number(chunk, "start")
    chunk_end = _required_number(chunk, "end")
    block_ids = _string_list(chunk.get("alignment_block_ids"))
    raw_blocks = artifact.get("blocks")
    if not isinstance(raw_blocks, list):
        return [], ["Alignment artifact must contain a blocks list."]

    blocks_by_id = _blocks_by_id(raw_blocks)
    candidates: list[JsonObject] = []
    for block_id in block_ids:
        block = blocks_by_id.get(block_id)
        if block is None or block.get("status") != "aligned":
            continue
        text = _clean_text(_string_field(block, "text") or "")
        tokens = TOKEN_PATTERN.findall(text)
        if len(tokens) < MIN_VISUAL_TOKENS:
            continue
        start = _number_field(block, "start")
        end = _number_field(block, "end")
        if start is None or end is None or end <= start:
            continue
        candidates.append(
            {
                "id": block_id,
                "text": text,
                "start": max(chunk_start, start),
                "end": min(chunk_end, end),
            }
        )

    selected = _evenly_select(candidates, max_visuals)
    planned: list[PlannedVisual] = []
    errors: list[str] = []
    for candidate in selected:
        params = _params_for_text(_string_field(candidate, "text") or "")
        timing = _visual_timing(
            _required_number(candidate, "start"),
            _required_number(candidate, "end"),
            chunk_start,
            chunk_end,
        )
        if timing is None:
            continue
        start, end = timing
        validation = validate_template_params(DEFAULT_TEMPLATE_REF, params)
        if not validation["valid"] or validation["template_id"] is None:
            errors.extend(validation["errors"])
            continue
        visual_id = build_visual_id(DEFAULT_TEMPLATE_REF, params, start, end, chunk_id=chunk_id)
        planned.append(
            {
                "id": visual_id,
                "template_ref": DEFAULT_TEMPLATE_REF,
                "template_id": validation["template_id"],
                "params": params,
                "start": start,
                "end": end,
                "source_block_ids": [_required_string(candidate, "id")],
                "planning_reason": "high_signal_script_block",
            }
        )
    return planned, errors


def _freshness_errors(project_dir: Path, data: ProjectState) -> list[str]:
    errors: list[str] = []
    pipeline = build_pipeline_freshness(project_dir, data)
    for label in ("raw", "audio", "transcript", "alignment"):
        result = pipeline[label]
        if not is_current(result):
            detail = f" ({result['reason']})" if result["reason"] is not None else ""
            errors.append(f"{label} is not current: {result['state']}{detail}.")
    timeline = build_timeline_chunk_freshness(project_dir, data, pipeline)
    for label in ("timeline", "chunking"):
        result = timeline[label]
        if not is_current(result):
            detail = f" ({result['reason']})" if result["reason"] is not None else ""
            errors.append(f"{label} is not current: {result['state']}{detail}.")
    return errors


def _read_alignment_artifact(project_dir: Path, data: ProjectState) -> tuple[JsonObject | None, list[str]]:
    alignment_path = _alignment_path(data)
    path = artifact_path(project_dir, data, alignment_path)
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


def _replace_generated_visuals(
    data: ProjectState,
    chunk_id: str,
    proposed: list[PlannedVisual],
    now: str,
) -> None:
    created_by_id = {
        visual_id: created_at
        for visual in _chunk_visuals(data, chunk_id)
        if visual.get("planner") == PLANNER_ID
        and (visual_id := _string_field(visual, "id")) is not None
        and (created_at := _string_field(visual, "created_at")) is not None
    }
    _remove_generated_visuals(data, chunk_id)
    visuals = _ensure_visuals(data)
    for visual in proposed:
        record: JsonObject = {
            "id": visual["id"],
            "template_ref": visual["template_ref"],
            "template_id": visual["template_id"],
            "params": cast(JsonValue, visual["params"]),
            "start": visual["start"],
            "end": visual["end"],
            "status": "planned",
            "preview_id": None,
            "chunk_id": chunk_id,
            "planner": PLANNER_ID,
            "source_block_ids": cast(JsonValue, visual["source_block_ids"]),
            "planning_reason": visual["planning_reason"],
            "created_at": created_by_id.get(visual["id"], now),
            "updated_at": now,
        }
        visuals.append(record)


def _remove_generated_visuals(data: ProjectState, chunk_id: str) -> None:
    visuals = data.get("visuals")
    if visuals is None:
        data["visuals"] = []
        return
    data["visuals"] = [
        visual
        for visual in visuals
        if not (visual.get("chunk_id") == chunk_id and visual.get("planner") == PLANNER_ID)
    ]


def _mark_chunk_with_visuals(chunk: JsonObject, now: str) -> None:
    chunk["visual_mode"] = "visuals"
    chunk["status"] = "new"
    chunk["updated_at"] = now
    chunk.pop("camera_only_at", None)
    chunk.pop("visual_planning", None)


def _mark_chunk_no_candidates(chunk: JsonObject, now: str) -> None:
    chunk["visual_mode"] = "undecided"
    chunk["status"] = "new"
    chunk["visual_planning"] = {
        "planner": PLANNER_ID,
        "status": "no_candidates",
        "planned_at": now,
    }
    chunk["updated_at"] = now


def _record_plan_failure(project_dir: Path, data: ProjectState, chunk_id: str, errors: list[str]) -> None:
    record_failure(
        data,
        stage=PLAN_VISUALS_STAGE,
        scope=_chunk_scope(chunk_id),
        errors=errors,
        recommended_next_action=PLAN_VISUALS_RECOMMENDED_NEXT_ACTION,
        context={"chunk_id": chunk_id, "planner": PLANNER_ID},
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _alignment_path(data: ProjectState) -> str:
    alignment = data.get("alignment")
    if isinstance(alignment, dict):
        script = alignment.get("script")
        if isinstance(script, dict):
            value = script.get("path")
            if isinstance(value, str) and value:
                return value
    return "alignment/script_alignment.json"


def _chunk_visuals(data: ProjectState, chunk_id: str) -> list[JsonObject]:
    return [visual for visual in data.get("visuals", []) if visual.get("chunk_id") == chunk_id]


def _ensure_visuals(data: ProjectState) -> list[JsonObject]:
    visuals = data.get("visuals")
    if visuals is None:
        visuals = []
        data["visuals"] = visuals
    return visuals


def _blocks_by_id(raw_blocks: Iterable[object]) -> dict[str, JsonObject]:
    blocks: dict[str, JsonObject] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        block = cast(JsonObject, raw_block)
        block_id = _string_field(block, "id")
        if block_id is not None:
            blocks[block_id] = block
    return blocks


def _evenly_select(candidates: list[JsonObject], max_visuals: int) -> list[JsonObject]:
    if len(candidates) <= max_visuals:
        return candidates
    if max_visuals == 1:
        return [candidates[len(candidates) // 2]]
    indexes: list[int] = []
    for offset in range(max_visuals):
        index = round(offset * (len(candidates) - 1) / (max_visuals - 1))
        if index not in indexes:
            indexes.append(index)
    return [candidates[index] for index in indexes]


def _params_for_text(text: str) -> dict[str, object]:
    clean = _clean_text(text)
    title = _truncate_words(clean, 72)
    params: dict[str, object] = {"title": title}
    if len(clean) > len(title):
        subtitle = _truncate_words(clean[len(title) :].strip(" -:;,.") or clean, 120)
        if subtitle and subtitle != title:
            params["subtitle"] = subtitle
    return params


def _visual_timing(
    block_start: float,
    block_end: float,
    chunk_start: float,
    chunk_end: float,
) -> tuple[float, float] | None:
    start = max(chunk_start, block_start)
    block_duration = max(0.0, block_end - block_start)
    duration = min(MAX_VISUAL_SECONDS, max(PREFERRED_VISUAL_SECONDS, block_duration))
    end = min(chunk_end, start + duration)
    if end - start < MIN_VISUAL_SECONDS and chunk_end - start >= MIN_VISUAL_SECONDS:
        end = start + MIN_VISUAL_SECONDS
    if end - start < MIN_VISUAL_SECONDS:
        return None
    return round(start, 6), round(end, 6)


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    words = text.split()
    output: list[str] = []
    for word in words:
        candidate = " ".join([*output, word])
        if len(candidate) > limit:
            break
        output.append(word)
    if not output:
        return text[:limit].rstrip()
    return " ".join(output).rstrip(" ,;:")


def _clean_text(text: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _validate_max_visuals(max_visuals: int) -> list[str]:
    if not isinstance(max_visuals, int) or isinstance(max_visuals, bool) or max_visuals <= 0:
        return ["max-visuals must be an integer greater than 0"]
    return []


def _visual_ids(visuals: list[JsonObject]) -> list[str]:
    ids: list[str] = []
    for visual in visuals:
        visual_id = _string_field(visual, "id")
        if visual_id is not None:
            ids.append(visual_id)
    return sorted(ids)


def _string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            output.append(item)
    return output


def _required_string(data: JsonObject, key: str) -> str:
    value = _string_field(data, key)
    if value is None:
        raise ValueError(f"Missing string field: {key}")
    return value


def _required_number(data: JsonObject, key: str) -> float:
    value = _number_field(data, key)
    if value is None:
        raise ValueError(f"Missing numeric field: {key}")
    return value


def _string_field(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _chunk_scope(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def _plan_visuals_result(
    project_dir: Path,
    chunk_id: str,
    success: bool,
    visual_ids: list[str],
    requires_human_decision: bool,
    reused_existing: bool,
    warnings: list[str],
    errors: list[str],
) -> PlanVisualsResult:
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "success": success,
        "planned_count": len(visual_ids),
        "visual_ids": visual_ids,
        "requires_human_decision": requires_human_decision,
        "reused_existing": reused_existing,
        "warnings": warnings,
        "errors": errors,
    }
