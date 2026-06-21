"""Failure lifecycle management for resumable projects."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for, utc_now_iso, write_project


FAILURE_STATUS_ACTIVE = "active"
FAILURE_STATUS_RESOLVED = "resolved"
FAILURE_ID_PATTERN = re.compile(r"^failure_(\d+)$")


class FailuresSummary(TypedDict):
    project_dir: str
    active_count: int
    resolved_count: int
    total_count: int
    failures: list[JsonObject]


class ResolveFailureResult(TypedDict):
    project_dir: str
    failure_id: str
    success: bool
    already_resolved: bool
    errors: list[str]


def template_failure_scope(template_ref: str) -> str:
    return f"template:{template_ref}"


def visual_failure_scope(visual_id: str) -> str:
    return f"visual:{visual_id}"


def chunk_failure_scope(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def media_failure_scope(media_path: str) -> str:
    return f"media:{media_path}"


def normalize_failures(data: ProjectState) -> bool:
    failures = data["failures"]
    reserved_ids = {
        failure_id
        for failure in failures
        if (failure_id := _valid_failure_id(failure.get("id"))) is not None
    }
    used_ids: set[str] = set()
    next_number = 1
    changed = False

    for failure in failures:
        failure_id = _valid_failure_id(failure.get("id"))
        if failure_id is None or failure_id in used_ids:
            while _format_failure_id(next_number) in reserved_ids or _format_failure_id(next_number) in used_ids:
                next_number += 1
            failure_id = _format_failure_id(next_number)
            failure["id"] = failure_id
            next_number += 1
            changed = True
        used_ids.add(failure_id)

        status = failure.get("status")
        if status not in (FAILURE_STATUS_ACTIVE, FAILURE_STATUS_RESOLVED):
            failure["status"] = FAILURE_STATUS_ACTIVE
            status = FAILURE_STATUS_ACTIVE
            changed = True

        scope = failure.get("scope")
        if not isinstance(scope, str) or not scope:
            failure["scope"] = _derive_legacy_scope(failure, failure_id)
            changed = True

        attempt_count = failure.get("attempt_count")
        if not isinstance(attempt_count, int) or isinstance(attempt_count, bool) or attempt_count < 1:
            failure["attempt_count"] = 1
            changed = True

        created_at = _timestamp_field(failure, "created_at", data["project"]["created_at"])
        if failure.get("created_at") != created_at:
            failure["created_at"] = created_at
            changed = True

        updated_at = _timestamp_field(failure, "updated_at", created_at)
        if failure.get("updated_at") != updated_at:
            failure["updated_at"] = updated_at
            changed = True

        resolved_at = failure.get("resolved_at")
        if status == FAILURE_STATUS_ACTIVE and resolved_at is not None:
            failure["resolved_at"] = None
            changed = True
        elif status == FAILURE_STATUS_ACTIVE and "resolved_at" not in failure:
            failure["resolved_at"] = None
            changed = True
        elif status == FAILURE_STATUS_RESOLVED and not isinstance(resolved_at, str):
            failure["resolved_at"] = updated_at
            changed = True

    return changed


def record_failure(
    data: ProjectState,
    *,
    stage: str,
    scope: str,
    errors: list[str],
    recommended_next_action: str,
    context: JsonObject | None = None,
) -> JsonObject:
    normalize_failures(data)
    now = utc_now_iso()
    existing = _find_active_failure(data["failures"], stage, scope)
    error_values: list[JsonValue] = []
    for error in errors:
        error_values.append(error)

    if existing is None:
        new_failure: JsonObject = {
            "id": _next_failure_id(data["failures"]),
            "stage": stage,
            "scope": scope,
            "status": FAILURE_STATUS_ACTIVE,
            "errors": error_values,
            "recommended_next_action": recommended_next_action,
            "attempt_count": 1,
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        }
        data["failures"].append(new_failure)
        existing = new_failure
    else:
        attempt_count = existing.get("attempt_count")
        existing["attempt_count"] = attempt_count + 1 if isinstance(attempt_count, int) else 2
        existing["errors"] = error_values
        existing["recommended_next_action"] = recommended_next_action
        existing["updated_at"] = now
        existing["resolved_at"] = None

    if context is not None:
        for key, value in context.items():
            if key not in _reserved_fields():
                existing[key] = value
    return existing


def resolve_matching_failures(data: ProjectState, *, stage: str, scope: str) -> list[str]:
    normalize_failures(data)
    now = utc_now_iso()
    resolved_ids: list[str] = []
    for failure in data["failures"]:
        if (
            failure.get("stage") == stage
            and failure.get("scope") == scope
            and failure_status(failure) == FAILURE_STATUS_ACTIVE
        ):
            failure["status"] = FAILURE_STATUS_RESOLVED
            failure["updated_at"] = now
            failure["resolved_at"] = now
            failure_id = failure.get("id")
            if isinstance(failure_id, str):
                resolved_ids.append(failure_id)
    return resolved_ids


def active_failures(data: ProjectState) -> list[JsonObject]:
    normalize_failures(data)
    return [failure for failure in data["failures"] if failure_status(failure) == FAILURE_STATUS_ACTIVE]


def build_failures_summary(project_dir: Path, *, include_resolved: bool = False) -> FailuresSummary:
    data = load_project(project_dir)
    normalize_failures(data)
    active = active_failures(data)
    resolved = [failure for failure in data["failures"] if failure_status(failure) == FAILURE_STATUS_RESOLVED]
    selected = data["failures"] if include_resolved else active
    return {
        "project_dir": str(project_dir),
        "active_count": len(active),
        "resolved_count": len(resolved),
        "total_count": len(data["failures"]),
        "failures": selected,
    }


def resolve_failure_by_id(project_dir: Path, failure_id: str) -> ResolveFailureResult:
    data = load_project(project_dir)
    normalized = normalize_failures(data)
    failure = _find_failure_by_id(data["failures"], failure_id)
    if failure is None:
        return _resolve_result(project_dir, failure_id, False, False, [f"Failure not found: {failure_id}"])

    already_resolved = failure_status(failure) == FAILURE_STATUS_RESOLVED
    if not already_resolved:
        now = utc_now_iso()
        failure["status"] = FAILURE_STATUS_RESOLVED
        failure["updated_at"] = now
        failure["resolved_at"] = now

    if normalized or not already_resolved:
        data["project"]["updated_at"] = utc_now_iso()
        write_project(project_file_for(project_dir), data)
    return _resolve_result(project_dir, failure_id, True, already_resolved, [])


def failure_status(failure: JsonObject) -> str:
    value = failure.get("status")
    return value if value in (FAILURE_STATUS_ACTIVE, FAILURE_STATUS_RESOLVED) else FAILURE_STATUS_ACTIVE


def format_failures_summary(summary: FailuresSummary) -> str:
    lines = [
        f"Failures: {summary['active_count']} active ({summary['total_count']} total)",
    ]
    for failure in summary["failures"]:
        failure_id = _string_field(failure, "id", "unknown")
        stage = _string_field(failure, "stage", "unknown")
        status = failure_status(failure)
        attempts = failure.get("attempt_count")
        attempt_count = attempts if isinstance(attempts, int) else 1
        lines.append(f"- {failure_id} {status} {stage} (attempts: {attempt_count})")
        errors = failure.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, str):
                    lines.append(f"  error: {error}")
    return "\n".join(lines)


def format_resolve_failure_result(result: ResolveFailureResult) -> str:
    if not result["success"]:
        return "\n".join([f"Failure: {result['failure_id']}", "Status: not-found", *result["errors"]])
    status = "already resolved" if result["already_resolved"] else "resolved"
    return f"Failure: {result['failure_id']}\nStatus: {status}"


def _derive_legacy_scope(failure: JsonObject, failure_id: str) -> str:
    stage = failure.get("stage")
    if stage in ("preview", "add_visual"):
        template_ref = failure.get("template_ref")
        if isinstance(template_ref, str) and template_ref:
            return template_failure_scope(template_ref)
    if stage in ("preview_visual", "update_visual"):
        visual_id = failure.get("visual_id")
        if isinstance(visual_id, str) and visual_id:
            return visual_failure_scope(visual_id)
    if stage == "chunk_preview":
        chunk_id = failure.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id:
            return chunk_failure_scope(chunk_id)
    if stage in ("final_compose", "final_verify"):
        return "final:final.mp4"
    if stage == "media_probe":
        media_path = failure.get("media_path")
        if isinstance(media_path, str) and media_path:
            return media_failure_scope(media_path)
    return f"legacy:{failure_id}"


def _find_active_failure(failures: list[JsonObject], stage: str, scope: str) -> JsonObject | None:
    for failure in failures:
        if (
            failure.get("stage") == stage
            and failure.get("scope") == scope
            and failure_status(failure) == FAILURE_STATUS_ACTIVE
        ):
            return failure
    return None


def _find_failure_by_id(failures: list[JsonObject], failure_id: str) -> JsonObject | None:
    for failure in failures:
        if failure.get("id") == failure_id:
            return failure
    return None


def _next_failure_id(failures: list[JsonObject]) -> str:
    highest = 0
    for failure in failures:
        failure_id = _valid_failure_id(failure.get("id"))
        if failure_id is None:
            continue
        match = FAILURE_ID_PATTERN.fullmatch(failure_id)
        if match is not None:
            highest = max(highest, int(match.group(1)))
    return _format_failure_id(highest + 1)


def _valid_failure_id(value: JsonValue | None) -> str | None:
    if isinstance(value, str) and FAILURE_ID_PATTERN.fullmatch(value):
        return value
    return None


def _format_failure_id(number: int) -> str:
    return f"failure_{number:04d}"


def _timestamp_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _string_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _reserved_fields() -> set[str]:
    return {
        "id",
        "stage",
        "scope",
        "status",
        "errors",
        "recommended_next_action",
        "attempt_count",
        "created_at",
        "updated_at",
        "resolved_at",
    }


def _resolve_result(
    project_dir: Path,
    failure_id: str,
    success: bool,
    already_resolved: bool,
    errors: list[str],
) -> ResolveFailureResult:
    return {
        "project_dir": str(project_dir),
        "failure_id": failure_id,
        "success": success,
        "already_resolved": already_resolved,
        "errors": errors,
    }
