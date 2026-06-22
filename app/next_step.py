"""Read-only next-action recommendations for Visual Forge projects."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from .artifacts import REASON_ARTIFACT_MISSING, FreshnessResult, is_current
from .project import JsonObject, JsonValue, ProjectState, load_project
from .render_freshness import build_chunk_render_freshness_map
from .status import StatusSummary, build_status
from .timeline import chunk_visual_mode
from .visual_quality import build_visual_plan_review


ActionKind = Literal["command", "human", "none"]


class NextStepSummary(TypedDict):
    project_dir: str
    state: str
    human_input_required: bool
    action_kind: ActionKind
    recommended_action: str
    recommended_command: list[str]
    reason: str
    chunk_id: NotRequired[str]


def build_next_step(project_dir: Path) -> NextStepSummary:
    status = build_status(project_dir)
    data = load_project(project_dir)
    state = status["state"]

    if state == "failed":
        return _command(project_dir, status, "Inspect active failures.", _cli("failures", str(project_dir), "--json"))
    if state == "missing_inputs":
        missing = ", ".join(status["missing_inputs"])
        return _human(project_dir, status, f"Add missing input files: {missing}.")
    if state == "ready_for_probe":
        return _command(project_dir, status, "Probe raw media.", _cli("probe", str(project_dir), "--json"))
    if state == "ready_for_audio":
        return _command(project_dir, status, "Extract narration audio.", _cli("extract-audio", str(project_dir), "--json"))
    if state == "ready_for_transcription":
        return _command(project_dir, status, "Transcribe narration audio.", _cli("transcribe", str(project_dir), "--json"))
    if state == "ready_for_alignment":
        return _command(project_dir, status, "Align the script to the transcript.", _cli("align", str(project_dir), "--json"))
    if state == "ready_for_chunks":
        return _command(project_dir, status, "Create resumable chunks.", _cli("create-chunks", str(project_dir), "--json"))
    if state == "ready_for_final":
        return _command(project_dir, status, "Compose the final video.", _cli("final", str(project_dir), "--json"))
    if state == "ready_for_verification":
        return _command(project_dir, status, "Verify the final video.", _cli("verify-final", str(project_dir), "--json"))
    if state == "complete":
        return _none(project_dir, status, "Review final video.")
    if state == "in_progress":
        return _in_progress_next(project_dir, status, data)

    return _human(project_dir, status, status["next_action"])


def format_next_step(summary: NextStepSummary) -> str:
    lines = [
        f"Project: {summary['project_dir']}",
        f"State: {summary['state']}",
        f"Recommended action: {summary['recommended_action']}",
    ]
    chunk_id = summary.get("chunk_id")
    if chunk_id:
        lines.append(f"Chunk: {chunk_id}")
    if summary["recommended_command"]:
        lines.append("Command: " + " ".join(summary["recommended_command"]))
    else:
        lines.append("Command: none")
    lines.append(f"Human input required: {'yes' if summary['human_input_required'] else 'no'}")
    lines.append(f"Reason: {summary['reason']}")
    return "\n".join(lines)


def _in_progress_next(project_dir: Path, status: StatusSummary, data: ProjectState) -> NextStepSummary:
    render_freshness = build_chunk_render_freshness_map(project_dir, data)
    chunks = sorted(data["chunks"], key=_chunk_sort_key)
    for chunk in chunks:
        chunk_id = _string_field(chunk, "id")
        if chunk_id is None:
            continue
        chunk_status = _string_field(chunk, "status") or "unknown"
        visual_mode = chunk_visual_mode(chunk, data.get("visuals", []))
        if chunk_status == "new":
            if visual_mode == "visuals":
                if _has_codex_intents(data, chunk_id):
                    review = build_visual_plan_review(project_dir, chunk_id)
                    if not review["passed"]:
                        return _chunk_command(
                            project_dir,
                            status,
                            chunk_id,
                            "Improve the dense animated visual plan before preview.",
                            _cli("planning-context", str(project_dir), "--chunk", chunk_id, "--json"),
                        )
                return _chunk_command(
                    project_dir,
                    status,
                    chunk_id,
                    "Preview planned visuals for the chunk.",
                    _cli("preview", str(project_dir), "--chunk", chunk_id, "--json"),
                )
            intent_statuses = _chunk_intent_statuses(data, chunk_id)
            if intent_statuses.get("capability_gap", 0):
                gap_types = _chunk_gap_types(data, chunk_id)
                detail = ", ".join(gap_types) if gap_types else "unknown"
                return _chunk_human(
                    project_dir,
                    status,
                    chunk_id,
                    f"Create missing visual capabilities before rendering: {detail}.",
                )
            if intent_statuses.get("unbound", 0):
                return _chunk_command(
                    project_dir,
                    status,
                    chunk_id,
                    "Inspect candidate templates and bind the visual intents.",
                    _cli("visual-intents", str(project_dir), "--chunk", chunk_id, "--json"),
                )
            if _visual_planning_no_candidates(chunk):
                return _chunk_human(
                    project_dir,
                    status,
                    chunk_id,
                    "Add a manual visual for the chunk or approve it as camera-only.",
                )
            return _chunk_command(
                project_dir,
                status,
                chunk_id,
                "Prepare compact context for Codex visual planning.",
                _cli("planning-context", str(project_dir), "--chunk", chunk_id, "--json"),
            )
        if chunk_status == "previewed":
            return _chunk_command(
                project_dir,
                status,
                chunk_id,
                "Render the previewed chunk.",
                _cli("render-chunk", str(project_dir), chunk_id, "--json"),
            )
        if chunk_status == "rendered" and not is_current(render_freshness.get(chunk_id, _missing_render())):
            if visual_mode == "visuals":
                freshness = render_freshness.get(chunk_id, _missing_render())
                if freshness["reason"] == REASON_ARTIFACT_MISSING:
                    return _chunk_command(
                        project_dir,
                        status,
                        chunk_id,
                        "Render the chunk again because the rendered MP4 is missing.",
                        _cli("render-chunk", str(project_dir), chunk_id, "--json"),
                    )
                return _chunk_command(
                    project_dir,
                    status,
                    chunk_id,
                    "Refresh the chunk storyboard before re-rendering.",
                    _cli("preview", str(project_dir), "--chunk", chunk_id, "--json"),
                )
            return _chunk_command(
                project_dir,
                status,
                chunk_id,
                "Render the camera-only chunk again.",
                _cli("render-chunk", str(project_dir), chunk_id, "--json"),
            )
    return _human(project_dir, status, status["next_action"])


def _command(project_dir: Path, status: StatusSummary, action: str, command: list[str]) -> NextStepSummary:
    return {
        "project_dir": str(project_dir),
        "state": status["state"],
        "human_input_required": False,
        "action_kind": "command",
        "recommended_action": action,
        "recommended_command": command,
        "reason": status["next_action"],
    }


def _human(project_dir: Path, status: StatusSummary, action: str) -> NextStepSummary:
    return {
        "project_dir": str(project_dir),
        "state": status["state"],
        "human_input_required": True,
        "action_kind": "human",
        "recommended_action": action,
        "recommended_command": [],
        "reason": status["next_action"],
    }


def _none(project_dir: Path, status: StatusSummary, action: str) -> NextStepSummary:
    return {
        "project_dir": str(project_dir),
        "state": status["state"],
        "human_input_required": True,
        "action_kind": "none",
        "recommended_action": action,
        "recommended_command": [],
        "reason": status["next_action"],
    }


def _chunk_command(
    project_dir: Path,
    status: StatusSummary,
    chunk_id: str,
    action: str,
    command: list[str],
) -> NextStepSummary:
    summary = _command(project_dir, status, action, command)
    summary["chunk_id"] = chunk_id
    return summary


def _chunk_human(project_dir: Path, status: StatusSummary, chunk_id: str, action: str) -> NextStepSummary:
    summary = _human(project_dir, status, action)
    summary["chunk_id"] = chunk_id
    return summary


def _cli(*args: str) -> list[str]:
    return ["python", "-m", "app.main", *args]


def _missing_render() -> FreshnessResult:
    return {"state": "missing", "reason": REASON_ARTIFACT_MISSING}


def _visual_planning_no_candidates(chunk: JsonObject) -> bool:
    planning = chunk.get("visual_planning")
    if not isinstance(planning, dict):
        return False
    return planning.get("planner") == "auto_v0" and planning.get("status") == "no_candidates"


def _chunk_intent_statuses(data: ProjectState, chunk_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for intent in data.get("visual_intents", []):
        if intent.get("chunk_id") != chunk_id:
            continue
        status = _string_field(intent, "status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _has_codex_intents(data: ProjectState, chunk_id: str) -> bool:
    return any(
        intent.get("chunk_id") == chunk_id and intent.get("planner") == "codex_v1"
        for intent in data.get("visual_intents", [])
    )


def _chunk_gap_types(data: ProjectState, chunk_id: str) -> list[str]:
    gap_types = {
        intent_type
        for intent in data.get("visual_intents", [])
        if intent.get("chunk_id") == chunk_id
        and intent.get("status") == "capability_gap"
        and (intent_type := _string_field(intent, "intent_type")) is not None
    }
    return sorted(gap_types)


def _chunk_sort_key(chunk: JsonObject) -> tuple[float, float, str]:
    start = _number_field(chunk, "start")
    end = _number_field(chunk, "end")
    return (
        start if start is not None else float("inf"),
        end if end is not None else float("inf"),
        _string_field(chunk, "id") or "",
    )


def _string_field(data: JsonObject, key: str) -> str | None:
    value: JsonValue | None = data.get(key)
    return value if isinstance(value, str) and value else None


def _number_field(data: JsonObject, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None
