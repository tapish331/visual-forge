"""Canonical project timeline, chunk coverage, and chunk-plan provenance."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TypedDict, cast

from .artifacts import (
    FRESHNESS_CURRENT,
    FRESHNESS_NOT_CREATED,
    FRESHNESS_STALE,
    FRESHNESS_UNVERIFIED,
    REASON_FINGERPRINT_MISMATCH,
    REASON_FINGERPRINT_MISSING,
    REASON_METADATA_MISSING,
    REASON_UPSTREAM_STALE,
    FreshnessResult,
    PipelineFreshness,
    fingerprint_from_json,
    freshness,
    is_current,
    sha256_json_fingerprint,
    stat_fingerprint,
)
from .project import JsonObject, JsonValue, ProjectState, utc_now_iso


TIMELINE_POLICY = "full_raw_v1"
CHUNKING_METHOD = "aligned_blocks_contiguous_v1"
COVERAGE_TOLERANCE_SECONDS = 0.000001


class TimelineChunkFreshness(TypedDict):
    timeline: FreshnessResult
    chunking: FreshnessResult


def build_full_raw_timeline(project_dir: Path, data: ProjectState) -> JsonObject:
    duration = raw_duration_seconds(data)
    if duration is None or duration <= 0:
        raise ValueError("Raw media metadata is missing a valid duration_seconds value")
    raw_file = project_dir / data["project"]["video"]
    if not raw_file.is_file():
        raise ValueError(f"Missing raw media file: {data['project']['video']}")
    return {
        "policy": TIMELINE_POLICY,
        "start": 0.0,
        "end": duration,
        "duration_seconds": duration,
        "source": data["project"]["video"],
        "source_fingerprint": stat_fingerprint(raw_file),
    }


def build_timeline_fingerprint(timeline: JsonObject) -> JsonObject:
    payload: JsonObject = {
        "policy": timeline.get("policy"),
        "start": timeline.get("start"),
        "end": timeline.get("end"),
        "duration_seconds": timeline.get("duration_seconds"),
        "source": timeline.get("source"),
        "source_fingerprint": timeline.get("source_fingerprint"),
    }
    return sha256_json_fingerprint(payload)


def build_chunk_plan_fingerprint(
    chunks: list[JsonObject],
    options: JsonObject,
) -> JsonObject:
    records: list[JsonValue] = []
    for chunk in chunks:
        records.append(
            {
                "id": chunk.get("id"),
                "start": chunk.get("start"),
                "end": chunk.get("end"),
                "alignment_block_ids": chunk.get("alignment_block_ids"),
                "warning_block_ids": chunk.get("warning_block_ids"),
            }
        )
    payload: JsonObject = {
        "method": CHUNKING_METHOD,
        "options": options,
        "chunks": records,
    }
    return sha256_json_fingerprint(payload)


def build_chunking_metadata(
    data: ProjectState,
    timeline: JsonObject,
    chunks: list[JsonObject],
    options: JsonObject,
) -> JsonObject:
    alignment = alignment_metadata(data)
    source_fingerprint = (
        fingerprint_from_json(alignment.get("artifact_fingerprint")) if alignment is not None else None
    )
    if source_fingerprint is None:
        raise ValueError("Alignment metadata is missing an artifact fingerprint")
    coverage = build_chunk_coverage(chunks, timeline)
    if coverage.get("complete") is not True:
        raise ValueError("Generated chunks do not completely cover the canonical timeline")
    return {
        "method": CHUNKING_METHOD,
        "source_alignment": _string_field(alignment, "path") or "alignment/script_alignment.json",
        "source_fingerprint": source_fingerprint,
        "timeline_fingerprint": build_timeline_fingerprint(timeline),
        "options": options,
        "chunk_plan_fingerprint": build_chunk_plan_fingerprint(chunks, options),
        "coverage": coverage,
        "created_at": utc_now_iso(),
    }


def build_chunk_coverage(chunks: list[JsonObject], timeline: JsonObject) -> JsonObject:
    timeline_start = _number_field(timeline, "start")
    timeline_end = _number_field(timeline, "end")
    if timeline_start is None or timeline_end is None or timeline_end <= timeline_start:
        return _empty_coverage()
    ordered = sorted(chunks, key=lambda item: (_number_field(item, "start") or 0.0, _string_field(item, "id") or ""))
    if not ordered:
        return _empty_coverage(start=timeline_start, end=timeline_end)
    first_start = _number_field(ordered[0], "start")
    last_end = _number_field(ordered[-1], "end")
    if first_start is None or last_end is None:
        return _empty_coverage(start=timeline_start, end=timeline_end)
    head_gap = max(0.0, first_start - timeline_start)
    tail_gap = max(0.0, timeline_end - last_end)
    internal_gap = 0.0
    overlap = max(0.0, timeline_start - first_start) + max(0.0, last_end - timeline_end)
    covered = 0.0
    valid = True
    previous_end: float | None = None
    for chunk in ordered:
        start = _number_field(chunk, "start")
        end = _number_field(chunk, "end")
        if start is None or end is None or end <= start:
            valid = False
            continue
        covered += end - start
        if previous_end is not None:
            internal_gap += max(0.0, start - previous_end)
            overlap += max(0.0, previous_end - start)
        previous_end = end
    complete = (
        valid
        and head_gap <= COVERAGE_TOLERANCE_SECONDS
        and tail_gap <= COVERAGE_TOLERANCE_SECONDS
        and internal_gap <= COVERAGE_TOLERANCE_SECONDS
        and overlap <= COVERAGE_TOLERANCE_SECONDS
        and abs(first_start - timeline_start) <= COVERAGE_TOLERANCE_SECONDS
        and abs(last_end - timeline_end) <= COVERAGE_TOLERANCE_SECONDS
    )
    return {
        "start": timeline_start,
        "end": timeline_end,
        "duration_seconds": round(timeline_end - timeline_start, 6),
        "covered_duration_seconds": round(covered, 6),
        "head_gap_seconds": round(head_gap, 6),
        "tail_gap_seconds": round(tail_gap, 6),
        "internal_gap_seconds": round(internal_gap, 6),
        "overlap_seconds": round(overlap, 6),
        "complete": complete,
    }


def build_timeline_chunk_freshness(
    project_dir: Path,
    data: ProjectState,
    pipeline: PipelineFreshness,
) -> TimelineChunkFreshness:
    timeline_result = build_timeline_freshness(project_dir, data, pipeline)
    chunking_result = build_chunking_freshness(data, pipeline, timeline_result)
    return {"timeline": timeline_result, "chunking": chunking_result}


def build_timeline_freshness(
    project_dir: Path,
    data: ProjectState,
    pipeline: PipelineFreshness,
) -> FreshnessResult:
    timeline = data.get("timeline")
    if timeline is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)
    if not is_current(pipeline["raw"]):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    source_expected = fingerprint_from_json(timeline.get("source_fingerprint"))
    if source_expected is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    start = _number_field(timeline, "start")
    end = _number_field(timeline, "end")
    duration = _number_field(timeline, "duration_seconds")
    raw_duration = raw_duration_seconds(data)
    if (
        timeline.get("policy") != TIMELINE_POLICY
        or start is None
        or end is None
        or duration is None
        or raw_duration is None
        or start != 0.0
        or end <= start
        or abs(duration - (end - start)) > COVERAGE_TOLERANCE_SECONDS
        or abs(end - raw_duration) > COVERAGE_TOLERANCE_SECONDS
    ):
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    raw_file = project_dir / data["project"]["video"]
    try:
        if source_expected != stat_fingerprint(raw_file):
            return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    except OSError:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    return freshness(FRESHNESS_CURRENT, None)


def build_chunking_freshness(
    data: ProjectState,
    pipeline: PipelineFreshness,
    timeline_result: FreshnessResult,
) -> FreshnessResult:
    metadata = data.get("chunking")
    if metadata is None:
        if data["chunks"]:
            return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)
    if not is_current(pipeline["alignment"]) or not is_current(timeline_result):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    source_expected = fingerprint_from_json(metadata.get("source_fingerprint"))
    timeline_expected = fingerprint_from_json(metadata.get("timeline_fingerprint"))
    plan_expected = fingerprint_from_json(metadata.get("chunk_plan_fingerprint"))
    timeline = data.get("timeline")
    options = metadata.get("options")
    coverage = metadata.get("coverage")
    alignment = alignment_metadata(data)
    alignment_actual = (
        fingerprint_from_json(alignment.get("artifact_fingerprint")) if alignment is not None else None
    )
    if (
        source_expected is None
        or timeline_expected is None
        or plan_expected is None
        or timeline is None
        or not isinstance(options, dict)
        or not isinstance(coverage, dict)
        or alignment_actual is None
    ):
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    options_data = cast(JsonObject, options)
    if metadata.get("method") != CHUNKING_METHOD:
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    if source_expected != alignment_actual or timeline_expected != build_timeline_fingerprint(timeline):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    if plan_expected != build_chunk_plan_fingerprint(data["chunks"], options_data):
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    actual_coverage = build_chunk_coverage(data["chunks"], timeline)
    if cast(JsonObject, coverage) != actual_coverage or actual_coverage.get("complete") is not True:
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    return freshness(FRESHNESS_CURRENT, None)


def current_chunk_plan_fingerprint(data: ProjectState) -> JsonObject | None:
    metadata = data.get("chunking")
    return fingerprint_from_json(metadata.get("chunk_plan_fingerprint")) if metadata is not None else None


def chunk_visual_mode(chunk: JsonObject, visuals: list[JsonObject]) -> str:
    value = chunk.get("visual_mode")
    if value in {"undecided", "visuals", "camera_only"}:
        return cast(str, value)
    chunk_id = _string_field(chunk, "id")
    if chunk_id is not None and any(visual.get("chunk_id") == chunk_id for visual in visuals):
        return "visuals"
    return "undecided"


def raw_duration_seconds(data: ProjectState) -> float | None:
    media = data.get("media")
    if not isinstance(media, dict):
        return None
    raw = media.get("raw")
    if not isinstance(raw, dict):
        return None
    return _number_field(cast(JsonObject, raw), "duration_seconds")


def alignment_metadata(data: ProjectState) -> JsonObject | None:
    alignment = data.get("alignment")
    if not isinstance(alignment, dict):
        return None
    script = alignment.get("script")
    return cast(JsonObject, script) if isinstance(script, dict) else None


def _empty_coverage(*, start: float = 0.0, end: float = 0.0) -> JsonObject:
    return {
        "start": start,
        "end": end,
        "duration_seconds": max(0.0, end - start),
        "covered_duration_seconds": 0.0,
        "head_gap_seconds": max(0.0, end - start),
        "tail_gap_seconds": 0.0,
        "internal_gap_seconds": 0.0,
        "overlap_seconds": 0.0,
        "complete": False,
    }


def _string_field(data: JsonObject | None, key: str) -> str | None:
    if data is None:
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
