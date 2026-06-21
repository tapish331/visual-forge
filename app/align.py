"""Deterministic script-to-transcript alignment and checkpoint updates."""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from .artifacts import (
    FRESHNESS_CURRENT,
    atomic_write_json,
    build_pipeline_freshness,
    is_current,
    sha256_fingerprint,
)
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import (
    JsonObject,
    JsonValue,
    ProjectError,
    ProjectState,
    load_project,
    project_file_for,
    utc_now_iso,
    write_project,
)
from .transcribe import NARRATION_TRANSCRIPT, TRANSCRIPTS_DIR


ALIGNMENT_DIR = "alignment"
SCRIPT_ALIGNMENT = "script_alignment.json"
ALIGNMENT_METHOD = "sequence_matcher_words_v1"
ALIGNMENT_STAGE = "align"
ALIGNMENT_SCOPE = f"alignment:{ALIGNMENT_DIR}/{SCRIPT_ALIGNMENT}"
ALIGNMENT_RECOMMENDED_NEXT_ACTION = (
    "Fix the configured narration script or timestamped narration transcript, then rerun align."
)
BLOCK_CONFIDENCE_THRESHOLD = 0.5
REVIEW_CONFIDENCE_THRESHOLD = 0.25
REVIEW_MINIMUM_MATCHED_TOKENS = 2

TOKEN_PATTERN = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
BLANK_LINE_PATTERN = re.compile(r"\r?\n[ \t]*\r?\n+")
SENTENCE_END_PATTERN = re.compile(r"[.!?\u0964]+(?:[\"')\]]+)?(?=\s|$)")
ANNOTATION_LINE_PATTERN = re.compile(
    r"^[ \t]*\[(?P<timestamp>\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\]"
    r"[ \t]*-[ \t]*(?P<speaker>[^\r\n]*\S)[ \t]*\r?$",
    re.MULTILINE,
)


class AlignmentResult(TypedDict):
    project_dir: str
    source_script: str
    source_transcript: str
    output_path: str
    method: str
    success: bool
    metadata: JsonObject | None
    errors: list[str]


class AlignmentReviewSummary(TypedDict):
    project_dir: str
    alignment_path: str
    stale: bool
    coverage: float | None
    aligned_count: int
    needs_review_count: int
    unmatched_count: int
    items: list[JsonObject]


class ScriptToken(TypedDict):
    value: str
    start: int
    end: int


class TimedWord(TypedDict):
    start: float
    end: float
    word: str


class TranscriptToken(TypedDict):
    value: str
    word_index: int


class ScriptBlock(TypedDict):
    text: str
    start: int
    end: int
    speaker: NotRequired[str]
    section_timestamp_hint_seconds: NotRequired[float]


def align_project(project_dir: Path) -> AlignmentResult:
    data = load_project(project_dir)
    source_script = data["project"]["script"]
    source_transcript = f"{TRANSCRIPTS_DIR}/{NARRATION_TRANSCRIPT}"
    output_relative = f"{ALIGNMENT_DIR}/{SCRIPT_ALIGNMENT}"
    script_file = project_dir / source_script
    transcript_file = artifact_path(project_dir, data, source_transcript)
    output_file = artifact_path(project_dir, data, output_relative)

    errors = validate_alignment_prerequisites(data, project_dir, script_file, transcript_file)
    script_text: str | None = None
    transcript_payload: dict[str, object] | None = None
    source_fingerprints: JsonObject | None = None
    if not errors:
        script_text, read_errors = _read_script(script_file)
        errors.extend(read_errors)
    if not errors:
        transcript_payload, read_errors = _read_transcript(transcript_file)
        errors.extend(read_errors)
    if not errors:
        source_fingerprints, fingerprint_errors = build_source_fingerprints(script_file, transcript_file)
        errors.extend(fingerprint_errors)

    artifact: JsonObject | None = None
    metadata: JsonObject | None = None
    if (
        not errors
        and script_text is not None
        and transcript_payload is not None
        and source_fingerprints is not None
    ):
        artifact, metadata, errors = build_alignment_artifact(
            script_text,
            transcript_payload,
            source_script=source_script,
            source_transcript=source_transcript,
            source_fingerprints=source_fingerprints,
        )
    if not errors and artifact is not None:
        errors = _write_alignment_artifact(output_file, artifact)
    if not errors and metadata is not None:
        try:
            metadata["artifact_fingerprint"] = sha256_fingerprint(output_file)
        except OSError as exc:
            errors.append(f"Could not fingerprint alignment artifact: {exc}")

    result: AlignmentResult = {
        "project_dir": str(project_dir),
        "source_script": source_script,
        "source_transcript": source_transcript,
        "output_path": str(output_file),
        "method": ALIGNMENT_METHOD,
        "success": not errors and metadata is not None,
        "metadata": metadata,
        "errors": errors,
    }
    if result["success"] and metadata is not None:
        _record_alignment_success(project_dir, data, metadata)
    else:
        _record_alignment_failure(project_dir, data, source_script, source_transcript, output_relative, errors)
    return result


