"""Narration transcription providers and project checkpoint updates."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, TypedDict, cast

from .artifacts import atomic_write_json, build_pipeline_freshness, is_current, sha256_fingerprint
from .audio import AUDIO_DIR, NARRATION_AUDIO
from .failures import record_failure, resolve_matching_failures
from .layout import artifact_path
from .project import JsonObject, JsonValue, ProjectState, load_project, project_file_for, utc_now_iso, write_project


TRANSCRIPTS_DIR = "transcripts"
NARRATION_TRANSCRIPT = "narration.json"
MODEL_CACHE_DIR = Path("models") / "faster-whisper"

DEFAULT_TRANSCRIBE_PROVIDER = "faster-whisper"
DEFAULT_TRANSCRIBE_MODEL = "large-v3"
DEFAULT_TRANSCRIBE_DEVICE = "auto"
DEFAULT_TRANSCRIBE_COMPUTE_TYPE = "auto"

TRANSCRIBE_STAGE = "transcribe"
TRANSCRIBE_SCOPE = f"transcript:{TRANSCRIPTS_DIR}/{NARRATION_TRANSCRIPT}"
TRANSCRIBE_RECOMMENDED_NEXT_ACTION = (
    "Fix narration audio, local transcription dependencies, model cache, or device settings, then rerun transcribe."
)


class TranscriptWord(TypedDict):
    start: float
    end: float
    word: str
    probability: float | None


class RequiredTranscriptSegment(TypedDict):
    id: int
    start: float
    end: float
    text: str


class TranscriptSegment(RequiredTranscriptSegment, total=False):
    words: list[TranscriptWord]


class TranscriptData(TypedDict):
    text: str
    segments: list[TranscriptSegment]


class TranscribeResult(TypedDict):
    project_dir: str
    source_path: str
    output_path: str
    provider: str
    model: str
    device: str
    compute_type: str
    success: bool
    metadata: JsonObject | None
    errors: list[str]


class FasterWhisperModel(Protocol):
    def transcribe(
        self,
        audio: str,
        *,
        beam_size: int,
        word_timestamps: bool,
        vad_filter: bool,
        language: str | None = None,
    ) -> tuple[Iterable[object], object]: ...


class FasterWhisperModelFactory(Protocol):
    def __call__(
        self,
        model_size_or_path: str,
        *,
        device: str,
        compute_type: str,
        download_root: str,
    ) -> FasterWhisperModel: ...


def transcribe_project(
    project_dir: Path,
    *,
    provider: str,
    model: str,
    language: str | None,
    device: str = DEFAULT_TRANSCRIBE_DEVICE,
    compute_type: str = DEFAULT_TRANSCRIBE_COMPUTE_TYPE,
) -> TranscribeResult:
    data = load_project(project_dir)
    source_path = f"{AUDIO_DIR}/{NARRATION_AUDIO}"
    output_path = f"{TRANSCRIPTS_DIR}/{NARRATION_TRANSCRIPT}"
    source_file = artifact_path(project_dir, data, source_path)
    output_file = artifact_path(project_dir, data, output_path)

    transcript, errors = build_transcript(data, project_dir, source_file, provider, model, language, device, compute_type)
    metadata: JsonObject | None = None
    if not errors and transcript is not None:
        metadata, errors = write_transcript_artifact(
            data,
            output_file,
            source_path,
            provider,
            model,
            language,
            device,
            compute_type,
            transcript,
        )

    result: TranscribeResult = {
        "project_dir": str(project_dir),
        "source_path": source_path,
        "output_path": str(output_file),
        "provider": provider,
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "success": not errors and metadata is not None,
        "metadata": metadata,
        "errors": errors,
    }

    if result["success"] and metadata is not None:
        _record_transcribe_success(project_dir, data, metadata)
    else:
        _record_transcribe_failure(project_dir, data, source_path, output_path, provider, model, device, compute_type, errors)
    return result


def build_transcript(
    data: ProjectState,
    project_dir: Path,
    source_file: Path,
    provider: str,
    model: str,
    language: str | None,
    device: str,
    compute_type: str,
) -> tuple[TranscriptData | None, list[str]]:
    errors = validate_transcription_prerequisites(data, project_dir, source_file)
    if errors:
        return None, errors

    duration = _audio_duration(data)
    if provider == "mock":
        return build_mock_transcript(project_dir, data["project"]["script"], duration), []
    if provider == "faster-whisper":
        return build_faster_whisper_transcript(source_file, model, language, device, compute_type)
    return None, [f"Unsupported transcription provider: {provider}"]


def validate_transcription_prerequisites(
    data: ProjectState,
    project_dir: Path,
    source_file: Path,
) -> list[str]:
    errors: list[str] = []
    narration = _narration_audio(data)
    if narration is None:
        errors.append("Missing media.audio.narration metadata. Run extract-audio before transcribe.")
    elif _string_field(narration, "path", "") != f"{AUDIO_DIR}/{NARRATION_AUDIO}":
        errors.append("media.audio.narration points to an unexpected path. Rerun extract-audio.")
    elif not is_current(build_pipeline_freshness(project_dir, data)["audio"]):
        errors.append("Narration audio is stale or unverified. Rerun extract-audio before transcribe.")

    if not source_file.exists():
        errors.append(f"Missing narration audio file: {AUDIO_DIR}/{NARRATION_AUDIO}")
    elif not source_file.is_file():
        errors.append(f"Narration audio path is not a file: {AUDIO_DIR}/{NARRATION_AUDIO}")
    return errors


def build_mock_transcript(project_dir: Path, script_path: str, duration: int | float | None) -> TranscriptData:
    script_file = project_dir / script_path
    text = "Mock transcript."
    if script_file.exists() and script_file.is_file():
        script_text = script_file.read_text(encoding="utf-8").strip()
        if script_text:
            text = " ".join(script_text.split())
    end_time = float(duration) if isinstance(duration, int | float) and not isinstance(duration, bool) else 0.0
    return {
        "text": text,
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": end_time,
                "text": text,
            }
        ],
    }


def build_faster_whisper_transcript(
    source_file: Path,
    model_name: str,
    language: str | None,
    device: str,
    compute_type: str,
) -> tuple[TranscriptData | None, list[str]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except ImportError:
        return None, ["faster-whisper is not installed. Run pip install -r requirements.txt."]

    try:
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, [f"Could not create model cache {MODEL_CACHE_DIR.as_posix()}: {exc}"]

    try:
        model_factory = cast(FasterWhisperModelFactory, WhisperModel)
        whisper_model = model_factory(
            model_name,
            device=device,
            compute_type=_faster_whisper_compute_type(compute_type),
            download_root=MODEL_CACHE_DIR.as_posix(),
        )
        if language is not None and language.strip():
            raw_segments, _info = whisper_model.transcribe(
                str(source_file),
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
                language=language.strip(),
            )
        else:
            raw_segments, _info = whisper_model.transcribe(
                str(source_file),
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
            )
    except Exception as exc:  # noqa: BLE001 - provider errors are user-facing checkpoint failures.
        return None, [f"faster-whisper transcription failed: {exc}"]

    segments = normalize_segments(raw_segments)
    if not segments:
        return None, ["Transcript response does not contain valid timestamped segments."]

    text = " ".join(segment["text"].strip() for segment in segments if segment["text"].strip()).strip()
    if not text:
        return None, ["Transcript response is empty."]
    return {"text": text, "segments": segments}, []


def normalize_transcript_payload(payload: object) -> tuple[TranscriptData | None, list[str]]:
    if not isinstance(payload, dict):
        return None, ["Transcript response must be a JSON object."]
    payload_data = cast(dict[str, object], payload)

    text_raw = payload_data.get("text")
    if not isinstance(text_raw, str) or not text_raw.strip():
        return None, ["Transcript response is empty."]
    text = text_raw.strip()

    segments = normalize_segments(payload_data.get("segments"))
    if not segments:
        return None, ["Transcript response does not contain valid timestamped segments."]
    return {"text": text, "segments": segments}, []


def normalize_segments(raw_segments: object) -> list[TranscriptSegment]:
    if raw_segments is None or isinstance(raw_segments, dict | str | bytes) or not isinstance(raw_segments, Iterable):
        return []
    segments: list[TranscriptSegment] = []
    for index, item in enumerate(raw_segments):
        segment = _segment_from_object(item, index)
        if segment is not None:
            segments.append(segment)
    return segments


def write_transcript_artifact(
    data: ProjectState,
    output_file: Path,
    source_path: str,
    provider: str,
    model: str,
    language: str | None,
    device: str,
    compute_type: str,
    transcript: TranscriptData,
) -> tuple[JsonObject | None, list[str]]:
    duration = _audio_duration(data)
    transcribed_at = utc_now_iso()
    segment_count = len(transcript["segments"])
    word_count = _word_count(transcript["segments"])
    artifact: JsonObject = {
        "schema_version": 1,
        "source": source_path,
        "provider": provider,
        "model": model,
        "language": language,
        "device": device,
        "compute_type": compute_type,
        "duration_seconds": duration,
        "text": transcript["text"],
        "segments": _segments_json(transcript["segments"]),
        "transcribed_at": transcribed_at,
    }

    try:
        atomic_write_json(output_file, artifact)
    except (OSError, TypeError, ValueError) as exc:
        return None, [f"Could not write transcript artifact: {exc}"]

    if not output_file.exists() or not output_file.is_file():
        return None, [f"Transcript provider completed but did not create {TRANSCRIPTS_DIR}/{NARRATION_TRANSCRIPT}"]

    narration = _narration_audio(data)
    source_fingerprint = narration.get("artifact_fingerprint") if narration is not None else None
    metadata: JsonObject = {
        "path": f"{TRANSCRIPTS_DIR}/{NARRATION_TRANSCRIPT}",
        "source": source_path,
        "source_fingerprint": source_fingerprint,
        "artifact_fingerprint": sha256_fingerprint(output_file),
        "provider": provider,
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "status": "transcribed",
        "duration_seconds": duration,
        "text_length": len(transcript["text"]),
        "segments": segment_count,
        "word_count": word_count,
        "transcribed_at": transcribed_at,
    }
    return metadata, []


def format_transcribe_result(result: TranscribeResult) -> str:
    status = "transcribed" if result["success"] else "failed"
    lines = [
        f"Transcript: {result['output_path']}",
        f"Source: {result['source_path']}",
        f"Provider: {result['provider']}",
        f"Model: {result['model']}",
        f"Device: {result['device']}",
        f"Compute type: {result['compute_type']}",
        f"Status: {status}",
    ]
    metadata = result["metadata"]
    if result["success"] and metadata is not None:
        lines.append(f"Segments: {metadata.get('segments')}")
        lines.append(f"Words: {metadata.get('word_count')}")
        lines.append(f"Text length: {metadata.get('text_length')}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _record_transcribe_success(project_dir: Path, data: ProjectState, metadata: JsonObject) -> None:
    transcript = _ensure_transcript(data)
    transcript["narration"] = metadata
    resolve_matching_failures(data, stage=TRANSCRIBE_STAGE, scope=TRANSCRIBE_SCOPE)
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _record_transcribe_failure(
    project_dir: Path,
    data: ProjectState,
    source_path: str,
    output_path: str,
    provider: str,
    model: str,
    device: str,
    compute_type: str,
    errors: list[str],
) -> None:
    context: JsonObject = {
        "source_path": source_path,
        "output_path": output_path,
        "provider": provider,
        "model": model,
        "device": device,
        "compute_type": compute_type,
    }
    record_failure(
        data,
        stage=TRANSCRIBE_STAGE,
        scope=TRANSCRIBE_SCOPE,
        errors=errors,
        recommended_next_action=TRANSCRIBE_RECOMMENDED_NEXT_ACTION,
        context=context,
    )
    data["project"]["updated_at"] = utc_now_iso()
    write_project(project_file_for(project_dir), data)


def _segment_from_object(item: object, index: int) -> TranscriptSegment | None:
    if isinstance(item, dict):
        item_data = cast(dict[str, object], item)
        start = _number_value(item_data.get("start"))
        end = _number_value(item_data.get("end"))
        segment_text = item_data.get("text")
        segment_id_value = item_data.get("id")
        raw_words = item_data.get("words")
    else:
        start = _number_value(getattr(item, "start", None))
        end = _number_value(getattr(item, "end", None))
        segment_text = getattr(item, "text", None)
        segment_id_value = getattr(item, "id", index)
        raw_words = getattr(item, "words", None)

    if start is None or end is None or end < start or not isinstance(segment_text, str):
        return None
    segment_id = segment_id_value if isinstance(segment_id_value, int) and not isinstance(segment_id_value, bool) else index
    segment: TranscriptSegment = {
        "id": segment_id,
        "start": float(start),
        "end": float(end),
        "text": segment_text.strip(),
    }
    words = _words_from_object(raw_words)
    if words:
        segment["words"] = words
    return segment


def _words_from_object(raw_words: object) -> list[TranscriptWord]:
    if raw_words is None or isinstance(raw_words, dict | str | bytes) or not isinstance(raw_words, Iterable):
        return []
    words: list[TranscriptWord] = []
    for item in raw_words:
        word = _word_from_object(item)
        if word is not None:
            words.append(word)
    return words


def _word_from_object(item: object) -> TranscriptWord | None:
    if isinstance(item, dict):
        item_data = cast(dict[str, object], item)
        start = _number_value(item_data.get("start"))
        end = _number_value(item_data.get("end"))
        word_text = item_data.get("word")
        probability = _number_value(item_data.get("probability"))
    else:
        start = _number_value(getattr(item, "start", None))
        end = _number_value(getattr(item, "end", None))
        word_text = getattr(item, "word", None)
        probability = _number_value(getattr(item, "probability", None))

    if start is None or end is None or end < start or not isinstance(word_text, str):
        return None
    probability_value = float(probability) if probability is not None else None
    return {
        "start": float(start),
        "end": float(end),
        "word": word_text,
        "probability": probability_value,
    }


def _ensure_transcript(data: ProjectState) -> JsonObject:
    transcript = data.get("transcript")
    if transcript is None:
        transcript = {}
        data["transcript"] = transcript
    return transcript


def _narration_audio(data: ProjectState) -> JsonObject | None:
    media = data.get("media")
    if not isinstance(media, dict):
        return None
    audio = media.get("audio")
    if not isinstance(audio, dict):
        return None
    narration = audio.get("narration")
    if isinstance(narration, dict):
        return cast(JsonObject, narration)
    return None


def _audio_duration(data: ProjectState) -> int | float | None:
    narration = _narration_audio(data)
    if narration is None:
        return None
    return _number_field(narration, "duration_seconds")


def _faster_whisper_compute_type(compute_type: str) -> str:
    return "default" if compute_type == "auto" else compute_type


def _segments_json(segments: list[TranscriptSegment]) -> list[JsonValue]:
    items: list[JsonValue] = []
    for segment in segments:
        item: JsonObject = {
            "id": segment["id"],
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["text"],
        }
        words = segment.get("words")
        if words:
            item["words"] = _words_json(words)
        items.append(item)
    return items


def _words_json(words: list[TranscriptWord]) -> list[JsonValue]:
    items: list[JsonValue] = []
    for word in words:
        items.append(
            {
                "start": word["start"],
                "end": word["end"],
                "word": word["word"],
                "probability": word["probability"],
            }
        )
    return items


def _word_count(segments: list[TranscriptSegment]) -> int:
    total = 0
    for segment in segments:
        total += len(segment.get("words", []))
    return total


def _string_field(data: JsonObject, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _number_field(data: JsonObject, key: str) -> int | float | None:
    return _number_value(data.get(key))


def _number_value(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None
