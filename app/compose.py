"""Compose rendered chunks into the final project video."""

from __future__ import annotations

import subprocess
import tempfile
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
    replace_artifact,
    stat_fingerprint,
    temporary_artifact_path,
)
from .audio import resolve_ffmpeg
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for, utc_now_iso, write_project
from .render_freshness import build_chunk_render_freshness
from .timeline import (
    build_timeline_chunk_freshness,
    build_timeline_fingerprint,
    current_chunk_plan_fingerprint,
)


FINAL_COMPOSE_STAGE = "final_compose"
FINAL_COMPOSE_SCOPE = "final:final.mp4"
FINAL_COMPOSE_RECOMMENDED_NEXT_ACTION = (
    "Fix rendered chunks, media freshness, or FFmpeg availability, then rerun final composition."
)
RENDERED_STATUS = "rendered"


class FinalComposeResult(TypedDict):
    project_dir: str
    output_path: str | None
    success: bool
    metadata: JsonObject | None
    errors: list[str]


class ChunkForCompose(TypedDict):
    chunk_id: str
    start: float
    end: float
    path: str
    file: Path
    duration_seconds: float
    artifact_fingerprint: JsonObject


def compose_project_final(project_dir: Path) -> FinalComposeResult:
    data = load_project(project_dir)
    output_file = artifact_path(project_dir, data, data["project"]["final"])
    chunks, errors = _chunks_for_compose(project_dir, data)
    errors.extend(_pipeline_errors(project_dir, data))

    ffmpeg: str | None = None
    if not errors:
        ffmpeg, ffmpeg_error = resolve_ffmpeg()
        if ffmpeg is None:
            errors.append(ffmpeg_error)

    metadata: JsonObject | None = None
    if not errors and ffmpeg is not None:
        compose_errors = _run_ffmpeg_concat(ffmpeg, chunks, output_file)
        errors.extend(compose_errors)
        if not errors:
            metadata = _final_metadata(data, chunks, output_file)

    result: FinalComposeResult = {
        "project_dir": str(project_dir),
        "output_path": str(output_file),
        "success": not errors and metadata is not None,
        "metadata": metadata,
        "errors": errors,
    }
    if result["success"] and metadata is not None:
        _record_final_success(project_dir, data, metadata)
    else:
        _record_final_failure(project_dir, data, str(output_file), errors)
    return result


def build_final_freshness(project_dir: Path, data: ProjectState) -> FreshnessResult:
    metadata = _final_render_metadata(data)
    if metadata is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)

    final_path = _string_field(metadata, "path") or data["project"]["final"]
    output_file = artifact_path(project_dir, data, final_path)
    if not output_file.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)

    artifact_expected = fingerprint_from_json(metadata.get("artifact_fingerprint"))
    source_fingerprints = metadata.get("source_fingerprints")
    if artifact_expected is None or not isinstance(source_fingerprints, dict):
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)

    timeline_expected = fingerprint_from_json(metadata.get("timeline_fingerprint"))
    chunk_plan_expected = fingerprint_from_json(metadata.get("chunk_plan_fingerprint"))
    timeline = data.get("timeline")
    current_chunk_plan = current_chunk_plan_fingerprint(data)
    if timeline_expected is None or chunk_plan_expected is None or timeline is None or current_chunk_plan is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    pipeline = build_pipeline_freshness(project_dir, data)
    timeline_freshness = build_timeline_chunk_freshness(project_dir, data, pipeline)
    if not is_current(timeline_freshness["chunking"]):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    if timeline_expected != build_timeline_fingerprint(timeline) or chunk_plan_expected != current_chunk_plan:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)

    try:
        if artifact_expected != stat_fingerprint(output_file):
            return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)

    chunks, errors = _chunks_for_compose(project_dir, data)
    if errors:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)

    source_data = cast(dict[str, object], source_fingerprints)
    for chunk in chunks:
        expected_source = fingerprint_from_json(source_data.get(chunk["chunk_id"]))
        if expected_source is None or expected_source != chunk["artifact_fingerprint"]:
            return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    return freshness(FRESHNESS_CURRENT, None)


