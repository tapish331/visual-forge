"""Preview and chunk-render dependency provenance."""

from __future__ import annotations

from collections import Counter
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
    build_pipeline_freshness,
    fingerprint_from_json,
    freshness,
    is_current,
    sha256_fingerprint,
    sha256_json_fingerprint,
    stat_fingerprint,
)
from .assets import DEFAULT_ASSETS_DIR
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState
from .render_template import resolve_template_file, validate_template_params
from .templates import validate_template_file
from .timeline import (
    build_timeline_chunk_freshness,
    chunk_visual_mode,
    current_chunk_plan_fingerprint,
)


CHUNK_VISUAL_PLAN_METHOD = "chunk_visual_plan_sha256_v1"


class RenderFreshnessCounts(TypedDict):
    current: int
    stale: int
    unverified: int
    missing: int
    not_created: int


def build_preview_provenance(
    project_dir: Path,
    data: ProjectState,
    template_ref: str,
    relative_output: Path,
    params: dict[str, object] | None = None,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> JsonObject:
    template_file = resolve_template_file(template_ref)
    if template_file is None:
        raise ValueError(f"Template not found after successful render: {template_ref}")
    info = validate_template_file(template_file)
    version = info["template_version"]
    if not info["valid"] or not info["ready"] or version is None:
        detail = "; ".join(info["errors"]) or "template version is missing"
        raise ValueError(f"Template provenance is invalid: {detail}")
    validation = validate_template_params(template_ref, params or {}, assets_dir=assets_dir)
    if not validation["valid"]:
        raise ValueError(f"Template asset provenance is invalid: {'; '.join(validation['errors'])}")
    output_file = artifact_path(project_dir, data, relative_output)
    return {
        "template_version": version,
        "template_fingerprint": sha256_fingerprint(template_file),
        "required_asset_ids": cast(JsonValue, validation["required_assets"]),
        "asset_fingerprints": cast(JsonValue, validation["asset_fingerprints"]),
        "artifact_fingerprint": sha256_fingerprint(output_file),
    }


def build_preview_freshness(
    project_dir: Path,
    data: ProjectState,
    preview: JsonObject | None,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> FreshnessResult:
    if preview is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)
    output = _string_field(preview, "output")
    template_ref = _string_field(preview, "template_ref")
    template_version = _string_field(preview, "template_version")
    if output is None or template_ref is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    output_file = artifact_path(project_dir, data, output)
    if not output_file.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    artifact_expected = fingerprint_from_json(preview.get("artifact_fingerprint"))
    template_expected = fingerprint_from_json(preview.get("template_fingerprint"))
    required_assets_value = preview.get("required_asset_ids")
    asset_fingerprints_value = preview.get("asset_fingerprints")
    params_value = preview.get("params")
    if (
        artifact_expected is None
        or template_expected is None
        or template_version is None
        or not isinstance(params_value, dict)
    ):
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    template_file = resolve_template_file(template_ref)
    if template_file is None or not template_file.is_file():
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    try:
        if artifact_expected != sha256_fingerprint(output_file):
            return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
        if template_expected != sha256_fingerprint(template_file):
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    info = validate_template_file(template_file)
    if not info["valid"] or not info["ready"] or info["template_version"] != template_version:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    params = cast(dict[str, object], params_value)
    validation = validate_template_params(template_ref, params, assets_dir=assets_dir)
    if not validation["valid"]:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    if not isinstance(required_assets_value, list) or not isinstance(asset_fingerprints_value, dict):
        if validation["required_assets"]:
            return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
        return freshness(FRESHNESS_CURRENT, None)
    recorded_asset_ids = sorted(item for item in required_assets_value if isinstance(item, str))
    if recorded_asset_ids != validation["required_assets"]:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    recorded_assets = cast(dict[object, object], asset_fingerprints_value)
    if set(recorded_assets) != set(validation["asset_fingerprints"]):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    for asset_id, current_fingerprint in validation["asset_fingerprints"].items():
        expected_asset = fingerprint_from_json(recorded_assets.get(asset_id))
        if expected_asset is None or expected_asset != current_fingerprint:
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    return freshness(FRESHNESS_CURRENT, None)


def build_visual_plan_fingerprint(data: ProjectState, chunk_id: str) -> JsonObject | None:
    chunk = _find_chunk(data, chunk_id)
    if chunk is None:
        return None
    chunk_start = _number_field(chunk, "start")
    chunk_end = _number_field(chunk, "end")
    if chunk_start is None or chunk_end is None:
        return None
    visuals: list[JsonObject] = []
    for visual in data.get("visuals", []):
        if visual.get("chunk_id") != chunk_id:
            continue
        record: JsonObject = {
            "id": visual.get("id"),
            "template_ref": visual.get("template_ref"),
            "template_id": visual.get("template_id"),
            "params": visual.get("params"),
            "start": visual.get("start"),
            "end": visual.get("end"),
            "preview_id": visual.get("preview_id"),
        }
        # Keep the established fingerprint payload unchanged for manual and
        # heuristic visuals. Codex intent provenance is part of the plan only
        # for records created by the intent planner.
        if visual.get("planner") == "codex_v1":
            record["planner"] = "codex_v1"
            record["intent_id"] = visual.get("intent_id")
        visuals.append(record)
    visuals.sort(key=lambda item: str(item.get("id", "")))
    payload: JsonObject = {
        "method": CHUNK_VISUAL_PLAN_METHOD,
        "chunk": {
            "id": chunk_id,
            "start": chunk_start,
            "end": chunk_end,
            "visual_mode": chunk_visual_mode(chunk, data.get("visuals", [])),
        },
        "visuals": cast(list[JsonValue], visuals),
    }
    return sha256_json_fingerprint(payload)


def build_chunk_render_freshness(
    project_dir: Path,
    data: ProjectState,
    chunk_id: str,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> FreshnessResult:
    chunk = _find_chunk(data, chunk_id)
    metadata = _chunk_render_metadata(data, chunk_id)
    if metadata is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)
    output = _string_field(metadata, "path")
    if output is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    output_file = artifact_path(project_dir, data, output)
    if not output_file.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    artifact_expected = fingerprint_from_json(metadata.get("artifact_fingerprint"))
    plan_expected = fingerprint_from_json(metadata.get("visual_plan_fingerprint"))
    source_expected = fingerprint_from_json(metadata.get("source_fingerprint"))
    chunk_plan_expected = fingerprint_from_json(metadata.get("chunk_plan_fingerprint"))
    preview_fingerprints = metadata.get("preview_fingerprints")
    if (
        artifact_expected is None
        or plan_expected is None
        or source_expected is None
        or chunk_plan_expected is None
        or not isinstance(preview_fingerprints, dict)
    ):
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    pipeline = build_pipeline_freshness(project_dir, data)
    timeline_freshness = build_timeline_chunk_freshness(project_dir, data, pipeline)
    if not is_current(timeline_freshness["chunking"]):
        if timeline_freshness["chunking"]["state"] == FRESHNESS_UNVERIFIED:
            return timeline_freshness["chunking"]
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    current_chunk_plan = current_chunk_plan_fingerprint(data)
    if current_chunk_plan is None or current_chunk_plan != chunk_plan_expected:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    raw_file = project_dir / data["project"]["video"]
    try:
        if artifact_expected != stat_fingerprint(output_file):
            return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
        if source_expected != stat_fingerprint(raw_file):
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    if chunk is None or _string_field(chunk, "status") != "rendered":
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    plan_actual = build_visual_plan_fingerprint(data, chunk_id)
    if plan_actual is None or plan_actual != plan_expected:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    chunk_visuals = [visual for visual in data.get("visuals", []) if visual.get("chunk_id") == chunk_id]
    visual_mode = chunk_visual_mode(chunk, data.get("visuals", []))
    if visual_mode == "camera_only":
        if chunk_visuals or cast(dict[object, object], preview_fingerprints):
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
        return freshness(FRESHNESS_CURRENT, None)
    if visual_mode != "visuals" or not chunk_visuals:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    recorded = cast(dict[object, object], preview_fingerprints)
    for visual in chunk_visuals:
        if _string_field(visual, "status") != "previewed":
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
        preview_id = _string_field(visual, "preview_id")
        if preview_id is None:
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
        preview = _find_preview(data, preview_id)
        preview_freshness = build_preview_freshness(project_dir, data, preview, assets_dir)
        if not is_current(preview_freshness):
            if preview_freshness["state"] == FRESHNESS_UNVERIFIED:
                return preview_freshness
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
        expected_preview = fingerprint_from_json(recorded.get(preview_id))
        actual_preview = fingerprint_from_json(preview.get("artifact_fingerprint")) if preview is not None else None
        if expected_preview is None:
            return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
        if actual_preview is None or expected_preview != actual_preview:
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    return freshness(FRESHNESS_CURRENT, None)


def build_chunk_render_freshness_map(
    project_dir: Path,
    data: ProjectState,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> dict[str, FreshnessResult]:
    results: dict[str, FreshnessResult] = {}
    for chunk in data["chunks"]:
        chunk_id = _string_field(chunk, "id")
        if chunk_id is not None:
            results[chunk_id] = build_chunk_render_freshness(project_dir, data, chunk_id, assets_dir)
    return results


def summarize_chunk_render_freshness(
    results: dict[str, FreshnessResult],
) -> RenderFreshnessCounts:
    counts: Counter[str] = Counter(result["state"] for result in results.values())
    return {
        "current": counts[FRESHNESS_CURRENT],
        "stale": counts[FRESHNESS_STALE],
        "unverified": counts[FRESHNESS_UNVERIFIED],
        "missing": counts[FRESHNESS_MISSING],
        "not_created": counts[FRESHNESS_NOT_CREATED],
    }


def preview_artifact_fingerprint(preview: JsonObject) -> JsonObject | None:
    return fingerprint_from_json(preview.get("artifact_fingerprint"))


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


def _chunk_render_metadata(data: ProjectState, chunk_id: str) -> JsonObject | None:
    renders = data.get("renders")
    if not isinstance(renders, dict):
        return None
    chunks = renders.get("chunks")
    if not isinstance(chunks, dict):
        return None
    value = chunks.get(chunk_id)
    return cast(JsonObject, value) if isinstance(value, dict) else None


def _string_field(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None
