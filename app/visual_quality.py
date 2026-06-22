"""Visual density, diversity, and motion quality review."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

from .project import JsonObject, JsonValue, ProjectError, ProjectState, load_project
from .render_template import resolve_template_file
from .templates import validate_template_file


PLANNER_ID = "codex_v1"
GENERIC_INTENT_TYPES = {"title_card", "quote", "key_point"}
ALLOWED_VISUAL_ROLES = {"hook", "context", "proof", "contrast", "transition", "emphasis", "recap", "outro"}
ALLOWED_TRANSITIONS = {"cut", "fade", "slide", "type_on", "zoom"}
MAX_UNCOVERED_GAP_SECONDS = 8.0
MAX_MOTION_BEAT_INTERVAL_SECONDS = 12.0


class QualityPolicy(TypedDict):
    planner: str
    hard_min_visual_seconds: float
    target_visual_seconds: float
    max_uncovered_gap_seconds: float
    max_motion_beat_interval_seconds: float
    require_first_visual_at_chunk_start: bool
    require_mp4_for_codex_bindings: bool
    long_chunk_seconds: float
    long_chunk_min_distinct_intent_types: int
    generic_intent_max_ratio: float
    max_consecutive_same_intent_type: int
    max_consecutive_same_template: int


class VisualPlanReview(TypedDict):
    project_dir: str
    chunk_id: str
    passed: bool
    policy: QualityPolicy
    chunk: JsonObject | None
    counts: JsonObject
    checks: list[JsonObject]
    errors: list[str]


def quality_policy() -> QualityPolicy:
    return {
        "planner": PLANNER_ID,
        "hard_min_visual_seconds": 10.0,
        "target_visual_seconds": 7.5,
        "max_uncovered_gap_seconds": MAX_UNCOVERED_GAP_SECONDS,
        "max_motion_beat_interval_seconds": MAX_MOTION_BEAT_INTERVAL_SECONDS,
        "require_first_visual_at_chunk_start": True,
        "require_mp4_for_codex_bindings": True,
        "long_chunk_seconds": 120.0,
        "long_chunk_min_distinct_intent_types": 7,
        "generic_intent_max_ratio": 0.25,
        "max_consecutive_same_intent_type": 2,
        "max_consecutive_same_template": 2,
    }


def build_visual_plan_review(project_dir: Path, chunk_id: str) -> VisualPlanReview:
    data = load_project(project_dir)
    chunk = _find_chunk(data, chunk_id)
    if chunk is None:
        return _review_result(project_dir, chunk_id, None, [], [f"Chunk not found: {chunk_id}"])
    intents = _chunk_codex_intents(data, chunk_id)
    return review_intent_records(project_dir, chunk, intents)


def review_intent_records(project_dir: Path, chunk: JsonObject, intents: list[JsonObject]) -> VisualPlanReview:
    chunk_id = _string_field(chunk, "id") or "unknown"
    start = _number_field(chunk, "start")
    end = _number_field(chunk, "end")
    errors: list[str] = []
    if start is None or end is None or end <= start:
        errors.append(f"Chunk record is invalid: {chunk_id}")
        return _review_result(project_dir, chunk_id, chunk, [], errors)
    checks = _build_checks(chunk_id, start, end, intents)
    return _review_result(project_dir, chunk_id, chunk, checks, errors)


def format_visual_plan_review(review: VisualPlanReview) -> str:
    lines = [
        f"Chunk: {review['chunk_id']}",
        f"Status: {'passed' if review['passed'] else 'failed'}",
    ]
    counts = review["counts"]
    lines.append(
        f"Visuals: {counts.get('intent_count', 0)} "
        f"(minimum {counts.get('hard_min_visuals', 0)}, target {counts.get('target_visuals', 0)})"
    )
    lines.append(f"Distinct intent types: {counts.get('distinct_intent_types', 0)}")
    for check in review["checks"]:
        if check.get("passed") is not True:
            lines.append(f"Failed: {check.get('message')}")
    for error in review["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _build_checks(chunk_id: str, start: float, end: float, intents: list[JsonObject]) -> list[JsonObject]:
    duration = end - start
    hard_min = max(1, math.ceil(duration / 10.0))
    target = max(hard_min, math.ceil(duration / 7.5))
    sorted_intents = sorted(intents, key=lambda item: (_sort_number(item, "start"), _sort_number(item, "end"), str(item.get("id", ""))))
    checks: list[JsonObject] = []
    checks.append(
        _check(
            "density",
            len(intents) >= hard_min,
            f"{chunk_id} needs at least {hard_min} visuals; found {len(intents)}.",
            actual=len(intents),
        )
    )
    first_start = _number_field(sorted_intents[0], "start") if sorted_intents else None
    checks.append(
        _check(
            "opening_hook",
            first_start is not None and abs(first_start - start) <= 0.05,
            f"{chunk_id} must start with a visual at {start:.3f}s.",
        )
    )
    max_gap = _max_uncovered_gap(start, end, sorted_intents)
    checks.append(
        _check(
            "coverage_gap",
            max_gap <= MAX_UNCOVERED_GAP_SECONDS,
            f"{chunk_id} has an uncovered visual gap of {max_gap:.3f}s; maximum is {MAX_UNCOVERED_GAP_SECONDS:.1f}s.",
        )
    )
    intent_types = [_string_field(intent, "intent_type") or "" for intent in sorted_intents]
    distinct_types = len({item for item in intent_types if item})
    checks.append(_check("distinct_count", True, f"{chunk_id} uses {distinct_types} distinct intent types.", actual=distinct_types))
    if duration > 120.0:
        checks.append(
            _check(
                "diversity",
                distinct_types >= 7,
                f"{chunk_id} needs at least 7 distinct intent types for a long chunk; found {distinct_types}.",
            )
        )
    generic_count = sum(1 for item in intent_types if item in GENERIC_INTENT_TYPES)
    max_generic = math.floor(len(intents) * 0.25)
    checks.append(
        _check(
            "generic_overuse",
            len(intents) == 0 or generic_count <= max_generic,
            f"{chunk_id} uses {generic_count} generic card-like intents; maximum is {max_generic}.",
        )
    )
    checks.extend(_motion_checks(sorted_intents))
    checks.append(
        _check(
            "intent_sequence",
            _max_run(intent_types) <= 2,
            f"{chunk_id} repeats the same intent type more than twice consecutively.",
        )
    )
    template_ids = [_binding_template_id(intent) or "" for intent in sorted_intents if _binding_template_id(intent)]
    checks.append(
        _check(
            "template_sequence",
            _max_run(template_ids) <= 2,
            f"{chunk_id} repeats the same template more than twice consecutively.",
        )
    )
    return checks


def _motion_checks(intents: list[JsonObject]) -> list[JsonObject]:
    checks: list[JsonObject] = []
    for index, intent in enumerate(intents):
        prefix = f"intent {intent.get('id') or index}"
        role = _string_field(intent, "visual_role")
        checks.append(_check("visual_role", role in ALLOWED_VISUAL_ROLES, f"{prefix} must declare a valid visual_role."))
        motion = intent.get("motion")
        if not isinstance(motion, dict):
            checks.append(_check("motion", False, f"{prefix} must declare motion planning."))
            continue
        output_type = motion.get("preferred_output_type")
        checks.append(_check("motion_output_type", output_type == "mp4", f"{prefix} must prefer mp4 animation."))
        checks.append(
            _check(
                "motion_transition_in",
                motion.get("transition_in") in ALLOWED_TRANSITIONS,
                f"{prefix} must declare a valid transition_in.",
            )
        )
        checks.append(
            _check(
                "motion_transition_out",
                motion.get("transition_out") in ALLOWED_TRANSITIONS,
                f"{prefix} must declare a valid transition_out.",
            )
        )
        notes = motion.get("animation_notes")
        checks.append(_check("motion_notes", isinstance(notes, str) and bool(notes.strip()), f"{prefix} needs animation_notes."))
        start = _number_field(intent, "start")
        end = _number_field(intent, "end")
        beats = _number_list(motion.get("beats"))
        duration = (end - start) if start is not None and end is not None else 0.0
        if duration > MAX_MOTION_BEAT_INTERVAL_SECONDS:
            interval = _max_motion_interval(duration, beats)
            checks.append(
                _check(
                    "motion_beats",
                    interval <= MAX_MOTION_BEAT_INTERVAL_SECONDS,
                    f"{prefix} has a {interval:.3f}s motion-beat gap; maximum is {MAX_MOTION_BEAT_INTERVAL_SECONDS:.1f}s.",
                )
            )
        binding = intent.get("binding")
        if isinstance(binding, dict):
            template_ref = _string_field(binding, "template_ref")
            output_type = _template_output_type(template_ref) if template_ref is not None else None
            passed = output_type == "mp4"
            message = (
                f"{prefix} is bound to animated MP4 template {template_ref or 'unknown'}."
                if passed
                else f"{prefix} is bound to static/non-MP4 template {template_ref or 'unknown'}."
            )
            checks.append(
                _check(
                    "animated_binding",
                    passed,
                    message,
                )
            )
    return checks


def _review_result(
    project_dir: Path,
    chunk_id: str,
    chunk: JsonObject | None,
    checks: list[JsonObject],
    errors: list[str],
) -> VisualPlanReview:
    intents = _review_intents_from_checks(chunk, checks)
    _ = intents
    passed = not errors and bool(checks) and all(check.get("passed") is True for check in checks)
    counts = _counts(chunk, checks)
    return {
        "project_dir": str(project_dir),
        "chunk_id": chunk_id,
        "passed": passed,
        "policy": quality_policy(),
        "chunk": _compact_chunk(chunk),
        "counts": counts,
        "checks": checks,
        "errors": errors,
    }


def _counts(chunk: JsonObject | None, checks: list[JsonObject]) -> JsonObject:
    if chunk is None:
        return {"intent_count": 0, "hard_min_visuals": 0, "target_visuals": 0, "distinct_intent_types": 0}
    start = _number_field(chunk, "start") or 0.0
    end = _number_field(chunk, "end") or start
    duration = max(0.0, end - start)
    density_check = next((check for check in checks if check.get("id") == "density"), None)
    intent_count = _number_field(density_check or {}, "actual") if density_check is not None else None
    distinct_check = next((check for check in checks if check.get("id") == "distinct_count"), None)
    distinct_count = _number_field(distinct_check or {}, "actual") if distinct_check is not None else None
    return {
        "duration_seconds": duration,
        "intent_count": int(intent_count or 0),
        "hard_min_visuals": max(1, math.ceil(duration / 10.0)) if duration else 0,
        "target_visuals": max(1, math.ceil(duration / 7.5)) if duration else 0,
        "distinct_intent_types": int(distinct_count or 0),
    }


def _compact_chunk(chunk: JsonObject | None) -> JsonObject | None:
    if chunk is None:
        return None
    return {
        "id": chunk.get("id"),
        "start": chunk.get("start"),
        "end": chunk.get("end"),
        "status": chunk.get("status"),
        "visual_mode": chunk.get("visual_mode"),
    }


def _review_intents_from_checks(chunk: JsonObject | None, checks: list[JsonObject]) -> list[JsonObject]:
    _ = chunk, checks
    return []


def _check(check_id: str, passed: bool, message: str, *, actual: float | int | None = None) -> JsonObject:
    record: JsonObject = {"id": check_id, "passed": passed, "message": message}
    if actual is not None:
        record["actual"] = actual
    return record


def _max_uncovered_gap(start: float, end: float, intents: list[JsonObject]) -> float:
    cursor = start
    max_gap = 0.0
    for intent in intents:
        intent_start = _number_field(intent, "start")
        intent_end = _number_field(intent, "end")
        if intent_start is None or intent_end is None or intent_end <= intent_start:
            continue
        if intent_start > cursor:
            max_gap = max(max_gap, intent_start - cursor)
        cursor = max(cursor, intent_end)
    if cursor < end:
        max_gap = max(max_gap, end - cursor)
    return max_gap


def _max_motion_interval(duration: float, beats: list[float]) -> float:
    points = sorted({0.0, duration, *(beat for beat in beats if 0.0 <= beat <= duration)})
    if len(points) < 2:
        return duration
    return max(end - start for start, end in zip(points, points[1:], strict=False))


def _max_run(values: list[str]) -> int:
    longest = 0
    current_value = None
    current_count = 0
    for value in values:
        if not value:
            current_value = None
            current_count = 0
            continue
        if value == current_value:
            current_count += 1
        else:
            current_value = value
            current_count = 1
        longest = max(longest, current_count)
    return longest


def _template_output_type(template_ref: str | None) -> str | None:
    if template_ref is None:
        return None
    template_file = resolve_template_file(template_ref)
    if template_file is None:
        return None
    info = validate_template_file(template_file)
    return info["output_type"] if info["valid"] else None


def _binding_template_id(intent: JsonObject) -> str | None:
    binding = intent.get("binding")
    if not isinstance(binding, dict):
        return None
    return _string_field(cast(JsonObject, binding), "template_id") or _string_field(cast(JsonObject, binding), "template_ref")


def _chunk_codex_intents(data: ProjectState, chunk_id: str) -> list[JsonObject]:
    return [
        intent
        for intent in data.get("visual_intents", [])
        if intent.get("chunk_id") == chunk_id and intent.get("planner") == PLANNER_ID
    ]


def _find_chunk(data: ProjectState, chunk_id: str) -> JsonObject | None:
    for chunk in data["chunks"]:
        if chunk.get("id") == chunk_id:
            return chunk
    return None


def _number_list(value: JsonValue | None) -> list[float]:
    if not isinstance(value, list):
        return []
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            continue
        numbers.append(float(item))
    return numbers


def _string_field(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) and math.isfinite(float(value)) else None


def _sort_number(data: JsonObject, key: str) -> float:
    value = _number_field(data, key)
    return value if value is not None else math.inf
