"""Project state creation, loading, and validation."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypeAlias, TypedDict, cast


SCHEMA_VERSION = 1
PROJECT_FILENAME = "project.json"
DEFAULT_SCRIPT = "script.txt"
DEFAULT_VIDEO = "raw.mp4"
DEFAULT_FINAL = "final.mp4"
LAYOUT_VERSION = 1
LAYOUT_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class ProjectInfo(TypedDict):
    name: str
    script: str
    video: str
    final: str
    created_at: str
    updated_at: str


class ProjectState(TypedDict):
    schema_version: int
    project: ProjectInfo
    chunks: list[JsonObject]
    corrections: list[JsonObject]
    cache: JsonObject
    failures: list[JsonObject]
    media: NotRequired[JsonObject]
    transcript: NotRequired[JsonObject]
    alignment: NotRequired[JsonObject]
    previews: NotRequired[list[JsonObject]]
    chunk_previews: NotRequired[list[JsonObject]]
    visual_intents: NotRequired[list[JsonObject]]
    visuals: NotRequired[list[JsonObject]]
    renders: NotRequired[JsonObject]
    verification: NotRequired[JsonObject]
    layout: NotRequired[JsonObject]
    timeline: NotRequired[JsonObject]
    chunking: NotRequired[JsonObject]


class ProjectError(Exception):
    """Raised when a project cannot be loaded or validated."""


@dataclass(frozen=True)
class InitResult:
    project_dir: Path
    project_file: Path
    data: ProjectState
    created: bool


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def project_file_for(project_dir: Path) -> Path:
    return project_dir / PROJECT_FILENAME


def build_initial_project(
    project_dir: Path,
    *,
    script_source: Path | None = None,
    video_source: Path | None = None,
    layout: JsonObject | None = None,
) -> ProjectState:
    now = utc_now_iso()
    script_path = path_reference(project_dir, script_source) if script_source is not None else DEFAULT_SCRIPT
    video_path = path_reference(project_dir, video_source) if video_source is not None else DEFAULT_VIDEO
    state: ProjectState = {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "name": project_dir.name,
            "script": script_path,
            "video": video_path,
            "final": DEFAULT_FINAL,
            "created_at": now,
            "updated_at": now,
        },
        "chunks": [],
        "corrections": [],
        "cache": {},
        "failures": [],
        "media": {},
        "transcript": {},
        "alignment": {},
        "previews": [],
        "chunk_previews": [],
        "visual_intents": [],
        "visuals": [],
        "renders": {},
        "verification": {},
    }
    if layout is not None:
        state["layout"] = layout
    return state


def init_project(
    project_dir: Path,
    *,
    script_source: Path | None = None,
    video_source: Path | None = None,
    layout: JsonObject | None = None,
) -> InitResult:
    _validate_input_source(script_source, "script")
    _validate_input_source(video_source, "video")
    project_dir.mkdir(parents=True, exist_ok=True)
    project_file = project_file_for(project_dir)

    if project_file.exists():
        data = load_project(project_dir)
        _validate_existing_input_reference(project_dir, data, "script", script_source)
        _validate_existing_input_reference(project_dir, data, "video", video_source)
        if layout is not None:
            changed = _merge_existing_layout(data, layout)
            if changed:
                data["project"]["updated_at"] = utc_now_iso()
                write_project(project_file, data)
        return InitResult(project_dir=project_dir, project_file=project_file, data=data, created=False)

    data = build_initial_project(
        project_dir,
        script_source=script_source,
        video_source=video_source,
        layout=layout,
    )
    write_project(project_file, data)
    return InitResult(project_dir=project_dir, project_file=project_file, data=data, created=True)


def load_project(project_dir: Path) -> ProjectState:
    project_file = project_file_for(project_dir)
    if not project_file.exists():
        raise ProjectError(f"Missing {PROJECT_FILENAME} in {project_dir}")

    try:
        with project_file.open("r", encoding="utf-8") as handle:
            data: object = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ProjectError(f"Invalid JSON in {project_file}: {exc.msg}") from exc
    except OSError as exc:
        raise ProjectError(f"Could not read {project_file}: {exc}") from exc

    return validate_project(data, project_file)


def write_project(project_file: Path, data: ProjectState) -> None:
    temporary_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=project_file.parent,
            prefix=f".{project_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_file = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_file, project_file)
    except (OSError, TypeError, ValueError) as exc:
        if temporary_file is not None:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProjectError(f"Could not write {project_file}: {exc}") from exc


def validate_project(data: object, project_file: Path | None = None) -> ProjectState:
    location = f" in {project_file}" if project_file else ""
    if not isinstance(data, dict):
        raise ProjectError(f"Project state{location} must be a JSON object")
    project_state = cast(dict[str, object], data)

    schema_version = project_state.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ProjectError(
            f"Unsupported schema_version{location}: expected {SCHEMA_VERSION}, got {schema_version!r}"
        )

    project_raw = project_state.get("project")
    if not isinstance(project_raw, dict):
        raise ProjectError(f"Project state{location} must contain a project object")
    project = cast(dict[str, object], project_raw)

    for key in ("name", "script", "video", "final", "created_at", "updated_at"):
        value = project.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProjectError(f"Project field {key!r}{location} must be a non-empty string")

    _require_list(project_state, "chunks", location)
    _require_list(project_state, "corrections", location)
    _require_list(project_state, "failures", location)
    if "previews" in project_state:
        _require_list(project_state, "previews", location)
    if "chunk_previews" in project_state:
        _require_list(project_state, "chunk_previews", location)
    if "visual_intents" in project_state:
        _require_list(project_state, "visual_intents", location)
    if "visuals" in project_state:
        _require_list(project_state, "visuals", location)

    if not isinstance(project_state.get("cache"), dict):
        raise ProjectError(f"Project field 'cache'{location} must be an object")
    if "media" in project_state and not isinstance(project_state.get("media"), dict):
        raise ProjectError(f"Project field 'media'{location} must be an object")
    if "transcript" in project_state and not isinstance(project_state.get("transcript"), dict):
        raise ProjectError(f"Project field 'transcript'{location} must be an object")
    if "alignment" in project_state and not isinstance(project_state.get("alignment"), dict):
        raise ProjectError(f"Project field 'alignment'{location} must be an object")
    if "renders" in project_state and not isinstance(project_state.get("renders"), dict):
        raise ProjectError(f"Project field 'renders'{location} must be an object")
    if "verification" in project_state and not isinstance(project_state.get("verification"), dict):
        raise ProjectError(f"Project field 'verification'{location} must be an object")
    if "timeline" in project_state and not isinstance(project_state.get("timeline"), dict):
        raise ProjectError(f"Project field 'timeline'{location} must be an object")
    if "chunking" in project_state and not isinstance(project_state.get("chunking"), dict):
        raise ProjectError(f"Project field 'chunking'{location} must be an object")
    if "layout" in project_state:
        _validate_layout(project_state.get("layout"), location)

    return cast(ProjectState, data)


def _require_list(data: dict[str, object], key: str, location: str) -> None:
    if not isinstance(data.get(key), list):
        raise ProjectError(f"Project field {key!r}{location} must be a list")


def _validate_input_source(source: Path | None, label: str) -> None:
    if source is None:
        return
    if not source.exists():
        raise ProjectError(f"Input {label} file does not exist: {source}")
    if not source.is_file():
        raise ProjectError(f"Input {label} path is not a file: {source}")


def path_reference(base_dir: Path, target: Path) -> str:
    target_resolved = target.resolve()
    base_resolved = base_dir.resolve()
    try:
        relative = os.path.relpath(target_resolved, base_resolved)
    except ValueError:
        return str(target_resolved)
    return Path(relative).as_posix()


def _validate_existing_input_reference(
    project_dir: Path,
    data: ProjectState,
    field: str,
    source: Path | None,
) -> None:
    if source is None:
        return
    current = (project_dir / data["project"][field]).resolve()
    requested = source.resolve()
    if current != requested:
        raise ProjectError(
            f"Project is already initialized with {field} input {data['project'][field]!r}; "
            "initialize a new project to use a different input file"
        )


def _merge_existing_layout(data: ProjectState, layout: JsonObject) -> bool:
    existing = data.get("layout")
    if existing is None:
        data["layout"] = layout
        return True
    if existing != layout:
        raise ProjectError(
            "Project is already initialized with different layout metadata; "
            "create a new project or run adopt-layout intentionally"
        )
    return False


def _validate_layout(layout: object, location: str) -> None:
    if not isinstance(layout, dict):
        raise ProjectError(f"Project field 'layout'{location} must be an object")
    data = cast(dict[str, object], layout)
    version = data.get("version")
    if version != LAYOUT_VERSION:
        raise ProjectError(f"Project layout version{location} must be {LAYOUT_VERSION}")
    slug = data.get("slug")
    if not isinstance(slug, str) or not LAYOUT_SLUG_PATTERN.fullmatch(slug):
        raise ProjectError(f"Project layout slug{location} must be lowercase kebab-case")
    for key in ("inputs_root", "outputs_root"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProjectError(f"Project layout field {key!r}{location} must be a non-empty string")
