"""Artifact fingerprints, freshness evaluation, and atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal, TypedDict, cast

from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState


FingerprintKind = Literal["stat_v1", "sha256_v1"]
FreshnessState = Literal["current", "not_created", "missing", "stale", "unverified"]

FRESHNESS_CURRENT: FreshnessState = "current"
FRESHNESS_NOT_CREATED: FreshnessState = "not_created"
FRESHNESS_MISSING: FreshnessState = "missing"
FRESHNESS_STALE: FreshnessState = "stale"
FRESHNESS_UNVERIFIED: FreshnessState = "unverified"

REASON_METADATA_MISSING = "metadata_missing"
REASON_FINGERPRINT_MISSING = "fingerprint_missing"
REASON_ARTIFACT_MISSING = "artifact_missing"
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"
REASON_UPSTREAM_STALE = "upstream_stale"


class FreshnessResult(TypedDict):
    state: FreshnessState
    reason: str | None


class PipelineFreshness(TypedDict):
    raw: FreshnessResult
    audio: FreshnessResult
    transcript: FreshnessResult
    alignment: FreshnessResult


def stat_fingerprint(path: Path) -> JsonObject:
    stat = path.stat()
    return {
        "kind": "stat_v1",
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def sha256_fingerprint(path: Path) -> JsonObject:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {
        "kind": "sha256_v1",
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


def sha256_json_fingerprint(value: JsonValue) -> JsonObject:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "kind": "sha256_v1",
        "size_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def fingerprints_match(expected: object, actual: JsonObject) -> bool:
    parsed = fingerprint_from_json(expected)
    return parsed == actual if parsed is not None else False


def fingerprint_from_json(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    data = cast(dict[object, object], value)
    kind = data.get("kind")
    size = data.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    if kind == "stat_v1":
        modified = data.get("modified_ns")
        if not isinstance(modified, int) or isinstance(modified, bool) or modified < 0:
            return None
        fingerprint: JsonObject = {
            "kind": "stat_v1",
            "size_bytes": size,
            "modified_ns": modified,
        }
        return fingerprint
    if kind == "sha256_v1":
        digest = data.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            return None
        fingerprint = {
            "kind": "sha256_v1",
            "size_bytes": size,
            "sha256": digest,
        }
        return fingerprint
    return None


def build_pipeline_freshness(project_dir: Path, data: ProjectState) -> PipelineFreshness:
    project = data["project"]
    media = data.get("media", {})
    raw_metadata = _object_field(media, "raw")
    raw_path = project_dir / project["video"]
    raw, raw_actual = _source_freshness(raw_metadata, raw_path)

    audio_metadata = _object_field(_object_field(media, "audio") or {}, "narration")
    audio_path = artifact_path(project_dir, data, _metadata_path(audio_metadata, "audio/narration.wav"))
    audio, audio_actual = _derived_freshness(
        audio_metadata,
        audio_path,
        fingerprint_kind="stat_v1",
        upstream=raw,
        upstream_actual=raw_actual,
    )

    transcript_root = data.get("transcript", {})
    transcript_metadata = _object_field(transcript_root, "narration")
    transcript_path = artifact_path(project_dir, data, _metadata_path(transcript_metadata, "transcripts/narration.json"))
    transcript, transcript_actual = _derived_freshness(
        transcript_metadata,
        transcript_path,
        fingerprint_kind="sha256_v1",
        upstream=audio,
        upstream_actual=audio_actual,
    )

    alignment_root = data.get("alignment", {})
    alignment_metadata = _object_field(alignment_root, "script")
    alignment_path = artifact_path(project_dir, data, _metadata_path(alignment_metadata, "alignment/script_alignment.json"))
    alignment = _alignment_freshness(
        alignment_metadata,
        alignment_path,
        upstream=transcript,
        project_dir=project_dir,
        data=data,
    )

    return {
        "raw": raw,
        "audio": audio,
        "transcript": transcript,
        "alignment": alignment,
    }


def freshness(state: FreshnessState, reason: str | None) -> FreshnessResult:
    return {"state": state, "reason": reason}


def is_current(result: FreshnessResult) -> bool:
    return result["state"] == FRESHNESS_CURRENT


def atomic_write_json(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        if temporary is not None:
            _remove_quietly(temporary)
        raise


@contextmanager
def temporary_artifact_path(target: Path) -> Iterator[Path]:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=target.parent,
        prefix=f".{target.stem}.",
        suffix=target.suffix,
        delete=False,
    )
    temporary = Path(handle.name)
    handle.close()
    _remove_quietly(temporary)
    try:
        yield temporary
    finally:
        _remove_quietly(temporary)


def replace_artifact(temporary: Path, target: Path) -> None:
    os.replace(temporary, target)


def _source_freshness(
    metadata: JsonObject | None,
    path: Path,
) -> tuple[FreshnessResult, JsonObject | None]:
    if metadata is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING), None
    if not path.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING), None
    expected = fingerprint_from_json(metadata.get("source_fingerprint"))
    if expected is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING), None
    try:
        actual = stat_fingerprint(path)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING), None
    if expected != actual:
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH), actual
    return freshness(FRESHNESS_CURRENT, None), actual


def _derived_freshness(
    metadata: JsonObject | None,
    path: Path,
    *,
    fingerprint_kind: FingerprintKind,
    upstream: FreshnessResult,
    upstream_actual: JsonObject | None,
) -> tuple[FreshnessResult, JsonObject | None]:
    if metadata is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING), None
    if not path.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING), None
    source_expected = fingerprint_from_json(metadata.get("source_fingerprint"))
    artifact_expected = fingerprint_from_json(metadata.get("artifact_fingerprint"))
    if source_expected is None or artifact_expected is None:
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING), None
    try:
        actual = stat_fingerprint(path) if fingerprint_kind == "stat_v1" else sha256_fingerprint(path)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING), None
    if artifact_expected != actual:
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH), actual
    if not is_current(upstream) or upstream_actual is None or source_expected != upstream_actual:
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE), actual
    return freshness(FRESHNESS_CURRENT, None), actual


def _alignment_sources_match(
    project_dir: Path,
    data: ProjectState,
    metadata: JsonObject | None,
) -> bool:
    if metadata is None:
        return False
    fingerprints = _object_field(metadata, "source_fingerprints")
    if fingerprints is None:
        return False
    script_digest = fingerprints.get("script_sha256")
    transcript_digest = fingerprints.get("transcript_sha256")
    if not isinstance(script_digest, str) or not isinstance(transcript_digest, str):
        return False
    script_file = project_dir / data["project"]["script"]
    transcript_path = _metadata_path(
        _object_field(data.get("transcript", {}), "narration"),
        "transcripts/narration.json",
    )
    transcript_file = artifact_path(project_dir, data, transcript_path)
    try:
        return (
            sha256_fingerprint(script_file).get("sha256") == script_digest
            and sha256_fingerprint(transcript_file).get("sha256") == transcript_digest
        )
    except OSError:
        return False


def _alignment_freshness(
    metadata: JsonObject | None,
    path: Path,
    *,
    upstream: FreshnessResult,
    project_dir: Path,
    data: ProjectState,
) -> FreshnessResult:
    if metadata is None:
        return freshness(FRESHNESS_NOT_CREATED, REASON_METADATA_MISSING)
    if not path.is_file():
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    artifact_expected = fingerprint_from_json(metadata.get("artifact_fingerprint"))
    if artifact_expected is None or not isinstance(metadata.get("source_fingerprints"), dict):
        return freshness(FRESHNESS_UNVERIFIED, REASON_FINGERPRINT_MISSING)
    try:
        actual = sha256_fingerprint(path)
    except OSError:
        return freshness(FRESHNESS_MISSING, REASON_ARTIFACT_MISSING)
    if artifact_expected != actual or not _alignment_sources_match(project_dir, data, metadata):
        return freshness(FRESHNESS_STALE, REASON_FINGERPRINT_MISMATCH)
    if not is_current(upstream):
        return freshness(FRESHNESS_STALE, REASON_UPSTREAM_STALE)
    return freshness(FRESHNESS_CURRENT, None)


def _object_field(data: JsonObject, key: str) -> JsonObject | None:
    value = data.get(key)
    if isinstance(value, dict):
        return cast(JsonObject, value)
    return None


def _metadata_path(metadata: JsonObject | None, default: str) -> str:
    if metadata is not None:
        value = metadata.get("path")
        if isinstance(value, str) and value:
            return value
    return default


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