def validate_alignment_prerequisites(
    data: ProjectState,
    project_dir: Path,
    script_file: Path,
    transcript_file: Path,
) -> list[str]:
    errors: list[str] = []
    narration = _narration_transcript(data)
    expected_transcript = f"{TRANSCRIPTS_DIR}/{NARRATION_TRANSCRIPT}"
    if narration is None:
        errors.append("Missing transcript.narration metadata. Run transcribe before align.")
    else:
        if _string_field(narration, "status", "") != "transcribed":
            errors.append("Narration transcript is not marked transcribed. Rerun transcribe.")
        if _string_field(narration, "path", "") != expected_transcript:
            errors.append("transcript.narration points to an unexpected path. Rerun transcribe.")
        elif not is_current(build_pipeline_freshness(project_dir, data)["transcript"]):
            errors.append("Narration transcript is stale or unverified. Rerun transcribe before align.")

    if not script_file.exists():
        errors.append(f"Missing narration script: {data['project']['script']}")
    elif not script_file.is_file():
        errors.append(f"Narration script path is not a file: {data['project']['script']}")

    if not transcript_file.exists():
        errors.append(f"Missing narration transcript file: {expected_transcript}")
    elif not transcript_file.is_file():
        errors.append(f"Narration transcript path is not a file: {expected_transcript}")
    return errors


def build_alignment_artifact(
    script_text: str,
    transcript_payload: dict[str, object],
    *,
    source_script: str,
    source_transcript: str,
    source_fingerprints: JsonObject,
) -> tuple[JsonObject | None, JsonObject | None, list[str]]:
    if not script_text.strip():
        return None, None, ["Narration script is empty."]

    script_blocks = _split_script_blocks(script_text)
    script_tokens = _tokenize_script(script_text, script_blocks)
    if not script_tokens:
        return None, None, ["Narration script does not contain alignable words."]

    timed_words = _extract_timed_words(transcript_payload)
    if not timed_words:
        return None, None, ["Narration transcript does not contain usable word timestamps."]

    transcript_tokens = _tokenize_transcript_words(timed_words)
    if not transcript_tokens:
        return None, None, ["Narration transcript does not contain alignable timestamped words."]

    matcher = SequenceMatcher(
        None,
        [token["value"] for token in script_tokens],
        [token["value"] for token in transcript_tokens],
        autojunk=False,
    )
    token_mapping: dict[int, int] = {}
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            token_mapping[match.a + offset] = match.b + offset

    if not token_mapping:
        return None, None, ["No script words could be aligned to the timestamped transcript."]

    blocks = _build_aligned_blocks(script_blocks, script_tokens, transcript_tokens, timed_words, token_mapping)
    aligned_blocks = sum(1 for block in blocks if block.get("status") == "aligned")
    if aligned_blocks == 0:
        return None, None, ["No script block reached the minimum alignment confidence of 0.50."]

    created_at = utc_now_iso()
    matched_script_tokens = len(token_mapping)
    coverage = _ratio(matched_script_tokens, len(script_tokens))
    needs_review_blocks = sum(1 for block in blocks if block.get("status") == "needs_review")
    unmatched_blocks = sum(1 for block in blocks if block.get("status") == "unmatched")
    duration = _number_value(transcript_payload.get("duration_seconds"))
    language_value = transcript_payload.get("language")
    language = language_value if isinstance(language_value, str) and language_value else None
    artifact: JsonObject = {
        "schema_version": 1,
        "source_script": source_script,
        "source_transcript": source_transcript,
        "method": ALIGNMENT_METHOD,
        "source_fingerprints": source_fingerprints,
        "language": language,
        "duration_seconds": duration,
        "script_text_length": len(script_text),
        "script_token_count": len(script_tokens),
        "transcript_word_count": len(timed_words),
        "matched_script_tokens": matched_script_tokens,
        "coverage": coverage,
        "blocks": _blocks_json(blocks),
        "created_at": created_at,
    }
    metadata: JsonObject = {
        "path": f"{ALIGNMENT_DIR}/{SCRIPT_ALIGNMENT}",
        "source_script": source_script,
        "source_transcript": source_transcript,
        "method": ALIGNMENT_METHOD,
        "status": "aligned",
        "blocks": len(blocks),
        "aligned_blocks": aligned_blocks,
        "coverage": coverage,
        "needs_review_blocks": needs_review_blocks,
        "unmatched_blocks": unmatched_blocks,
        "source_fingerprints": source_fingerprints,
        "aligned_at": created_at,
    }
    return artifact, metadata, []