def format_final_compose_result(result: FinalComposeResult) -> str:
    status = "rendered" if result["success"] else "failed"
    lines = [
        f"Final: {result['output_path'] or 'not-created'}",
        f"Status: {status}",
    ]
    metadata = result["metadata"]
    if result["success"] and metadata is not None:
        lines.append(f"Duration: {metadata.get('duration_seconds')}s")
        lines.append(f"Chunks: {len(_string_list(metadata.get('chunk_ids')))}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _chunks_for_compose(project_dir: Path, data: ProjectState) -> tuple[list[ChunkForCompose], list[str]]:
    chunks = data["chunks"]
    if not chunks:
        return [], ["No chunks exist. Run create-chunks before final composition."]

    ordered = sorted(chunks, key=lambda chunk: (_number_field(chunk, "start") or 0.0, _number_field(chunk, "end") or 0.0, _string_field(chunk, "id") or ""))
    render_chunks = _render_chunks_metadata(data)
    results: list[ChunkForCompose] = []
    errors: list[str] = []

    for chunk in ordered:
        chunk_id = _string_field(chunk, "id")
        start = _number_field(chunk, "start")
        end = _number_field(chunk, "end")
        if chunk_id is None:
            errors.append("Chunk record is missing a non-empty id.")
            continue
        if start is None or end is None or end <= start:
            errors.append(f"Chunk record is invalid: {chunk_id}")
            continue
        if _string_field(chunk, "status") != RENDERED_STATUS:
            errors.append(f"Chunk must be rendered before final composition: {chunk_id}")
            continue

        render_metadata = _object_field(render_chunks, chunk_id)
        if render_metadata is None:
            errors.append(f"Missing rendered chunk metadata: {chunk_id}")
            continue
        render_path = _string_field(render_metadata, "path")
        if render_path is None:
            errors.append(f"Rendered chunk metadata is missing path: {chunk_id}")
            continue
        artifact_expected = fingerprint_from_json(render_metadata.get("artifact_fingerprint"))
        if artifact_expected is None:
            errors.append(f"Rendered chunk metadata is missing artifact fingerprint: {chunk_id}")
            continue

        render_file = artifact_path(project_dir, data, render_path)
        if not render_file.is_file():
            errors.append(f"Rendered chunk file is missing: {render_path}")
            continue
        try:
            if artifact_expected != stat_fingerprint(render_file):
                errors.append(f"Rendered chunk file is stale: {chunk_id}")
                continue
        except OSError as exc:
            errors.append(f"Could not inspect rendered chunk {chunk_id}: {exc}")
            continue

        render_freshness = build_chunk_render_freshness(project_dir, data, chunk_id)
        if not is_current(render_freshness):
            reason = render_freshness["reason"]
            detail = f": {reason}" if reason is not None else ""
            errors.append(
                f"Rendered chunk dependencies are not current: {chunk_id} "
                f"({render_freshness['state']}{detail})"
            )
            continue

        duration = _number_field(render_metadata, "duration_seconds") or round(end - start, 3)
        results.append(
            {
                "chunk_id": chunk_id,
                "start": start,
                "end": end,
                "path": render_path,
                "file": render_file,
                "duration_seconds": duration,
                "artifact_fingerprint": artifact_expected,
            }
        )
    return results, errors


def _pipeline_errors(project_dir: Path, data: ProjectState) -> list[str]:
    freshness_summary = build_pipeline_freshness(project_dir, data)
    errors: list[str] = []
    for stage in ("raw", "audio", "transcript", "alignment"):
        result = freshness_summary[stage]
        if not is_current(result):
            reason = result["reason"]
            detail = f" ({reason})" if reason is not None else ""
            errors.append(f"{stage} freshness is not current: {result['state']}{detail}")
    timeline_freshness = build_timeline_chunk_freshness(project_dir, data, freshness_summary)
    for stage in ("timeline", "chunking"):
        result = timeline_freshness[stage]
        if not is_current(result):
            reason = result["reason"]
            detail = f" ({reason})" if reason is not None else ""
            errors.append(f"{stage} freshness is not current: {result['state']}{detail}")
    return errors


def _run_ffmpeg_concat(ffmpeg: str, chunks: list[ChunkForCompose], output_file: Path) -> list[str]:
    list_file: Path | None = None
    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output_file.parent,
            prefix=".concat.",
            suffix=".txt",
            delete=False,
        ) as handle:
            list_file = Path(handle.name)
            for chunk in chunks:
                handle.write(f"file '{_ffmpeg_concat_path(chunk['file'])}'\n")
            handle.flush()
        with temporary_artifact_path(output_file) as temporary_output:
            command = [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
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
    except OSError as exc:
        return [f"Could not prepare final composition: {exc}"]
    finally:
        if list_file is not None:
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                pass
    return []


def _final_metadata(data: ProjectState, chunks: list[ChunkForCompose], output_file: Path) -> JsonObject:
    source_fingerprints: JsonObject = {}
    for chunk in chunks:
        source_fingerprints[chunk["chunk_id"]] = chunk["artifact_fingerprint"]
    timeline = data.get("timeline")
    chunk_plan_fingerprint = current_chunk_plan_fingerprint(data)
    if timeline is None or chunk_plan_fingerprint is None:
        raise ValueError("Cannot compose final metadata without current timeline and chunk plan")
    return {
        "path": data["project"]["final"],
        "status": RENDERED_STATUS,
        "chunk_ids": _json_string_list([chunk["chunk_id"] for chunk in chunks]),
        "chunk_paths": _json_string_list([chunk["path"] for chunk in chunks]),
        "duration_seconds": round(sum(chunk["duration_seconds"] for chunk in chunks), 3),
        "composed_at": utc_now_iso(),
        "source_fingerprints": source_fingerprints,
        "timeline_fingerprint": build_timeline_fingerprint(timeline),
        "chunk_plan_fingerprint": chunk_plan_fingerprint,
        "artifact_fingerprint": stat_fingerprint(output_file),
    }


def _record_final_success(project_dir: Path, data: ProjectState, metadata: JsonObject) -> None:
    now = utc_now_iso()
    renders = _ensure_renders(data)
    renders["final"] = metadata
    resolve_matching_failures(data, stage=FINAL_COMPOSE_STAGE, scope=FINAL_COMPOSE_SCOPE)
    data["project"]["updated_at"] = now
    write_project(project_file_for(project_dir), data)


def _record_final_failure(project_dir: Path, data: ProjectState, output_path: str, errors: list[str]) -> None:
    context: JsonObject = {"output_path": output_path}
    record_failure(
        data,
        stage=FINAL_COMPOSE_STAGE,
        scope=FINAL_COMPOSE_SCOPE,
        errors=errors,
        recommended_next_action=FINAL_COMPOSE_RECOMMENDED_NEXT_ACTION,
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


def _render_chunks_metadata(data: ProjectState) -> JsonObject:
    renders = data.get("renders")
    if not isinstance(renders, dict):
        return {}
    chunks = renders.get("chunks")
    if isinstance(chunks, dict):
        return cast(JsonObject, chunks)
    return {}


def _final_render_metadata(data: ProjectState) -> JsonObject | None:
    renders = data.get("renders")
    if not isinstance(renders, dict):
        return None
    final = renders.get("final")
    if isinstance(final, dict):
        return cast(JsonObject, final)
    return None


def _object_field(data: JsonObject, key: str) -> JsonObject | None:
    value = data.get(key)
    if isinstance(value, dict):
        return cast(JsonObject, value)
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


def _ffmpeg_concat_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "'\\''")
