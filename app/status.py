"""Status summaries for Visual Forge projects."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from .artifacts import (
    FRESHNESS_CURRENT,
    FRESHNESS_UNVERIFIED,
    REASON_UPSTREAM_STALE,
    PipelineFreshness,
    FreshnessResult,
    build_pipeline_freshness,
    is_current,
)
from .compose import build_final_freshness
from .failures import FAILURE_STATUS_RESOLVED, active_failures, failure_status
from .layout import artifact_path, layout_from_project
from .project import JsonObject, ProjectState, load_project, project_file_for
from .render_freshness import (
    RenderFreshnessCounts,
    build_chunk_render_freshness_map,
    summarize_chunk_render_freshness,
)
from .timeline import build_timeline_chunk_freshness
from .verify import VERIFY_REPORT_PATH, build_verification_freshness


RENDERED_STATUS = "rendered"


class StatusProject(TypedDict):
    name: str
    script: str
    video: str
    final: str
    created_at: str
    updated_at: str


class FileStatus(TypedDict):
    path: str
    exists: bool
    resolved_path: NotRequired[str]
    current: NotRequired[bool]


class ChunkSummary(TypedDict):
    total: int
    by_status: dict[str, int]
    render_freshness: RenderFreshnessCounts


class VisualSummary(TypedDict):
    total: int
    by_status: dict[str, int]
    by_chunk: dict[str, int]


class FailureSummary(TypedDict):
    total: int
    active: int
    resolved: int
    items: list[JsonObject]


class MediaSummary(TypedDict):
    raw: JsonObject
    audio: NotRequired[JsonObject]


class TranscriptSummary(TypedDict):
    narration: JsonObject


class AlignmentSummary(TypedDict):
    script: JsonObject


class LayoutSummary(TypedDict):
    active: bool
    version: int | None
    slug: str | None
    inputs_root: str | None
    outputs_root: str | None


class VerificationSummary(TypedDict):
    final: JsonObject


class StatusSummary(TypedDict):
    schema_version: int
    project_dir: str
    project_json: str
    project: StatusProject
    state: str
    inputs: dict[str, FileStatus]
    outputs: dict[str, FileStatus]
    layout: LayoutSummary
    media: MediaSummary
    transcript: TranscriptSummary
    alignment: AlignmentSummary
    freshness: PipelineFreshness
    timeline: JsonObject
    chunking: JsonObject
    verification: VerificationSummary
    chunks: ChunkSummary
    visuals: VisualSummary
    failures: FailureSummary
    missing_inputs: list[str]
    next_action: str


def build_status(project_dir: Path) -> StatusSummary:
    data = load_project(project_dir)
    project = data["project"]

    script_path = project_dir / project["script"]
    video_path = project_dir / project["video"]
    final_path = artifact_path(project_dir, data, project["final"])
    script_exists = script_path.exists()
    video_exists = video_path.exists()
    final_exists = final_path.exists()

    chunks = data["chunks"]
    freshness_summary = build_pipeline_freshness(project_dir, data)
    timeline_chunk_freshness = build_timeline_chunk_freshness(project_dir, data, freshness_summary)
    chunk_render_freshness = build_chunk_render_freshness_map(project_dir, data)
    chunk_render_counts = summarize_chunk_render_freshness(chunk_render_freshness)
    all_chunk_renders_current = bool(chunks) and len(chunk_render_freshness) == len(chunks) and all(
        is_current(result) for result in chunk_render_freshness.values()
    )
    final_freshness = build_final_freshness(project_dir, data)
    verification_freshness = build_verification_freshness(project_dir, data)
    media = data.get("media", {})
    raw_media = _raw_media_summary(media, project["video"], freshness_summary["raw"], project_dir)
    audio_media = _audio_media_summary(media, freshness_summary["audio"], project_dir, data)
    transcript_data = data.get("transcript", {})
    transcript_summary = _transcript_summary(transcript_data, freshness_summary["transcript"], project_dir, data)
    alignment_data = data.get("alignment", {})
    alignment_summary = _alignment_summary(alignment_data, freshness_summary["alignment"], project_dir, data)
    visuals = data.get("visuals", [])
    failures = data["failures"]
    active_failure_items = active_failures(data)
    resolved_failure_count = sum(
        1 for failure in failures if failure_status(failure) == FAILURE_STATUS_RESOLVED
    )
    chunk_statuses: Counter[str] = Counter(_chunk_status(chunk) for chunk in chunks)
    visual_statuses: Counter[str] = Counter(_item_status(visual) for visual in visuals)
    visual_chunks = _visual_chunk_counts(visuals)

    missing_inputs: list[str] = []
    if not script_exists:
        missing_inputs.append(project["script"])
    if not video_exists:
        missing_inputs.append(project["video"])

    state = determine_state(
        missing_inputs=missing_inputs,
        chunks=chunks,
        failures=active_failure_items,
        raw_media_probed=is_current(freshness_summary["raw"]),
        audio_extracted=is_current(freshness_summary["audio"]),
        transcript_ready=is_current(freshness_summary["transcript"]),
        alignment_ready=is_current(freshness_summary["alignment"]),
        timeline_ready=is_current(timeline_chunk_freshness["timeline"]),
        chunking_ready=is_current(timeline_chunk_freshness["chunking"]),
        chunk_renders_current=all_chunk_renders_current,
        final_current=is_current(final_freshness),
        verification_current=is_current(verification_freshness),
    )
    return {
        "schema_version": data["schema_version"],
        "project_dir": str(project_dir),
        "project_json": str(project_file_for(project_dir)),
        "project": {
            "name": project["name"],
            "script": project["script"],
            "video": project["video"],
            "final": project["final"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        },
        "state": state,
        "inputs": {
            "script": {
                "path": project["script"],
                "resolved_path": _display_path(script_path),
                "exists": script_exists,
            },
            "video": {
                "path": project["video"],
                "resolved_path": _display_path(video_path),
                "exists": video_exists,
            },
        },
        "outputs": {
            "final": {
                "path": project["final"],
                "resolved_path": _display_path(final_path),
                "exists": final_exists,
                "current": is_current(final_freshness),
            },
        },
        "layout": _layout_summary(data),
        "media": {
            "raw": raw_media,
            "audio": audio_media,
        },
        "transcript": transcript_summary,
        "alignment": alignment_summary,
        "freshness": freshness_summary,
        "timeline": {
            **data.get("timeline", {}),
            "freshness": _freshness_json(timeline_chunk_freshness["timeline"]),
        },
        "chunking": {
            **data.get("chunking", {}),
            "freshness": _freshness_json(timeline_chunk_freshness["chunking"]),
        },
        "verification": _verification_summary(data, verification_freshness, project_dir),
        "chunks": {
            "total": len(chunks),
            "by_status": dict(sorted(chunk_statuses.items())),
            "render_freshness": chunk_render_counts,
        },
        "visuals": {
            "total": len(visuals),
            "by_status": dict(sorted(visual_statuses.items())),
            "by_chunk": dict(sorted(visual_chunks.items())),
        },
        "failures": {
            "total": len(failures),
            "active": len(active_failure_items),
            "resolved": resolved_failure_count,
            "items": active_failure_items,
        },
        "missing_inputs": missing_inputs,
        "next_action": next_action_for(
            state,
            missing_inputs,
            script_path=project["script"],
            video_path=project["video"],
            chunks=chunks,
            visual_chunk_ids=set(visual_chunks),
            chunk_render_freshness=chunk_render_freshness,
        ),
    }


def determine_state(
    *,
    missing_inputs: list[str],
    chunks: list[JsonObject],
    failures: list[JsonObject],
    raw_media_probed: bool,
    audio_extracted: bool,
    transcript_ready: bool,
    alignment_ready: bool,
    timeline_ready: bool = False,
    chunking_ready: bool = False,
    chunk_renders_current: bool = False,
    final_current: bool = False,
    verification_current: bool = False,
) -> str:
    if failures or any(_chunk_status(chunk) == "failed" for chunk in chunks):
        return "failed"
    if missing_inputs:
        return "missing_inputs"
    if not raw_media_probed:
        return "ready_for_probe"
    if not audio_extracted:
        return "ready_for_audio"
    if not transcript_ready:
        return "ready_for_transcription"
    if not alignment_ready:
        return "ready_for_alignment"
    if not timeline_ready or not chunking_ready or not chunks:
        return "ready_for_chunks"
    if all(_chunk_status(chunk) == RENDERED_STATUS for chunk in chunks) and chunk_renders_current:
        if not final_current:
            return "ready_for_final"
        return "complete" if verification_current else "ready_for_verification"
    return "in_progress"


def next_action_for(
    state: str,
    missing_inputs: list[str],
    *,
    script_path: str,
    video_path: str,
    chunks: list[JsonObject],
    visual_chunk_ids: set[str],
    chunk_render_freshness: dict[str, FreshnessResult],
) -> str:
    if state == "missing_inputs":
        return "Add missing input files: " + ", ".join(missing_inputs)
    if state == "ready_for_probe":
        return f"Run media probing for {video_path}."
    if state == "ready_for_audio":
        return f"Extract audio from {video_path}."
    if state == "ready_for_transcription":
        return "Transcribe narration audio."
    if state == "ready_for_alignment":
        return f"Align {script_path} to the narration transcript."
    if state == "ready_for_chunks":
        return "Create resumable video chunks."
    if state == "failed":
        return "Inspect failures and apply a targeted correction."
    if state == "ready_for_final":
        return "Run final composition."
    if state == "ready_for_verification":
        return "Verify final video."
    if state == "complete":
        return "Review final video."
    if state == "in_progress" and any(_chunk_status(chunk) == "previewed" for chunk in chunks):
        return "Render each previewed chunk."
    if state == "in_progress" and any(_chunk_status(chunk) == RENDERED_STATUS for chunk in chunks):
        rendered_ids = {
            chunk_id
            for chunk in chunks
            if _chunk_status(chunk) == RENDERED_STATUS
            and (chunk_id := _optional_string(chunk, "id")) is not None
        }
        if not rendered_ids.issubset(visual_chunk_ids):
            return "Plan visuals for each chunk."
        requires_preview = any(
            result["state"] == FRESHNESS_UNVERIFIED or result["reason"] == REASON_UPSTREAM_STALE
            for chunk_id, result in chunk_render_freshness.items()
            if chunk_id in rendered_ids
        )
        if requires_preview:
            return "Preview visuals for each changed chunk."
        return "Render each non-current chunk."
    if state == "in_progress" and any(_chunk_status(chunk) == "new" for chunk in chunks):
        new_chunk_ids = {
            chunk_id
            for chunk in chunks
            if _chunk_status(chunk) == "new" and (chunk_id := _optional_string(chunk, "id")) is not None
        }
        if new_chunk_ids and new_chunk_ids.issubset(visual_chunk_ids):
            return "Preview visuals for each changed chunk."
        return "Plan visuals for each chunk."
    return "Continue the next incomplete checkpoint."


def format_status(summary: StatusSummary) -> str:
    input_lines: list[str] = []
    for label, item in summary["inputs"].items():
        marker = "found" if item["exists"] else "missing"
        input_lines.append(f"  {label}: {item['path']} ({marker})")

    return "\n".join(
        [
            f"Project: {summary['project']['name']}",
            f"Path: {summary['project_dir']}",
            f"Layout: {_format_layout(summary['layout'])}",
            f"State: {summary['state']}",
            "Inputs:",
            *input_lines,
            f"Media: {_raw_media_path(summary['media'])} ({_format_freshness(summary['freshness']['raw'])})",
            f"Audio: {_audio_path(summary['media'])} ({_format_freshness(summary['freshness']['audio'])})",
            f"Transcript: {_transcript_path(summary['transcript'])} ({_format_freshness(summary['freshness']['transcript'])})",
            f"Alignment: {_alignment_path(summary['alignment'])} ({_format_freshness(summary['freshness']['alignment'])})",
            f"Alignment warnings: {_alignment_warning_count(summary['alignment'])}",
            f"Timeline: {_format_timeline(summary['timeline'])}",
            f"Chunking: {_format_chunking(summary['chunking'])}",
            f"Chunks: {summary['chunks']['total']}",
            f"Chunk renders: {summary['chunks']['render_freshness']['current']} current",
            f"Visuals: {summary['visuals']['total']}",
            f"Final: {_file_display_path(summary['outputs']['final'])} ({_final_marker(summary['outputs']['final'])})",
            f"Verification: {_verification_path(summary['verification'])} ({_verification_marker(summary['verification'])})",
            f"Failures: {summary['failures']['active']} active ({summary['failures']['total']} total)",
            f"Next action: {summary['next_action']}",
        ]
    )


def _chunk_status(chunk: JsonObject) -> str:
    return _item_status(chunk)


def _item_status(item: JsonObject) -> str:
    status = item.get("status")
    if isinstance(status, str) and status:
        return status
    return "unknown"


def _visual_chunk_id(visual: JsonObject) -> str | None:
    value = visual.get("chunk_id")
    if isinstance(value, str) and value:
        return value
    return None


def _visual_chunk_counts(visuals: list[JsonObject]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for visual in visuals:
        chunk_id = _visual_chunk_id(visual)
        if chunk_id is not None:
            counts[chunk_id] += 1
    return dict(sorted(counts.items()))


def _layout_summary(data: ProjectState) -> LayoutSummary:
    layout = layout_from_project(data)
    if layout is None:
        return {
            "active": False,
            "version": None,
            "slug": None,
            "inputs_root": None,
            "outputs_root": None,
        }
    return {
        "active": True,
        "version": _int_field(layout, "version"),
        "slug": _optional_string(layout, "slug"),
        "inputs_root": _optional_string(layout, "inputs_root"),
        "outputs_root": _optional_string(layout, "outputs_root"),
    }


def _verification_summary(
    data: ProjectState,
    freshness_result: FreshnessResult,
    project_dir: Path,
) -> VerificationSummary:
    verification = data.get("verification")
    final = verification.get("final") if isinstance(verification, dict) else None
    if not isinstance(final, dict):
        return {
            "final": {
                "path": VERIFY_REPORT_PATH,
                "resolved_path": _display_path(artifact_path(project_dir, data, VERIFY_REPORT_PATH)),
                "status": "not_verified",
                "current": False,
                "error_count": 0,
                "warning_count": 0,
            }
        }
    final_data = cast(JsonObject, final)
    report_path = _string_field(final_data, "path", VERIFY_REPORT_PATH)
    return {
        "final": {
            "path": report_path,
            "resolved_path": _display_path(artifact_path(project_dir, data, report_path)),
            "status": _string_field(final_data, "status", "unknown"),
            "current": is_current(freshness_result),
            "error_count": _number_field(final_data, "error_count") or 0,
            "warning_count": _number_field(final_data, "warning_count") or 0,
            "verified_at": _string_field(final_data, "verified_at", "unknown"),
        }
    }


def _format_layout(layout: LayoutSummary) -> str:
    if not layout["active"]:
        return "legacy project-local artifacts"
    slug = layout["slug"] or "unknown"
    outputs_root = layout["outputs_root"] or "unknown outputs"
    return f"{slug} -> {outputs_root}"


def _file_display_path(item: Mapping[str, object]) -> str:
    value = item.get("resolved_path") or item.get("path")
    if isinstance(value, str) and value:
        return value
    return "unknown"


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _probed_raw_media(media: JsonObject) -> bool:
    raw = media.get("raw")
    return isinstance(raw, dict) and isinstance(raw.get("duration_seconds"), int | float)


def _raw_media_summary(
    media: JsonObject,
    default_path: str,
    freshness_result: FreshnessResult,
    project_dir: Path,
) -> JsonObject:
    raw = media.get("raw")
    if not isinstance(raw, dict):
        resolved_path = _display_path(project_dir / default_path)
        return {
            "path": default_path,
            "resolved_path": resolved_path,
            "probed": False,
        }

    raw_data = raw
    raw_path = _string_field(raw_data, "path", default_path)
    video = raw_data.get("video")
    audio = raw_data.get("audio")
    summary: JsonObject = {
        "path": raw_path,
        "resolved_path": _display_path(project_dir / raw_path),
        "probed": is_current(freshness_result),
        "duration_seconds": _number_field(raw_data, "duration_seconds"),
    }
    if isinstance(video, dict):
        video_data = cast(JsonObject, video)
        summary["video"] = {
            "width": _number_field(video_data, "width"),
            "height": _number_field(video_data, "height"),
            "frame_rate": _number_field(video_data, "frame_rate"),
            "codec": _string_field(video_data, "codec", "unknown"),
        }
    if isinstance(audio, dict):
        audio_data = cast(JsonObject, audio)
        summary["audio"] = {
            "codec": _string_field(audio_data, "codec", "unknown"),
            "sample_rate": _number_field(audio_data, "sample_rate"),
            "channels": _number_field(audio_data, "channels"),
        }
    return summary


def _audio_media_summary(
    media: JsonObject,
    freshness_result: FreshnessResult,
    project_dir: Path,
    data: ProjectState,
) -> JsonObject:
    audio = media.get("audio")
    if not isinstance(audio, dict):
        return {
            "narration": {
                "path": "audio/narration.wav",
                "resolved_path": _display_path(artifact_path(project_dir, data, "audio/narration.wav")),
                "extracted": False,
            }
        }

    narration = audio.get("narration")
    if not isinstance(narration, dict):
        return {
            "narration": {
                "path": "audio/narration.wav",
                "resolved_path": _display_path(artifact_path(project_dir, data, "audio/narration.wav")),
                "extracted": False,
            }
        }

    narration_data = cast(JsonObject, narration)
    narration_path = _string_field(narration_data, "path", "audio/narration.wav")
    return {
        "narration": {
            "path": narration_path,
            "resolved_path": _display_path(artifact_path(project_dir, data, narration_path)),
            "extracted": is_current(freshness_result),
            "sample_rate": _number_field(narration_data, "sample_rate"),
            "channels": _number_field(narration_data, "channels"),
            "duration_seconds": _number_field(narration_data, "duration_seconds"),
            "size_bytes": _number_field(narration_data, "size_bytes"),
        }
    }


def _extracted_audio(media: JsonObject) -> bool:
    audio = media.get("audio")
    if not isinstance(audio, dict):
        return False
    narration = audio.get("narration")
    return isinstance(narration, dict) and isinstance(narration.get("path"), str)


def _transcribed_narration(transcript: JsonObject) -> bool:
    narration = transcript.get("narration")
    return isinstance(narration, dict) and narration.get("status") == "transcribed"


def _transcript_summary(
    transcript: JsonObject,
    freshness_result: FreshnessResult,
    project_dir: Path,
    data: ProjectState,
) -> TranscriptSummary:
    narration = transcript.get("narration")
    if not isinstance(narration, dict):
        return {
            "narration": {
                "path": "transcripts/narration.json",
                "resolved_path": _display_path(artifact_path(project_dir, data, "transcripts/narration.json")),
                "transcribed": False,
            }
        }

    narration_data = cast(JsonObject, narration)
    narration_path = _string_field(narration_data, "path", "transcripts/narration.json")
    return {
        "narration": {
            "path": narration_path,
            "resolved_path": _display_path(artifact_path(project_dir, data, narration_path)),
            "transcribed": is_current(freshness_result),
            "provider": _string_field(narration_data, "provider", "unknown"),
            "model": _string_field(narration_data, "model", "unknown"),
            "device": _string_field(narration_data, "device", "unknown"),
            "compute_type": _string_field(narration_data, "compute_type", "unknown"),
            "duration_seconds": _number_field(narration_data, "duration_seconds"),
            "segments": _number_field(narration_data, "segments"),
            "word_count": _number_field(narration_data, "word_count"),
            "text_length": _number_field(narration_data, "text_length"),
        }
    }


def _alignment_summary(
    alignment: JsonObject,
    freshness_result: FreshnessResult,
    project_dir: Path,
    data: ProjectState,
) -> AlignmentSummary:
    script = alignment.get("script")
    if not isinstance(script, dict):
        return {
            "script": {
                "path": "alignment/script_alignment.json",
                "resolved_path": _display_path(artifact_path(project_dir, data, "alignment/script_alignment.json")),
                "aligned": False,
                "stale": False,
                "aligned_blocks": 0,
                "needs_review_blocks": 0,
                "unmatched_blocks": 0,
            }
        }

    script_data = cast(JsonObject, script)
    stale = freshness_result["state"] != FRESHNESS_CURRENT
    status_aligned = _string_field(script_data, "status", "") == "aligned"
    block_count = _number_field(script_data, "blocks")
    needs_review_count = _number_field(script_data, "needs_review_blocks") or 0
    unmatched_count = _number_field(script_data, "unmatched_blocks") or 0
    aligned_count = _number_field(script_data, "aligned_blocks")
    if aligned_count is None and block_count is not None:
        aligned_count = max(0, block_count - needs_review_count - unmatched_count)
    alignment_path = _string_field(script_data, "path", "alignment/script_alignment.json")
    return {
        "script": {
            "path": alignment_path,
            "resolved_path": _display_path(artifact_path(project_dir, data, alignment_path)),
            "aligned": status_aligned and not stale,
            "stale": stale,
            "method": _string_field(script_data, "method", "unknown"),
            "blocks": block_count,
            "aligned_blocks": aligned_count,
            "coverage": _number_field(script_data, "coverage"),
            "needs_review_blocks": needs_review_count,
            "unmatched_blocks": unmatched_count,
        }
    }


def _media_marker(raw_media: JsonObject) -> str:
    probed = raw_media.get("probed")
    return "probed" if probed is True else "not probed"


def _raw_media_path(media: MediaSummary) -> str:
    raw = media["raw"]
    path = raw.get("resolved_path") or raw.get("path")
    if isinstance(path, str) and path:
        return path
    return "raw.mp4"


def _audio_path(media: MediaSummary) -> str:
    audio = media.get("audio")
    if isinstance(audio, dict):
        narration = audio.get("narration")
        if isinstance(narration, dict):
            path = narration.get("resolved_path") or narration.get("path")
            if isinstance(path, str) and path:
                return path
    return "audio/narration.wav"


def _audio_marker(media: MediaSummary) -> str:
    audio = media.get("audio")
    if isinstance(audio, dict):
        narration = audio.get("narration")
        if isinstance(narration, dict) and narration.get("extracted") is True:
            return "extracted"
    return "not extracted"


def _transcript_path(transcript: TranscriptSummary) -> str:
    narration = transcript["narration"]
    path = narration.get("resolved_path") or narration.get("path")
    if isinstance(path, str) and path:
        return path
    return "transcripts/narration.json"


def _transcript_marker(transcript: TranscriptSummary) -> str:
    return "transcribed" if transcript["narration"].get("transcribed") is True else "not transcribed"


def _alignment_path(alignment: AlignmentSummary) -> str:
    path = alignment["script"].get("resolved_path") or alignment["script"].get("path")
    if isinstance(path, str) and path:
        return path
    return "alignment/script_alignment.json"


def _alignment_marker(alignment: AlignmentSummary) -> str:
    if alignment["script"].get("stale") is True:
        return "stale"
    return "aligned" if alignment["script"].get("aligned") is True else "not aligned"


def _alignment_warning_count(alignment: AlignmentSummary) -> int:
    script = alignment["script"]
    needs_review = _number_field(script, "needs_review_blocks") or 0
    unmatched = _number_field(script, "unmatched_blocks") or 0
    return int(needs_review + unmatched)


def _final_marker(final: FileStatus) -> str:
    if final.get("current") is True:
        return "current"
    if final["exists"]:
        return "not current"
    return "missing"


def _verification_marker(verification: VerificationSummary) -> str:
    final = verification["final"]
    if final.get("current") is True and final.get("status") == "passed":
        warnings = _number_field(final, "warning_count") or 0
        return f"passed ({int(warnings)} warnings)"
    status = final.get("status")
    return status if isinstance(status, str) else "not_verified"


def _verification_path(verification: VerificationSummary) -> str:
    final = verification["final"]
    path = final.get("resolved_path") or final.get("path")
    if isinstance(path, str) and path:
        return path
    return VERIFY_REPORT_PATH


def _format_freshness(result: FreshnessResult) -> str:
    state = result["state"]
    reason = result["reason"]
    if state == "stale" and reason == "upstream_stale":
        return "stale: upstream changed"
    if state == "stale" and reason == "fingerprint_mismatch":
        return "stale: fingerprint mismatch"
    if state == "not_created":
        return "not created"
    return state


def _format_timeline(timeline: JsonObject) -> str:
    freshness_result = timeline.get("freshness")
    marker = "not_created"
    if isinstance(freshness_result, dict):
        marker = str(freshness_result.get("state", marker))
    start = _number_field(timeline, "start")
    end = _number_field(timeline, "end")
    if start is None or end is None:
        return marker
    return f"{start} -> {end} ({marker})"


def _format_chunking(chunking: JsonObject) -> str:
    freshness_result = chunking.get("freshness")
    marker = "not_created"
    if isinstance(freshness_result, dict):
        marker = str(freshness_result.get("state", marker))
    coverage = chunking.get("coverage")
    complete = isinstance(coverage, dict) and coverage.get("complete") is True
    return f"{marker}, coverage={'complete' if complete else 'incomplete'}"


def _freshness_json(result: FreshnessResult) -> JsonObject:
    return {"state": result["state"], "reason": result["reason"]}


def _string_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _number_field(data: JsonObject, key: str) -> int | float | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _int_field(data: JsonObject, key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_string(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None