def format_alignment_result(result: AlignmentResult) -> str:
    status = "aligned" if result["success"] else "failed"
    lines = [
        f"Alignment: {result['output_path']}",
        f"Script: {result['source_script']}",
        f"Transcript: {result['source_transcript']}",
        f"Method: {result['method']}",
        f"Status: {status}",
    ]
    metadata = result["metadata"]
    if result["success"] and metadata is not None:
        lines.append(f"Blocks: {metadata.get('blocks')}")
        lines.append(f"Aligned: {metadata.get('aligned_blocks')}")
        lines.append(f"Coverage: {metadata.get('coverage')}")
        lines.append(f"Needs review: {metadata.get('needs_review_blocks')}")
        lines.append(f"Unmatched: {metadata.get('unmatched_blocks')}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def build_alignment_review(project_dir: Path) -> AlignmentReviewSummary:
    data = load_project(project_dir)
    metadata = _alignment_metadata(data)
    alignment_path = _string_field(metadata, "path", f"{ALIGNMENT_DIR}/{SCRIPT_ALIGNMENT}")
    artifact_file = artifact_path(project_dir, data, alignment_path)
    artifact = _read_alignment_artifact(artifact_file)
    raw_blocks = artifact.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ProjectError(f"Alignment artifact {artifact_file} must contain a blocks list")

    aligned_count = 0
    needs_review_count = 0
    unmatched_count = 0
    items: list[JsonObject] = []
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            raise ProjectError(f"Alignment artifact {artifact_file} contains an invalid block")
        block = cast(JsonObject, raw_block)
        status = _string_field(block, "status", "unknown")
        if status == "aligned":
            aligned_count += 1
        elif status == "needs_review":
            needs_review_count += 1
        elif status == "unmatched":
            unmatched_count += 1
        if status in {"needs_review", "unmatched"}:
            items.append(_review_item(block))

    coverage_value = _number_value(artifact.get("coverage"))
    coverage = float(coverage_value) if coverage_value is not None else None
    return {
        "project_dir": str(project_dir),
        "alignment_path": alignment_path,
        "stale": alignment_is_stale(project_dir, data),
        "coverage": coverage,
        "aligned_count": aligned_count,
        "needs_review_count": needs_review_count,
        "unmatched_count": unmatched_count,
        "items": items,
    }


def format_alignment_review(summary: AlignmentReviewSummary) -> str:
    lines = [
        f"Alignment: {summary['alignment_path']}",
        f"Stale: {'yes' if summary['stale'] else 'no'}",
        f"Coverage: {summary['coverage']}",
        f"Aligned: {summary['aligned_count']}",
        f"Needs review: {summary['needs_review_count']}",
        f"Unmatched: {summary['unmatched_count']}",
    ]
    for item in summary["items"]:
        lines.append(
            f"{item.get('id')} [{item.get('status')}] confidence={item.get('confidence')}: {item.get('text')}"
        )
        speaker = item.get("speaker")
        timestamp_hint = item.get("timestamp_hint")
        if speaker is not None or timestamp_hint is not None:
            lines.append(f"  speaker={speaker}, timestamp_hint={timestamp_hint}")
        candidate_start = item.get("candidate_start")
        candidate_end = item.get("candidate_end")
        if candidate_start is not None or candidate_end is not None:
            lines.append(f"  candidate={candidate_start}-{candidate_end}")
    return "\n".join(lines)


def build_source_fingerprints(script_file: Path, transcript_file: Path) -> tuple[JsonObject | None, list[str]]:
    try:
        return {
            "script_sha256": _sha256_file(script_file),
            "transcript_sha256": _sha256_file(transcript_file),
        }, []
    except OSError as exc:
        return None, [f"Could not fingerprint alignment sources: {exc}"]


def alignment_is_stale(project_dir: Path, data: ProjectState) -> bool:
    metadata = _alignment_metadata(data)
    if _string_field(metadata, "status", "") != "aligned":
        return False
    return build_pipeline_freshness(project_dir, data)["alignment"]["state"] != FRESHNESS_CURRENT


def _read_script(script_file: Path) -> tuple[str | None, list[str]]:
    try:
        return script_file.read_text(encoding="utf-8-sig"), []
    except (OSError, UnicodeError) as exc:
        return None, [f"Could not read narration script: {exc}"]


def _read_transcript(transcript_file: Path) -> tuple[dict[str, object] | None, list[str]]:
    try:
        payload: object = json.loads(transcript_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON in narration transcript: {exc.msg}"]
    except (OSError, UnicodeError) as exc:
        return None, [f"Could not read narration transcript: {exc}"]
    if not isinstance(payload, dict):
        return None, ["Narration transcript must be a JSON object."]
    return cast(dict[str, object], payload), []


def _tokenize_script(text: str, blocks: list[ScriptBlock]) -> list[ScriptToken]:
    normalized = text.replace("\u2018", "'").replace("\u2019", "'")
    tokens: list[ScriptToken] = []
    for block in blocks:
        for match in TOKEN_PATTERN.finditer(normalized, block["start"], block["end"]):
            tokens.append({"value": match.group(0).casefold(), "start": match.start(), "end": match.end()})
    return tokens


def _tokenize_transcript_words(words: list[TimedWord]) -> list[TranscriptToken]:
    tokens: list[TranscriptToken] = []
    for word_index, item in enumerate(words):
        normalized = item["word"].replace("\u2018", "'").replace("\u2019", "'")
        for match in TOKEN_PATTERN.finditer(normalized):
            tokens.append({"value": match.group(0).casefold(), "word_index": word_index})
    return tokens


def _extract_timed_words(payload: dict[str, object]) -> list[TimedWord]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return []
    words: list[TimedWord] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            continue
        segment = cast(dict[str, object], raw_segment)
        raw_words = segment.get("words")
        if not isinstance(raw_words, list):
            continue
        for raw_word in raw_words:
            if not isinstance(raw_word, dict):
                continue
            word_data = cast(dict[str, object], raw_word)
            start = _number_value(word_data.get("start"))
            end = _number_value(word_data.get("end"))
            word = word_data.get("word")
            if start is None or end is None or end < start or not isinstance(word, str) or not word.strip():
                continue
            words.append({"start": float(start), "end": float(end), "word": word})
    return words


def _build_aligned_blocks(
    script_blocks: list[ScriptBlock],
    script_tokens: list[ScriptToken],
    transcript_tokens: list[TranscriptToken],
    timed_words: list[TimedWord],
    token_mapping: dict[int, int],
) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    for block_number, block in enumerate(script_blocks, start=1):
        block_token_indices = [
            index
            for index, token in enumerate(script_tokens)
            if token["start"] >= block["start"] and token["end"] <= block["end"]
        ]
        mapped_word_indices = [
            transcript_tokens[token_mapping[index]]["word_index"]
            for index in block_token_indices
            if index in token_mapping
        ]
        matched_tokens = sum(1 for index in block_token_indices if index in token_mapping)
        confidence = _ratio(matched_tokens, len(block_token_indices))
        candidate_start: float | None = None
        candidate_end: float | None = None
        if mapped_word_indices:
            candidate_start = timed_words[min(mapped_word_indices)]["start"]
            candidate_end = timed_words[max(mapped_word_indices)]["end"]

        start: float | None = None
        end: float | None = None
        review_start: float | None = None
        review_end: float | None = None
        if candidate_start is not None and confidence >= BLOCK_CONFIDENCE_THRESHOLD:
            status = "aligned"
            start = candidate_start
            end = candidate_end
        elif (
            candidate_start is not None
            and confidence >= REVIEW_CONFIDENCE_THRESHOLD
            and matched_tokens >= REVIEW_MINIMUM_MATCHED_TOKENS
        ):
            status = "needs_review"
            review_start = candidate_start
            review_end = candidate_end
        else:
            status = "unmatched"

        block_data: JsonObject = {
            "id": f"block_{block_number:03d}",
            "text": block["text"],
            "script_start_char": block["start"],
            "script_end_char": block["end"],
            "script_token_count": len(block_token_indices),
            "matched_tokens": matched_tokens,
            "confidence": confidence,
            "start": start,
            "end": end,
            "candidate_start": review_start,
            "candidate_end": review_end,
            "status": status,
        }
        speaker = block.get("speaker")
        if speaker is not None:
            block_data["speaker"] = speaker
        timestamp_hint = block.get("section_timestamp_hint_seconds")
        if timestamp_hint is not None:
            block_data["section_timestamp_hint_seconds"] = timestamp_hint
        blocks.append(block_data)
    return blocks


def _blocks_json(blocks: list[JsonObject]) -> list[JsonValue]:
    values: list[JsonValue] = []
    for block in blocks:
        values.append(block)
    return values


def _split_script_blocks(text: str) -> list[ScriptBlock]:
    annotations = list(ANNOTATION_LINE_PATTERN.finditer(text))
    if not annotations:
        return _split_content_blocks(text, 0, len(text), speaker=None, timestamp_hint=None)

    blocks: list[ScriptBlock] = []
    cursor = 0
    speaker: str | None = None
    timestamp_hint: float | None = None
    for annotation in annotations:
        blocks.extend(
            _split_content_blocks(
                text,
                cursor,
                annotation.start(),
                speaker=speaker,
                timestamp_hint=timestamp_hint,
            )
        )
        speaker = annotation.group("speaker").strip()
        timestamp_hint = _timestamp_seconds(annotation.group("timestamp"))
        cursor = annotation.end()
    blocks.extend(
        _split_content_blocks(text, cursor, len(text), speaker=speaker, timestamp_hint=timestamp_hint)
    )
    return blocks


def _split_content_blocks(
    text: str,
    start: int,
    end: int,
    *,
    speaker: str | None,
    timestamp_hint: float | None,
) -> list[ScriptBlock]:
    paragraph_spans: list[tuple[int, int]] = []
    cursor = start
    for separator in BLANK_LINE_PATTERN.finditer(text, start, end):
        span = _trim_span(text, cursor, separator.start())
        if span is not None:
            paragraph_spans.append(span)
        cursor = separator.end()
    final_span = _trim_span(text, cursor, end)
    if final_span is not None:
        paragraph_spans.append(final_span)

    blocks: list[ScriptBlock] = []
    for paragraph_start, paragraph_end in paragraph_spans:
        sentence_start = paragraph_start
        for ending in SENTENCE_END_PATTERN.finditer(text, paragraph_start, paragraph_end):
            span = _trim_span(text, sentence_start, ending.end())
            if span is not None:
                blocks.append(_block_from_span(text, span, speaker=speaker, timestamp_hint=timestamp_hint))
            sentence_start = ending.end()
        remainder = _trim_span(text, sentence_start, paragraph_end)
        if remainder is not None:
            blocks.append(_block_from_span(text, remainder, speaker=speaker, timestamp_hint=timestamp_hint))
    return blocks


def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _block_from_span(
    text: str,
    span: tuple[int, int],
    *,
    speaker: str | None,
    timestamp_hint: float | None,
) -> ScriptBlock:
    start, end = span
    block: ScriptBlock = {"text": text[start:end], "start": start, "end": end}
    if speaker is not None:
        block["speaker"] = speaker
    if timestamp_hint is not None:
        block["section_timestamp_hint_seconds"] = timestamp_hint
    return block


def _timestamp_seconds(value: str) -> float:
    hours_text, minutes_text, seconds_text = value.split(":", maxsplit=2)
    return int(hours_text) * 3600 + int(minutes_text) * 60 + float(seconds_text)


def _write_alignment_artifact(output_file: Path, artifact: JsonObject) -> list[str]:
    try:
        atomic_write_json(output_file, artifact)
    except (OSError, TypeError, ValueError) as exc:
        return [f"Could not write alignment artifact: {exc}"]
    if not output_file.exists() or not output_file.is_file():
        return [f"Alignment completed but did not create {ALIGNMENT_DIR}/{SCRIPT_ALIGNMENT}"]
    return []


def _record_alignment_success(project_dir: Path, data: ProjectState, metadata: JsonObject) -> None:
    alignment = _ensure_alignment(data)
    alignment["script"] = metadata
    resolve_matching_failures(data, stage=ALIGNMENT_STAGE, scope=ALIGNMENT_SCOPE)
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_alignment_failure(
    project_dir: Path,
    data: ProjectState,
    source_script: str,
    source_transcript: str,
    output_path: str,
    errors: list[str],
) -> None:
    context: JsonObject = {
        "source_script": source_script,
        "source_transcript": source_transcript,
        "output_path": output_path,
        "method": ALIGNMENT_METHOD,
    }
    record_failure(
        data,
        stage=ALIGNMENT_STAGE,
        scope=ALIGNMENT_SCOPE,
        errors=errors,
        recommended_next_action=ALIGNMENT_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _ensure_alignment(data: ProjectState) -> JsonObject:
    alignment = data.get("alignment")
    if alignment is None:
        alignment = {}
        data["alignment"] = alignment
    return alignment


def _narration_transcript(data: ProjectState) -> JsonObject | None:
    transcript = data.get("transcript")
    if not isinstance(transcript, dict):
        return None
    narration = transcript.get("narration")
    if isinstance(narration, dict):
        return cast(JsonObject, narration)
    return None


def _alignment_metadata(data: ProjectState) -> JsonObject:
    alignment = data.get("alignment")
    if not isinstance(alignment, dict):
        return {}
    script = alignment.get("script")
    if isinstance(script, dict):
        return cast(JsonObject, script)
    return {}


def _read_alignment_artifact(artifact_file: Path) -> JsonObject:
    if not artifact_file.is_file():
        raise ProjectError(f"Missing alignment artifact: {artifact_file}")
    try:
        payload: object = json.loads(artifact_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectError(f"Invalid JSON in {artifact_file}: {exc.msg}") from exc
    except OSError as exc:
        raise ProjectError(f"Could not read {artifact_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectError(f"Alignment artifact {artifact_file} must be a JSON object")
    return cast(JsonObject, payload)


def _review_item(block: JsonObject) -> JsonObject:
    return {
        "id": _string_field(block, "id", "unknown"),
        "status": _string_field(block, "status", "unknown"),
        "confidence": _number_value(block.get("confidence")),
        "start": _number_value(block.get("start")),
        "end": _number_value(block.get("end")),
        "candidate_start": _number_value(block.get("candidate_start")),
        "candidate_end": _number_value(block.get("candidate_end")),
        "speaker": _optional_string(block.get("speaker")),
        "timestamp_hint": _number_value(block.get("section_timestamp_hint_seconds")),
        "text": _string_field(block, "text", ""),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _string_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _number_value(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)
