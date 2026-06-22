"""Codex-authored visual intents and template capability matching."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import build_pipeline_freshness, is_current
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectError, ProjectState, load_project, project_file_for
from .project import utc_now_iso, write_project
from .render_template import parse_params_json, resolve_template_file, validate_template_params
from .templates import TemplateInfo, build_inventory, validate_template_file
from .timeline import build_timeline_chunk_freshness
from .visual_quality import quality_policy, review_intent_records
from .visuals import build_visual_id, find_chunk, format_seconds, validate_chunk_reference, validate_time_range


PLANNER_ID = "codex_v1"
INTENT_STAGE = "visual_intent_plan"
INTENT_BIND_STAGE = "visual_intent_bind"
INTENT_RECOMMENDED_NEXT_ACTION = (
    "Fix the visual intent plan, chunk references, or template bindings, then rerun apply-visual-plan."
)
INTENT_BIND_RECOMMENDED_NEXT_ACTION = (
    "Fix the template capability, params, or required assets, then rerun bind-visual-intent."
)
INTENT_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class PlanningContextResult(TypedDict):
    project_dir: str
    chunk_id: str
    success: bool
    chunk: JsonObject | None
    planning: JsonObject
    blocks: list[JsonObject]
    warning_block_ids: list[str]
    visual_coverage: list[JsonObject]
    templates: list[JsonObject]
    errors: list[str]


class ApplyVisualPlanResult(TypedDict):
    project_dir: str
    chunk_id: str
    success: bool
    intent_count: int
    bound_count: int
    unbound_count: int
    capability_gap_count: int
    intent_ids: list[str]
    visual_ids: list[str]
    reused_existing: bool
    errors: list[str]


class VisualIntentsSummary(TypedDict):
    project_dir: str
    chunk_id: str | None
    gaps_only: bool
    total: int
    by_status: dict[str, int]
    intents: list[JsonObject]


class BindVisualIntentResult(TypedDict):
    project_dir: str
    intent_id: str
    chunk_id: str | None
    template_ref: str
    visual_id: str | None
    success: bool
    reused_existing: bool
    errors: list[str]


def build_planning_context(project_dir: Path, chunk_id: str) -> PlanningContextResult:
    data = load_project(project_dir)
    errors = _prerequisite_errors(project_dir, data)
    chunk = find_chunk(data, chunk_id)
    if chunk is None:
        errors.append(f"Chunk not found: {chunk_id}")

    artifact: JsonObject | None = None
    if not errors:
        artifact, read_errors = _read_alignment_artifact(project_dir, data)
        errors.extend(read_errors)

    blocks: list[JsonObject] = []
    if not errors and chunk is not None and artifact is not None:
        blocks, block_errors = _context_blocks(chunk, artifact)
        errors.extend(block_errors)

    context_chunk: JsonObject | None = None
    warning_ids: list[str] = []
    if chunk is not None:
        context_chunk = {
            "id": chunk.get("id"),
            "start": chunk.get("start"),
            "end": chunk.get("end"),
            "status": chunk.get("status"),
            "visual_mode": chunk.get("visual_mode"),
        }
        warning_ids = _string_list(chunk.get("warning_block_ids"))

    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "success": not errors,
        "chunk": context_chunk,
        "planning": _planning_summary(data, chunk_id, chunk),
        "blocks": blocks if not errors else [],
        "warning_block_ids": warning_ids,
        "visual_coverage": _visual_coverage(data, chunk_id),
        "templates": _template_context(),
        "errors": errors,
    }


def apply_visual_plan_from_json(project_dir: Path, chunk_id: str, plan_json: str) -> ApplyVisualPlanResult:
    data = load_project(project_dir)
    raw_intents, errors = _parse_plan_json(plan_json)
    errors.extend(_prerequisite_errors(project_dir, data))
    chunk = find_chunk(data, chunk_id)
    if chunk is None:
        errors.append(f"Chunk not found: {chunk_id}")

    existing_intents = _chunk_intents(data, chunk_id)
    existing_visuals = _chunk_visuals(data, chunk_id)
    if any(intent.get("planner") != PLANNER_ID for intent in existing_intents):
        errors.append(f"Chunk {chunk_id} contains visual intents owned by another planner.")
    if any(visual.get("planner") != PLANNER_ID for visual in existing_visuals):
        errors.append(f"Chunk {chunk_id} contains manual or heuristic visuals; Codex planning will not overwrite them.")

    planned_intents: list[JsonObject] = []
    planned_visuals: list[JsonObject] = []
    if not errors and chunk is not None:
        planned_intents, planned_visuals, build_errors = _build_plan_records(
            data,
            chunk,
            raw_intents,
            existing_intents,
            existing_visuals,
        )
        errors.extend(build_errors)
    if not errors and chunk is not None:
        review = review_intent_records(project_dir, chunk, planned_intents)
        if not review["passed"]:
            errors.extend(
                f"quality: {check['message']}"
                for check in review["checks"]
                if check.get("passed") is not True and isinstance(check.get("message"), str)
            )
            errors.extend(f"quality: {error}" for error in review["errors"])

    if errors:
        result = _apply_result(project_dir, chunk_id, False, [], [], False, errors)
        _record_intent_failure(project_dir, data, chunk_id, errors)
        return result
    if chunk is None:
        raise ValueError("Chunk cannot be missing after validation succeeds")

    if _same_plan(existing_intents, existing_visuals, planned_intents, planned_visuals):
        resolved = resolve_matching_failures(data, stage=INTENT_STAGE, scope=_chunk_scope(chunk_id))
        if resolved:
            data["project"]["updated_at"] = utc_now_iso()
            write_project(project_file_for(project_dir), data)
        return _apply_result(project_dir, chunk_id, True, planned_intents, planned_visuals, True, [])

    now = utc_now_iso()
    _replace_chunk_records(data, chunk_id, planned_intents, planned_visuals)
    _update_chunk_planning(chunk, planned_intents, now)
    resolve_matching_failures(data, stage=INTENT_STAGE, scope=_chunk_scope(chunk_id))
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)
    return _apply_result(project_dir, chunk_id, True, planned_intents, planned_visuals, False, [])


def build_visual_intents_summary(
    project_dir: Path,
    *,
    chunk_id: str | None = None,
    gaps_only: bool = False,
) -> VisualIntentsSummary:
    data = load_project(project_dir)
    if chunk_id is not None and find_chunk(data, chunk_id) is None:
        raise ProjectError(f"Chunk not found: {chunk_id}")
    intents = list(data.get("visual_intents", []))
    if chunk_id is not None:
        intents = [intent for intent in intents if intent.get("chunk_id") == chunk_id]
    if gaps_only:
        intents = [intent for intent in intents if intent.get("status") == "capability_gap"]
    statuses: Counter[str] = Counter(_string_field(intent, "status") or "unknown" for intent in intents)
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "gaps_only": gaps_only,
        "total": len(intents),
        "by_status": dict(sorted(statuses.items())),
        "intents": intents,
    }


def bind_visual_intent_from_json(
    project_dir: Path,
    intent_id: str,
    template_ref: str,
    params_json: str,
) -> BindVisualIntentResult:
    data = load_project(project_dir)
    params, errors = parse_params_json(params_json)
    errors.extend(_prerequisite_errors(project_dir, data))
    intent_index = _intent_index(data, intent_id)
    intent = data.get("visual_intents", [])[intent_index] if intent_index is not None else None
    chunk_id = _string_field(intent, "chunk_id") if intent is not None else None
    if intent is None:
        errors.append(f"Visual intent not found: {intent_id}")
    elif intent.get("planner") != PLANNER_ID:
        errors.append(f"Visual intent is not owned by {PLANNER_ID}: {intent_id}")
    chunk = find_chunk(data, chunk_id) if chunk_id is not None else None
    if intent is not None and chunk is None:
        errors.append(f"Visual intent references a missing chunk: {chunk_id or 'unknown'}")

    visual: JsonObject | None = None
    binding: JsonObject | None = None
    if not errors and intent is not None and chunk_id is not None:
        intent_type = _string_field(intent, "intent_type")
        start = _number_field(intent, "start")
        end = _number_field(intent, "end")
        if intent_type is None or start is None or end is None:
            errors.append(f"Visual intent record is malformed: {intent_id}")
        else:
            binding, visual, binding_errors = _build_binding(
                {"template_ref": template_ref, "params": cast(JsonValue, params)},
                intent_id=intent_id,
                intent_type=intent_type,
                chunk_id=chunk_id,
                start=start,
                end=end,
                visual_role=_string_field(intent, "visual_role"),
                motion=cast(JsonObject | None, intent.get("motion") if isinstance(intent.get("motion"), dict) else None),
                created_at=_created_at_map(_chunk_visuals(data, chunk_id)),
                now=utc_now_iso(),
            )
            errors.extend(binding_errors)

    if errors or intent is None or intent_index is None or chunk is None or chunk_id is None or visual is None or binding is None:
        _record_bind_failure(project_dir, data, intent_id, chunk_id, template_ref, errors)
        return _bind_result(project_dir, intent_id, chunk_id, template_ref, None, False, False, errors)

    candidate_intents: list[JsonObject] = []
    for index, existing in enumerate(data.get("visual_intents", [])):
        if index == intent_index:
            updated_intent = dict(existing)
            updated_intent["binding"] = binding
            updated_intent["visual_id"] = _required_string(visual, "id")
            updated_intent["status"] = "bound"
            candidate_intents.append(cast(JsonObject, updated_intent))
        elif existing.get("chunk_id") == chunk_id and existing.get("planner") == PLANNER_ID:
            candidate_intents.append(existing)
    review = review_intent_records(project_dir, chunk, candidate_intents)
    if not review["passed"]:
        quality_errors = [
            f"quality: {check['message']}"
            for check in review["checks"]
            if check.get("passed") is not True and isinstance(check.get("message"), str)
        ] + [f"quality: {error}" for error in review["errors"]]
        _record_bind_failure(project_dir, data, intent_id, chunk_id, template_ref, quality_errors)
        return _bind_result(project_dir, intent_id, chunk_id, template_ref, None, False, False, quality_errors)

    visual_id = _required_string(visual, "id")
    existing_visual = _intent_visual(data, intent_id)
    already_bound = (
        intent.get("status") == "bound"
        and intent.get("visual_id") == visual_id
        and intent.get("binding") == binding
        and existing_visual is not None
        and _semantic_records([existing_visual]) == _semantic_records([visual])
    )
    if already_bound:
        resolved = resolve_matching_failures(data, stage=INTENT_BIND_STAGE, scope=_intent_scope(intent_id))
        if resolved:
            data["project"]["updated_at"] = utc_now_iso()
            write_project(project_file_for(project_dir), data)
        return _bind_result(project_dir, intent_id, chunk_id, template_ref, visual_id, True, True, [])

    now = utc_now_iso()
    intent["binding"] = binding
    intent["visual_id"] = visual_id
    intent["status"] = "bound"
    intent["candidate_template_ids"] = cast(JsonValue, _candidate_templates(_ready_template_infos(), _required_string(intent, "intent_type")))
    intent["updated_at"] = now
    data["visuals"] = [
        item
        for item in data.get("visuals", [])
        if not (item.get("planner") == PLANNER_ID and item.get("intent_id") == intent_id)
    ] + [visual]
    _update_chunk_planning(chunk, _chunk_intents(data, chunk_id), now)
    resolve_matching_failures(data, stage=INTENT_BIND_STAGE, scope=_intent_scope(intent_id))
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)
    return _bind_result(project_dir, intent_id, chunk_id, template_ref, visual_id, True, False, [])


def format_planning_context(result: PlanningContextResult) -> str:
    lines = [
        f"Chunk: {result['chunk_id']}",
        f"Status: {'ready' if result['success'] else 'unavailable'}",
        f"Blocks: {len(result['blocks'])}",
        f"Existing visuals: {len(result['visual_coverage'])}",
        f"Templates: {len(result['templates'])}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_apply_visual_plan_result(result: ApplyVisualPlanResult) -> str:
    lines = [
        f"Chunk: {result['chunk_id']}",
        f"Status: {'applied' if result['success'] else 'failed'}",
        f"Intents: {result['intent_count']}",
        f"Bound: {result['bound_count']}",
        f"Unbound: {result['unbound_count']}",
        f"Capability gaps: {result['capability_gap_count']}",
    ]
    if result["reused_existing"]:
        lines.append("Reused existing plan: yes")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_visual_intents_summary(summary: VisualIntentsSummary) -> str:
    suffix = f" for {summary['chunk_id']}" if summary["chunk_id"] is not None else ""
    lines = [f"Visual intents{suffix}: {summary['total']}"]
    for intent in summary["intents"]:
        lines.append(
            f"- {_string_field(intent, 'id') or 'unknown'} "
            f"{format_seconds(_number_field(intent, 'start') or 0.0)} -> "
            f"{format_seconds(_number_field(intent, 'end') or 0.0)} "
            f"{_string_field(intent, 'status') or 'unknown'} "
            f"{_string_field(intent, 'intent_type') or 'unknown'}"
        )
    return "\n".join(lines)


def format_bind_visual_intent_result(result: BindVisualIntentResult) -> str:
    lines = [
        f"Intent: {result['intent_id']}",
        f"Chunk: {result['chunk_id'] or 'unknown'}",
        f"Template: {result['template_ref']}",
        f"Status: {'bound' if result['success'] else 'failed'}",
    ]
    if result["visual_id"] is not None:
        lines.append(f"Visual: {result['visual_id']}")
    if result["reused_existing"]:
        lines.append("Reused existing binding: yes")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def build_intent_id(
    chunk_id: str,
    intent_type: str,
    purpose: str,
    content: JsonObject,
    style_notes: str | None,
    source_block_ids: list[str],
    visual_role: str | None = None,
    motion: JsonObject | None = None,
) -> str:
    payload: JsonObject = {
        "chunk_id": chunk_id,
        "intent_type": intent_type,
        "purpose": purpose,
        "content": content,
        "style_notes": style_notes,
        "source_block_ids": cast(JsonValue, source_block_ids),
    }
    if visual_role is not None:
        payload["visual_role"] = visual_role
    if motion is not None:
        payload["motion"] = motion
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"intent_{hashlib.sha256(encoded).hexdigest()[:12]}"


def _build_plan_records(
    data: ProjectState,
    chunk: JsonObject,
    raw_intents: list[JsonObject],
    existing_intents: list[JsonObject],
    existing_visuals: list[JsonObject],
) -> tuple[list[JsonObject], list[JsonObject], list[str]]:
    chunk_id = _required_string(chunk, "id")
    allowed_block_ids = _string_list(chunk.get("alignment_block_ids")) + _string_list(chunk.get("warning_block_ids"))
    allowed_set = set(allowed_block_ids)
    block_order = {block_id: index for index, block_id in enumerate(allowed_block_ids)}
    inventory = build_inventory()
    template_infos = [item for item in inventory["templates"] if item["ready"]]
    existing_intent_created = _created_at_map(existing_intents)
    existing_visual_created = _created_at_map(existing_visuals)
    now = utc_now_iso()
    intents: list[JsonObject] = []
    visuals: list[JsonObject] = []
    errors: list[str] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_intents):
        prefix = f"intents[{index}]"
        intent_type = _required_input_string(raw, "intent_type", prefix, errors)
        purpose = _required_input_string(raw, "purpose", prefix, errors)
        start = _input_number(raw, "start", prefix, errors)
        end = _input_number(raw, "end", prefix, errors)
        source_ids = _input_string_list(raw, "source_block_ids", prefix, errors)
        content = _input_object(raw, "content", prefix, errors)
        style_notes = _optional_input_string(raw, "style_notes", prefix, errors)
        visual_role = _optional_input_string(raw, "visual_role", prefix, errors)
        motion_value = raw.get("motion")
        motion = cast(JsonObject, motion_value) if isinstance(motion_value, dict) else None
        binding = raw.get("binding")

        if intent_type is not None and not INTENT_TYPE_PATTERN.fullmatch(intent_type):
            errors.append(f"{prefix}.intent_type must be lowercase snake-case")
        if start is not None and end is not None:
            errors.extend(f"{prefix}: {error}" for error in validate_time_range(start, end))
            errors.extend(f"{prefix}: {error}" for error in validate_chunk_reference(data, chunk_id, start, end))
        if not source_ids:
            errors.append(f"{prefix}.source_block_ids must contain at least one block ID")
        unknown_ids = [block_id for block_id in source_ids if block_id not in allowed_set]
        if unknown_ids:
            errors.append(f"{prefix}.source_block_ids do not belong to {chunk_id}: {', '.join(unknown_ids)}")

        if (
            intent_type is None
            or purpose is None
            or start is None
            or end is None
            or content is None
            or not source_ids
            or unknown_ids
            or not INTENT_TYPE_PATTERN.fullmatch(intent_type)
        ):
            continue

        source_ids = sorted(set(source_ids), key=lambda item: block_order[item])
        intent_id = build_intent_id(
            chunk_id,
            intent_type,
            purpose,
            content,
            style_notes,
            source_ids,
            visual_role,
            motion,
        )
        if intent_id in seen_ids:
            errors.append(f"{prefix} duplicates intent {intent_id}")
            continue
        seen_ids.add(intent_id)

        candidates = _candidate_templates(template_infos, intent_type)
        status = "capability_gap" if not candidates else "unbound"
        binding_record: JsonObject | None = None
        visual_id: str | None = None
        if binding is not None:
            binding_record, visual_record, binding_errors = _build_binding(
                binding,
                intent_id=intent_id,
                intent_type=intent_type,
                chunk_id=chunk_id,
                start=start,
                end=end,
                visual_role=visual_role,
                motion=motion,
                created_at=existing_visual_created,
                now=now,
            )
            errors.extend(f"{prefix}.binding: {error}" for error in binding_errors)
            if visual_record is not None:
                visual_id = _required_string(visual_record, "id")
                visuals.append(visual_record)
                status = "bound"

        record: JsonObject = {
            "id": intent_id,
            "chunk_id": chunk_id,
            "start": start,
            "end": end,
            "source_block_ids": cast(JsonValue, source_ids),
            "intent_type": intent_type,
            "purpose": purpose,
            "content": content,
            "style_notes": style_notes,
            "visual_role": visual_role,
            "motion": cast(JsonValue, motion),
            "status": status,
            "candidate_template_ids": cast(JsonValue, candidates),
            "binding": binding_record,
            "visual_id": visual_id,
            "planner": PLANNER_ID,
            "created_at": existing_intent_created.get(intent_id, now),
            "updated_at": now,
        }
        intents.append(record)

    if not raw_intents:
        errors.append("Plan must contain at least one intent")
    return intents, visuals, errors


def _build_binding(
    value: JsonValue,
    *,
    intent_id: str,
    intent_type: str,
    chunk_id: str,
    start: float,
    end: float,
    visual_role: str | None,
    motion: JsonObject | None,
    created_at: dict[str, str],
    now: str,
) -> tuple[JsonObject | None, JsonObject | None, list[str]]:
    if not isinstance(value, dict):
        return None, None, ["must be an object or null"]
    template_ref = _string_field(value, "template_ref")
    params_value = value.get("params")
    errors: list[str] = []
    if template_ref is None:
        errors.append("template_ref must be a non-empty string")
    if not isinstance(params_value, dict):
        errors.append("params must be an object")
    if errors or template_ref is None or not isinstance(params_value, dict):
        return None, None, errors
    params = cast(dict[str, object], params_value)
    template_file = resolve_template_file(template_ref)
    if template_file is None:
        return None, None, [f"Template not found: {template_ref}"]
    info = validate_template_file(template_file)
    if not info["valid"]:
        return None, None, list(info["errors"])
    if intent_type not in info["capabilities"]:
        return None, None, [f"Template {info['template_id'] or template_ref} does not support {intent_type}"]
    validation = validate_template_params(template_ref, params)
    if not validation["valid"] or validation["template_id"] is None:
        return None, None, list(validation["errors"])
    visual_id = build_visual_id(template_ref, params, start, end, chunk_id=chunk_id)
    binding: JsonObject = {
        "template_ref": template_ref,
        "template_id": validation["template_id"],
        "params": cast(JsonValue, params),
    }
    visual: JsonObject = {
        "id": visual_id,
        "template_ref": template_ref,
        "template_id": validation["template_id"],
        "params": cast(JsonValue, params),
        "start": start,
        "end": end,
        "status": "planned",
        "preview_id": None,
        "chunk_id": chunk_id,
        "planner": PLANNER_ID,
        "intent_id": intent_id,
        "visual_role": visual_role,
        "motion": cast(JsonValue, motion),
        "created_at": created_at.get(visual_id, now),
        "updated_at": now,
    }
    return binding, visual, []


def _parse_plan_json(plan_json: str) -> tuple[list[JsonObject], list[str]]:
    try:
        raw: object = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        return [], [f"Invalid plan JSON: {exc.msg}"]
    if not isinstance(raw, dict):
        return [], ["Plan JSON must be an object"]
    intents = raw.get("intents")
    if not isinstance(intents, list):
        return [], ["Plan JSON must contain an intents list"]
    output: list[JsonObject] = []
    errors: list[str] = []
    for index, item in enumerate(intents):
        if not isinstance(item, dict):
            errors.append(f"intents[{index}] must be an object")
            continue
        output.append(cast(JsonObject, item))
    return output, errors


def _context_blocks(chunk: JsonObject, artifact: JsonObject) -> tuple[list[JsonObject], list[str]]:
    raw_blocks = artifact.get("blocks")
    if not isinstance(raw_blocks, list):
        return [], ["Alignment artifact must contain a blocks list"]
    wanted = set(_string_list(chunk.get("alignment_block_ids")))
    blocks: list[JsonObject] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            continue
        block = cast(JsonObject, raw)
        block_id = _string_field(block, "id")
        if block_id not in wanted:
            continue
        compact: JsonObject = {
            "id": block_id,
            "text": block.get("text"),
            "start": block.get("start"),
            "end": block.get("end"),
            "speaker": block.get("speaker"),
        }
        blocks.append(compact)
    order = {block_id: index for index, block_id in enumerate(_string_list(chunk.get("alignment_block_ids")))}
    blocks.sort(key=lambda item: order.get(_string_field(item, "id") or "", len(order)))
    return blocks, []


def _template_context() -> list[JsonObject]:
    output: list[JsonObject] = []
    for info in build_inventory()["templates"]:
        if not info["ready"]:
            continue
        description = info["metadata"].get("description")
        output.append(
            {
                "template_id": info["template_id"],
                "output_type": info["output_type"],
                "description": description if isinstance(description, str) else "",
                "capabilities": cast(JsonValue, info["capabilities"]),
            }
        )
    return output


def _candidate_templates(infos: list[TemplateInfo], intent_type: str) -> list[str]:
    candidates = [
        template_id
        for info in infos
        if intent_type in info["capabilities"]
        and (template_id := info["template_id"]) is not None
    ]
    return sorted(candidates)


def _ready_template_infos() -> list[TemplateInfo]:
    return [item for item in build_inventory()["templates"] if item["ready"]]


def _planning_summary(data: ProjectState, chunk_id: str, chunk: JsonObject | None) -> JsonObject:
    intents = _chunk_intents(data, chunk_id)
    statuses: Counter[str] = Counter(_string_field(intent, "status") or "unknown" for intent in intents)
    return {
        "state": chunk.get("visual_planning") if chunk is not None else None,
        "intent_count": len(intents),
        "by_status": dict(sorted(statuses.items())),
        "quality_policy": cast(JsonValue, quality_policy()),
    }


def _visual_coverage(data: ProjectState, chunk_id: str) -> list[JsonObject]:
    coverage: list[JsonObject] = []
    for visual in _chunk_visuals(data, chunk_id):
        coverage.append(
            {
                "id": visual.get("id"),
                "start": visual.get("start"),
                "end": visual.get("end"),
                "status": visual.get("status"),
                "template_id": visual.get("template_id"),
                "planner": visual.get("planner"),
            }
        )
    return coverage


def _prerequisite_errors(project_dir: Path, data: ProjectState) -> list[str]:
    errors: list[str] = []
    pipeline = build_pipeline_freshness(project_dir, data)
    for label in ("raw", "audio", "transcript", "alignment"):
        result = pipeline[label]
        if not is_current(result):
            errors.append(f"{label} is not current: {result['state']} ({result['reason']})")
    timeline = build_timeline_chunk_freshness(project_dir, data, pipeline)
    for label in ("timeline", "chunking"):
        result = timeline[label]
        if not is_current(result):
            errors.append(f"{label} is not current: {result['state']} ({result['reason']})")
    return errors


def _read_alignment_artifact(project_dir: Path, data: ProjectState) -> tuple[JsonObject | None, list[str]]:
    path_value = "alignment/script_alignment.json"
    alignment = data.get("alignment")
    if isinstance(alignment, dict):
        script = alignment.get("script")
        if isinstance(script, dict) and isinstance(script.get("path"), str):
            path_value = cast(str, script["path"])
    path = artifact_path(project_dir, data, path_value)
    if not path.is_file():
        return None, [f"Missing alignment artifact: {path}"]
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON in alignment artifact: {exc.msg}"]
    except OSError as exc:
        return None, [f"Could not read alignment artifact: {exc}"]
    if not isinstance(raw, dict):
        return None, ["Alignment artifact must be a JSON object"]
    return cast(JsonObject, raw), []


def _replace_chunk_records(
    data: ProjectState,
    chunk_id: str,
    intents: list[JsonObject],
    visuals: list[JsonObject],
) -> None:
    data["visual_intents"] = [
        intent
        for intent in data.get("visual_intents", [])
        if not (intent.get("chunk_id") == chunk_id and intent.get("planner") == PLANNER_ID)
    ] + intents
    data["visuals"] = [
        visual
        for visual in data.get("visuals", [])
        if not (visual.get("chunk_id") == chunk_id and visual.get("planner") == PLANNER_ID)
    ] + visuals


def _update_chunk_planning(chunk: JsonObject, intents: list[JsonObject], now: str) -> None:
    statuses: Counter[str] = Counter(_string_field(intent, "status") or "unknown" for intent in intents)
    gap_count = statuses["capability_gap"]
    unbound_count = statuses["unbound"]
    if gap_count:
        planning_status = "capability_gaps"
    elif unbound_count:
        planning_status = "needs_binding"
    else:
        planning_status = "bound"
    chunk["visual_planning"] = {
        "planner": PLANNER_ID,
        "status": planning_status,
        "intent_count": len(intents),
        "bound_count": statuses["bound"],
        "unbound_count": unbound_count,
        "capability_gap_count": gap_count,
        "updated_at": now,
    }
    chunk["visual_mode"] = "visuals" if planning_status == "bound" else "undecided"
    chunk["status"] = "new"
    chunk["updated_at"] = now
    if planning_status == "bound":
        chunk.pop("camera_only_at", None)


def _same_plan(
    existing_intents: list[JsonObject],
    existing_visuals: list[JsonObject],
    planned_intents: list[JsonObject],
    planned_visuals: list[JsonObject],
) -> bool:
    return _semantic_records(existing_intents) == _semantic_records(planned_intents) and _semantic_records(
        existing_visuals
    ) == _semantic_records(planned_visuals)


def _semantic_records(records: list[JsonObject]) -> list[JsonObject]:
    output: list[JsonObject] = []
    for record in records:
        output.append({key: value for key, value in record.items() if key not in {"created_at", "updated_at"}})
    output.sort(key=lambda item: str(item.get("id", "")))
    return output


def _created_at_map(records: list[JsonObject]) -> dict[str, str]:
    output: dict[str, str] = {}
    for record in records:
        record_id = _string_field(record, "id")
        created_at = _string_field(record, "created_at")
        if record_id is not None and created_at is not None:
            output[record_id] = created_at
    return output


def _record_intent_failure(project_dir: Path, data: ProjectState, chunk_id: str, errors: list[str]) -> None:
    record_failure(
        data,
        stage=INTENT_STAGE,
        scope=_chunk_scope(chunk_id),
        errors=errors,
        recommended_next_action=INTENT_RECOMMENDED_NEXT_ACTION,
        context={"chunk_id": chunk_id, "planner": PLANNER_ID},
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_bind_failure(
    project_dir: Path,
    data: ProjectState,
    intent_id: str,
    chunk_id: str | None,
    template_ref: str,
    errors: list[str],
) -> None:
    record_failure(
        data,
        stage=INTENT_BIND_STAGE,
        scope=_intent_scope(intent_id),
        errors=errors or ["Visual intent binding could not be completed."],
        recommended_next_action=INTENT_BIND_RECOMMENDED_NEXT_ACTION,
        context={"intent_id": intent_id, "chunk_id": chunk_id, "template_ref": template_ref},
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _apply_result(
    project_dir: Path,
    chunk_id: str,
    success: bool,
    intents: list[JsonObject],
    visuals: list[JsonObject],
    reused_existing: bool,
    errors: list[str],
) -> ApplyVisualPlanResult:
    statuses: Counter[str] = Counter(_string_field(intent, "status") or "unknown" for intent in intents)
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "success": success,
        "intent_count": len(intents),
        "bound_count": statuses["bound"],
        "unbound_count": statuses["unbound"],
        "capability_gap_count": statuses["capability_gap"],
        "intent_ids": [_required_string(intent, "id") for intent in intents],
        "visual_ids": [_required_string(visual, "id") for visual in visuals],
        "reused_existing": reused_existing,
        "errors": errors,
    }


def _chunk_intents(data: ProjectState, chunk_id: str) -> list[JsonObject]:
    return [intent for intent in data.get("visual_intents", []) if intent.get("chunk_id") == chunk_id]


def _intent_index(data: ProjectState, intent_id: str) -> int | None:
    return next(
        (index for index, intent in enumerate(data.get("visual_intents", [])) if intent.get("id") == intent_id),
        None,
    )


def _intent_visual(data: ProjectState, intent_id: str) -> JsonObject | None:
    return next(
        (
            visual
            for visual in data.get("visuals", [])
            if visual.get("planner") == PLANNER_ID and visual.get("intent_id") == intent_id
        ),
        None,
    )


def _chunk_visuals(data: ProjectState, chunk_id: str) -> list[JsonObject]:
    return [visual for visual in data.get("visuals", []) if visual.get("chunk_id") == chunk_id]


def _required_input_string(data: JsonObject, key: str, prefix: str, errors: list[str]) -> str | None:
    value = _string_field(data, key)
    if value is None:
        errors.append(f"{prefix}.{key} must be a non-empty string")
    return value


def _optional_input_string(data: JsonObject, key: str, prefix: str, errors: list[str]) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{prefix}.{key} must be a string or null")
        return None
    return value.strip() or None


def _input_number(data: JsonObject, key: str, prefix: str, errors: list[str]) -> float | None:
    value = _number_field(data, key)
    if value is None:
        errors.append(f"{prefix}.{key} must be a finite number")
    return value


def _input_string_list(data: JsonObject, key: str, prefix: str, errors: list[str]) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        errors.append(f"{prefix}.{key} must be a list of non-empty strings")
        return []
    return cast(list[str], value)


def _input_object(data: JsonObject, key: str, prefix: str, errors: list[str]) -> JsonObject | None:
    value = data.get(key)
    if not isinstance(value, dict):
        errors.append(f"{prefix}.{key} must be an object")
        return None
    return cast(JsonObject, value)


def _string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _required_string(data: JsonObject, key: str) -> str:
    value = _string_field(data, key)
    if value is None:
        raise ValueError(f"Missing string field: {key}")
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


def _intent_scope(intent_id: str) -> str:
    return f"intent:{intent_id}"


def _bind_result(
    project_dir: Path,
    intent_id: str,
    chunk_id: str | None,
    template_ref: str,
    visual_id: str | None,
    success: bool,
    reused: bool,
    errors: list[str],
) -> BindVisualIntentResult:
    return {
        "project_dir": str(project_dir),
        "intent_id": intent_id,
        "chunk_id": chunk_id,
        "template_ref": template_ref,
        "visual_id": visual_id,
        "success": success,
        "reused_existing": reused,
        "errors": errors,
    }
