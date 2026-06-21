"""Project slug, layout, and artifact path helpers."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from .project import JsonObject, ProjectError, ProjectState, path_reference, project_file_for, utc_now_iso, write_project


LAYOUT_VERSION = 1
PROJECTS_DIR = "projects"
INPUTS_DIR = "inputs"
OUTPUTS_DIR = "outputs"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
GENERATED_DIRS = ("audio", "transcripts", "alignment", "previews", "renders", "cache", "generated")


class InputSelection(TypedDict):
    script: Path
    video: Path


class InitFromInputResult(TypedDict):
    project_dir: str
    input_dir: str
    outputs_dir: str
    slug: str
    created: bool
    project_json: str
    script: str
    video: str


class AdoptLayoutResult(TypedDict):
    project_dir: str
    input_dir: str
    outputs_dir: str
    slug: str
    success: bool
    moved: list[str]
    errors: list[str]


@dataclass(frozen=True)
class LayoutPaths:
    slug: str
    input_dir: Path
    project_dir: Path
    outputs_dir: Path


def normalize_slug(value: str) -> str:
    slug = NON_SLUG_CHARS.sub("-", value.casefold()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.fullmatch(slug):
        raise ProjectError(
            f"Invalid project slug {slug!r}; use lowercase kebab-case such as 'my-video'"
        )
    return slug


def slug_from_input_dir(input_dir: Path, explicit_slug: str | None = None) -> str:
    raw_slug = explicit_slug if explicit_slug is not None else normalize_slug(input_dir.name)
    return validate_slug(raw_slug)


def repo_root_from_input_dir(input_dir: Path) -> Path:
    resolved = input_dir.resolve()
    if resolved.parent.name.casefold() == INPUTS_DIR:
        return resolved.parent.parent
    return Path.cwd().resolve()


def repo_root_from_project_dir(project_dir: Path) -> Path:
    resolved = project_dir.resolve()
    if resolved.parent.name.casefold() == PROJECTS_DIR:
        return resolved.parent.parent
    return Path.cwd().resolve()


def layout_paths_for_input(input_dir: Path, explicit_slug: str | None = None) -> LayoutPaths:
    slug = slug_from_input_dir(input_dir, explicit_slug)
    repo_root = repo_root_from_input_dir(input_dir)
    return LayoutPaths(
        slug=slug,
        input_dir=input_dir.resolve(),
        project_dir=repo_root / PROJECTS_DIR / slug,
        outputs_dir=repo_root / OUTPUTS_DIR / slug,
    )


def layout_metadata(project_dir: Path, *, slug: str, input_dir: Path, outputs_dir: Path) -> JsonObject:
    return {
        "version": LAYOUT_VERSION,
        "slug": slug,
        "inputs_root": path_reference(project_dir, input_dir),
        "outputs_root": path_reference(project_dir, outputs_dir),
    }


def layout_from_project(data: ProjectState) -> JsonObject | None:
    layout = data.get("layout")
    if isinstance(layout, dict):
        return cast(JsonObject, layout)
    return None


def artifact_root(project_dir: Path, data: ProjectState) -> Path:
    layout = layout_from_project(data)
    if layout is None:
        return project_dir
    outputs_root = layout.get("outputs_root")
    if not isinstance(outputs_root, str) or not outputs_root:
        return project_dir
    return (project_dir / outputs_root).resolve()


def artifact_path(project_dir: Path, data: ProjectState, relative_path: str | Path) -> Path:
    return artifact_root(project_dir, data) / relative_path


def input_root(project_dir: Path, data: ProjectState) -> Path | None:
    layout = layout_from_project(data)
    if layout is None:
        return None
    inputs_root = layout.get("inputs_root")
    if not isinstance(inputs_root, str) or not inputs_root:
        return None
    return project_dir / inputs_root


def discover_input_files(
    input_dir: Path,
    *,
    script_source: Path | None = None,
    video_source: Path | None = None,
) -> InputSelection:
    if not input_dir.exists():
        raise ProjectError(f"Input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise ProjectError(f"Input path is not a directory: {input_dir}")
    script = script_source.resolve() if script_source is not None else _discover_script(input_dir)
    video = video_source.resolve() if video_source is not None else _discover_video(input_dir)
    _validate_file(script, "script")
    _validate_file(video, "video")
    return {"script": script, "video": video}


def adopt_layout(
    project_dir: Path,
    *,
    input_dir: Path,
    explicit_slug: str | None = None,
) -> AdoptLayoutResult:
    from .project import load_project

    if not input_dir.exists():
        raise ProjectError(f"Input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise ProjectError(f"Input path is not a directory: {input_dir}")

    data = load_project(project_dir)
    slug = validate_slug(explicit_slug) if explicit_slug is not None else validate_slug(normalize_slug(data["project"]["name"]))
    repo_root = repo_root_from_project_dir(project_dir)
    outputs_dir = repo_root / OUTPUTS_DIR / slug
    metadata = layout_metadata(project_dir, slug=slug, input_dir=input_dir.resolve(), outputs_dir=outputs_dir)

    errors = _layout_conflicts(project_dir, outputs_dir)
    result: AdoptLayoutResult = {
        "project_dir": str(project_dir),
        "input_dir": str(input_dir),
        "outputs_dir": str(outputs_dir),
        "slug": slug,
        "success": not errors,
        "moved": [],
        "errors": errors,
    }
    if errors:
        return result

    outputs_dir.mkdir(parents=True, exist_ok=True)
    moved_pairs: list[tuple[Path, Path]] = []
    try:
        for name in GENERATED_DIRS:
            source = project_dir / name
            if not source.exists():
                continue
            destination = outputs_dir / name
            if destination.exists():
                destination.rmdir()
            shutil.move(str(source), str(destination))
            moved_pairs.append((source, destination))
            result["moved"].append(name)

        data["layout"] = metadata
        data["project"]["updated_at"] = utc_now_iso()
        write_project(project_file_for(project_dir), data)
    except (OSError, ProjectError) as exc:
        _rollback_moves(moved_pairs)
        result["success"] = False
        result["errors"] = [f"Could not adopt layout: {exc}"]
    return result


def format_init_from_input_result(result: InitFromInputResult) -> str:
    return "\n".join(
        [
            f"Project: {result['slug']}",
            f"Project dir: {result['project_dir']}",
            f"Inputs: {result['input_dir']}",
            f"Outputs: {result['outputs_dir']}",
            f"Script input: {result['script']}",
            f"Video input: {result['video']}",
            f"Status: {'initialized' if result['created'] else 'already initialized'}",
        ]
    )


def format_adopt_layout_result(result: AdoptLayoutResult) -> str:
    lines = [
        f"Project: {result['slug']}",
        f"Project dir: {result['project_dir']}",
        f"Inputs: {result['input_dir']}",
        f"Outputs: {result['outputs_dir']}",
        f"Status: {'adopted' if result['success'] else 'failed'}",
    ]
    if result["moved"]:
        lines.append("Moved: " + ", ".join(result["moved"]))
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _discover_script(input_dir: Path) -> Path:
    canonical = input_dir / "script.txt"
    if canonical.is_file():
        return canonical.resolve()
    candidates = sorted(path for path in input_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".txt")
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise ProjectError(f"Input directory has no script.txt or single .txt file: {input_dir}")
    raise ProjectError("Input directory has multiple .txt scripts; pass --script explicitly.")


def _discover_video(input_dir: Path) -> Path:
    canonical = input_dir / "raw.mp4"
    if canonical.is_file():
        return canonical.resolve()
    candidates = sorted(
        path for path in input_dir.iterdir() if path.is_file() and path.suffix.casefold() in SUPPORTED_VIDEO_EXTENSIONS
    )
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise ProjectError(f"Input directory has no raw.mp4 or single supported video file: {input_dir}")
    raise ProjectError("Input directory has multiple video files; pass --video explicitly.")


def _validate_file(path: Path, label: str) -> None:
    if not path.exists():
        raise ProjectError(f"Input {label} file does not exist: {path}")
    if not path.is_file():
        raise ProjectError(f"Input {label} path is not a file: {path}")


def _layout_conflicts(project_dir: Path, outputs_dir: Path) -> list[str]:
    errors: list[str] = []
    for name in GENERATED_DIRS:
        source = project_dir / name
        destination = outputs_dir / name
        if not source.exists():
            continue
        if destination.exists() and _directory_has_entries(destination):
            errors.append(f"Destination already contains generated artifacts: {destination}")
        elif destination.exists() and not destination.is_dir():
            errors.append(f"Destination exists and is not a directory: {destination}")
    return errors


def _directory_has_entries(path: Path) -> bool:
    if not path.is_dir():
        return True
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    except OSError:
        return True
    return True


def _rollback_moves(moved_pairs: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved_pairs):
        if source.exists() or not destination.exists():
            continue
        try:
            shutil.move(str(destination), str(source))
        except OSError:
            pass
