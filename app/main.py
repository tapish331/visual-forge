"""Command-line interface for Visual Forge."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .align import align_project, build_alignment_review, format_alignment_result, format_alignment_review
from .audio import extract_project_audio, format_audio_extract_result
from .chunks import DEFAULT_MAX_SECONDS, DEFAULT_MIN_SECONDS, DEFAULT_TARGET_SECONDS
from .chunks import approve_camera_only, build_chunks_summary, create_chunks
from .chunks import format_camera_only_result, format_chunks_summary, format_create_chunks_result
from .compose import compose_project_final, format_final_compose_result
from .env import load_dotenv
from .failures import (
    build_failures_summary,
    format_failures_summary,
    format_resolve_failure_result,
    resolve_failure_by_id,
)
from .logging_utils import LogSession, capture_console, create_log_session, run_external_command
from .layout import (
    adopt_layout,
    discover_input_files,
    format_adopt_layout_result,
    InitFromInputResult,
    format_init_from_input_result,
    layout_metadata,
    layout_paths_for_input,
)
from .media_probe import format_media_probe_result, probe_project_media
from .next_step import build_next_step, format_next_step
from .preview import format_chunk_preview_result, format_preview_result, format_preview_visual_result
from .preview import render_project_preview_for_chunk, render_project_preview_for_visual, render_project_preview_from_json
from .project import ProjectError, init_project, project_file_for
from .project_lock import ProjectBusyError, ProjectMutationLock, format_project_busy_error
from .render import format_render_chunk_result, render_project_chunk
from .render_template import format_render_template_result, render_template_from_json
from .status import build_status, format_status
from .templates import (
    TemplateError,
    build_inventory,
    format_inventory,
    format_template_validation,
    validate_template_file,
)
from .transcribe import DEFAULT_TRANSCRIBE_COMPUTE_TYPE, DEFAULT_TRANSCRIBE_DEVICE, DEFAULT_TRANSCRIBE_MODEL
from .transcribe import DEFAULT_TRANSCRIBE_PROVIDER
from .transcribe import format_transcribe_result, transcribe_project
from .verify import format_verify_final_result, verify_project_final
from .visual_intents import (
    apply_visual_plan_from_json,
    build_planning_context,
    build_visual_intents_summary,
    format_apply_visual_plan_result,
    format_planning_context,
    format_visual_intents_summary,
)
from .visual_planner import DEFAULT_MAX_VISUALS, format_plan_visuals_result, plan_visuals_for_chunk
from .visuals import add_visual_from_json, build_visuals_summary, format_add_visual_result
from .visuals import format_update_visual_result, format_visuals_summary, update_visual_from_json


CommandHandler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="visual-forge", description="Visual Forge project tools")
    parser.add_argument(
        "--log-only",
        action="store_true",
        help="Write command output to the rotating log instead of the terminal",
    )
    parser.set_defaults(mutates_project=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize a video project")
    init_parser.add_argument("project_dir", type=Path)
    init_parser.add_argument("--script", dest="script_source", type=Path, help="Path to the raw narration script")
    init_parser.add_argument("--video", dest="video_source", type=Path, help="Path to the raw narration video")
    init_parser.set_defaults(handler=handle_init, mutates_project=True)

    init_from_input_parser = subparsers.add_parser(
        "init-from-input",
        help="Initialize a project from inputs/<project-slug>",
    )
    init_from_input_parser.add_argument("input_dir", type=Path)
    init_from_input_parser.add_argument("--slug", help="Explicit lowercase kebab-case project slug")
    init_from_input_parser.add_argument("--script", dest="script_source", type=Path, help="Explicit script file")
    init_from_input_parser.add_argument("--video", dest="video_source", type=Path, help="Explicit video file")
    init_from_input_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    init_from_input_parser.set_defaults(handler=handle_init_from_input, mutates_project=False)

    adopt_layout_parser = subparsers.add_parser("adopt-layout", help="Adopt the three-root layout for a project")
    adopt_layout_parser.add_argument("project_dir", type=Path)
    adopt_layout_parser.add_argument("--input-dir", required=True, type=Path, help="Existing input directory")
    adopt_layout_parser.add_argument("--slug", help="Explicit lowercase kebab-case project slug")
    adopt_layout_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    adopt_layout_parser.set_defaults(handler=handle_adopt_layout, mutates_project=True)

    status_parser = subparsers.add_parser("status", help="Show project status")
    status_parser.add_argument("project_dir", type=Path)
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status_parser.set_defaults(handler=handle_status)

    next_parser = subparsers.add_parser("next", help="Recommend the next project checkpoint")
    next_parser.add_argument("project_dir", type=Path)
    next_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    next_parser.set_defaults(handler=handle_next)

    probe_parser = subparsers.add_parser("probe", help="Probe raw project media")
    probe_parser.add_argument("project_dir", type=Path)
    probe_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    probe_parser.set_defaults(handler=handle_probe, mutates_project=True)

    extract_audio_parser = subparsers.add_parser("extract-audio", help="Extract narration audio")
    extract_audio_parser.add_argument("project_dir", type=Path)
    extract_audio_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    extract_audio_parser.set_defaults(handler=handle_extract_audio, mutates_project=True)

    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe narration audio")
    transcribe_parser.add_argument("project_dir", type=Path)
    transcribe_parser.add_argument(
        "--provider",
        choices=("faster-whisper", "mock"),
        default=DEFAULT_TRANSCRIBE_PROVIDER,
        help="Transcription provider",
    )
    transcribe_parser.add_argument("--model", default=DEFAULT_TRANSCRIBE_MODEL, help="Transcription model")
    transcribe_parser.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default=DEFAULT_TRANSCRIBE_DEVICE,
        help="Transcription device",
    )
    transcribe_parser.add_argument(
        "--compute-type",
        choices=("auto", "float16", "int8_float16", "int8", "float32"),
        default=DEFAULT_TRANSCRIBE_COMPUTE_TYPE,
        help="Transcription compute type",
    )
    transcribe_parser.add_argument("--language", help="Optional source language hint")
    transcribe_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    transcribe_parser.set_defaults(handler=handle_transcribe, mutates_project=True)

    align_parser = subparsers.add_parser("align", help="Align the narration script to transcript timestamps")
    align_parser.add_argument("project_dir", type=Path)
    align_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    align_parser.set_defaults(handler=handle_align, mutates_project=True)

    alignment_review_parser = subparsers.add_parser(
        "alignment-review",
        help="Inspect alignment warnings and unmatched blocks",
    )
    alignment_review_parser.add_argument("project_dir", type=Path)
    alignment_review_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    alignment_review_parser.set_defaults(handler=handle_alignment_review)

    create_chunks_parser = subparsers.add_parser("create-chunks", help="Create resumable chunks from alignment")
    create_chunks_parser.add_argument("project_dir", type=Path)
    create_chunks_parser.add_argument(
        "--target-seconds",
        type=float,
        default=DEFAULT_TARGET_SECONDS,
        help="Preferred chunk duration in seconds",
    )
    create_chunks_parser.add_argument(
        "--min-seconds",
        type=float,
        default=DEFAULT_MIN_SECONDS,
        help="Minimum preferred chunk duration in seconds",
    )
    create_chunks_parser.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_MAX_SECONDS,
        help="Maximum preferred chunk duration in seconds",
    )
    create_chunks_parser.add_argument("--force", action="store_true", help="Replace existing chunks")
    create_chunks_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    create_chunks_parser.set_defaults(handler=handle_create_chunks, mutates_project=True)

    chunks_parser = subparsers.add_parser("chunks", help="List project chunks")
    chunks_parser.add_argument("project_dir", type=Path)
    chunks_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    chunks_parser.set_defaults(handler=handle_chunks)

    camera_only_parser = subparsers.add_parser(
        "approve-camera-only",
        help="Approve one zero-visual chunk for camera-only rendering",
    )
    camera_only_parser.add_argument("project_dir", type=Path)
    camera_only_parser.add_argument("chunk_id")
    camera_only_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    camera_only_parser.set_defaults(handler=handle_approve_camera_only, mutates_project=True)

    plan_visuals_parser = subparsers.add_parser("plan-visuals", help="Plan visuals for one chunk")
    plan_visuals_parser.add_argument("project_dir", type=Path)
    plan_visuals_parser.add_argument("--chunk", dest="chunk_id", required=True, help="Chunk ID to plan")
    plan_visuals_parser.add_argument(
        "--max-visuals",
        type=int,
        default=DEFAULT_MAX_VISUALS,
        help="Maximum visuals to generate for the chunk",
    )
    plan_visuals_parser.add_argument(
        "--force-generated",
        action="store_true",
        help="Replace existing auto-generated visuals for this chunk",
    )
    plan_visuals_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    plan_visuals_parser.set_defaults(handler=handle_plan_visuals, mutates_project=True)

    planning_context_parser = subparsers.add_parser(
        "planning-context",
        help="Show compact Codex visual-planning context for one chunk",
    )
    planning_context_parser.add_argument("project_dir", type=Path)
    planning_context_parser.add_argument("--chunk", dest="chunk_id", required=True)
    planning_context_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    planning_context_parser.set_defaults(handler=handle_planning_context)

    apply_visual_plan_parser = subparsers.add_parser(
        "apply-visual-plan",
        help="Validate and apply Codex-authored visual intents for one chunk",
    )
    apply_visual_plan_parser.add_argument("project_dir", type=Path)
    apply_visual_plan_parser.add_argument("--chunk", dest="chunk_id", required=True)
    apply_visual_plan_parser.add_argument("--plan-json", required=True, help="Visual intent plan as JSON")
    apply_visual_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    apply_visual_plan_parser.set_defaults(handler=handle_apply_visual_plan, mutates_project=True)

    visual_intents_parser = subparsers.add_parser("visual-intents", help="List project visual intents")
    visual_intents_parser.add_argument("project_dir", type=Path)
    visual_intents_parser.add_argument("--chunk", dest="chunk_id", help="Filter intents by chunk ID")
    visual_intents_parser.add_argument("--gaps-only", action="store_true", help="Show capability gaps only")
    visual_intents_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    visual_intents_parser.set_defaults(handler=handle_visual_intents)

    render_chunk_parser = subparsers.add_parser("render-chunk", help="Render one previewed chunk to MP4")
    render_chunk_parser.add_argument("project_dir", type=Path)
    render_chunk_parser.add_argument("chunk_id")
    render_chunk_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    render_chunk_parser.set_defaults(handler=handle_render_chunk, mutates_project=True)

    final_parser = subparsers.add_parser("final", help="Compose rendered chunks into final.mp4")
    final_parser.add_argument("project_dir", type=Path)
    final_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    final_parser.set_defaults(handler=handle_final, mutates_project=True)

    verify_final_parser = subparsers.add_parser("verify-final", help="Verify final.mp4 output settings")
    verify_final_parser.add_argument("project_dir", type=Path)
    verify_final_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    verify_final_parser.set_defaults(handler=handle_verify_final, mutates_project=True)

    templates_parser = subparsers.add_parser("templates", help="List visual generator templates")
    templates_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    templates_parser.set_defaults(handler=handle_templates)

    validate_template_parser = subparsers.add_parser("validate-template", help="Validate one template file")
    validate_template_parser.add_argument("template_file", type=Path)
    validate_template_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate_template_parser.set_defaults(handler=handle_validate_template)

    render_template_parser = subparsers.add_parser("render-template", help="Render one visual template")
    render_template_parser.add_argument("template_ref", help="Template ID or template Python file")
    render_template_parser.add_argument("output_path", type=Path)
    render_template_parser.add_argument("--params-json", required=True, help="Template params as a JSON object")
    render_template_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    render_template_parser.set_defaults(handler=handle_render_template)

    preview_parser = subparsers.add_parser("preview", help="Render one project preview")
    preview_parser.add_argument("project_dir", type=Path)
    preview_parser.add_argument("--template", help="Template ID or template Python file")
    preview_parser.add_argument("--params-json", help="Template params as a JSON object")
    preview_parser.add_argument("--chunk", dest="chunk_id", help="Preview all planned visuals for one chunk")
    preview_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    preview_parser.set_defaults(handler=handle_preview, mutates_project=True)

    preview_visual_parser = subparsers.add_parser("preview-visual", help="Render a preview for one planned visual")
    preview_visual_parser.add_argument("project_dir", type=Path)
    preview_visual_parser.add_argument("visual_id")
    preview_visual_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    preview_visual_parser.set_defaults(handler=handle_preview_visual, mutates_project=True)

    add_visual_parser = subparsers.add_parser("add-visual", help="Add or update one planned visual")
    add_visual_parser.add_argument("project_dir", type=Path)
    add_visual_parser.add_argument("--chunk", dest="chunk_id", help="Optional chunk ID for this visual")
    add_visual_parser.add_argument("--template", required=True, help="Template ID or template Python file")
    add_visual_parser.add_argument("--start", required=True, type=float, help="Visual start time in seconds")
    add_visual_parser.add_argument("--end", required=True, type=float, help="Visual end time in seconds")
    add_visual_parser.add_argument("--params-json", required=True, help="Template params as a JSON object")
    add_visual_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    add_visual_parser.set_defaults(handler=handle_add_visual, mutates_project=True)

    visuals_parser = subparsers.add_parser("visuals", help="List planned visuals")
    visuals_parser.add_argument("project_dir", type=Path)
    visuals_parser.add_argument("--chunk", dest="chunk_id", help="Filter visuals by chunk ID")
    visuals_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    visuals_parser.set_defaults(handler=handle_visuals)

    update_visual_parser = subparsers.add_parser("update-visual", help="Update one planned visual")
    update_visual_parser.add_argument("project_dir", type=Path)
    update_visual_parser.add_argument("visual_id")
    update_visual_parser.add_argument("--chunk", dest="chunk_id", help="Replacement chunk ID")
    update_visual_parser.add_argument("--template", help="Replacement template ID or template Python file")
    update_visual_parser.add_argument("--start", type=float, help="Replacement visual start time in seconds")
    update_visual_parser.add_argument("--end", type=float, help="Replacement visual end time in seconds")
    update_visual_parser.add_argument("--params-json", help="Replacement template params as a JSON object")
    update_visual_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    update_visual_parser.set_defaults(handler=handle_update_visual, mutates_project=True)

    failures_parser = subparsers.add_parser("failures", help="List project failures")
    failures_parser.add_argument("project_dir", type=Path)
    failures_parser.add_argument(
        "--all",
        dest="include_resolved",
        action="store_true",
        help="Include resolved failure history",
    )
    failures_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    failures_parser.set_defaults(handler=handle_failures)

    resolve_failure_parser = subparsers.add_parser("resolve-failure", help="Resolve one project failure")
    resolve_failure_parser.add_argument("project_dir", type=Path)
    resolve_failure_parser.add_argument("failure_id")
    resolve_failure_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    resolve_failure_parser.set_defaults(handler=handle_resolve_failure, mutates_project=True)

    run_logged_parser = subparsers.add_parser("run-logged", help="Run an external command through the log")
    run_logged_parser.add_argument("external_command", nargs=argparse.REMAINDER)
    run_logged_parser.set_defaults(handler=handle_run_logged)

    return parser


def handle_init(args: argparse.Namespace) -> int:
    result = init_project(
        args.project_dir,
        script_source=cast(Path | None, args.script_source),
        video_source=cast(Path | None, args.video_source),
    )
    action = "Initialized project" if result.created else "Project already initialized"
    print(f"{action}: {result.project_dir}")
    print(f"Script input: {result.data['project']['script']}")
    print(f"Video input: {result.data['project']['video']}")
    summary = build_status(args.project_dir)
    print(f"State: {summary['state']}")
    print(f"Next action: {summary['next_action']}")
    return 0


def handle_init_from_input(args: argparse.Namespace) -> int:
    paths = layout_paths_for_input(args.input_dir, cast(str | None, args.slug))
    selection = discover_input_files(
        paths.input_dir,
        script_source=cast(Path | None, args.script_source),
        video_source=cast(Path | None, args.video_source),
    )
    layout = layout_metadata(
        paths.project_dir,
        slug=paths.slug,
        input_dir=paths.input_dir,
        outputs_dir=paths.outputs_dir,
    )
    paths.outputs_dir.mkdir(parents=True, exist_ok=True)
    result = init_project(
        paths.project_dir,
        script_source=selection["script"],
        video_source=selection["video"],
        layout=layout,
    )
    output: InitFromInputResult = {
        "project_dir": str(result.project_dir),
        "input_dir": str(paths.input_dir),
        "outputs_dir": str(paths.outputs_dir),
        "slug": paths.slug,
        "created": result.created,
        "project_json": str(result.project_file),
        "script": result.data["project"]["script"],
        "video": result.data["project"]["video"],
    }
    if args.json:
        print(json.dumps(output, separators=(",", ":")))
    else:
        print(format_init_from_input_result(output))
        summary = build_status(paths.project_dir)
        print(f"State: {summary['state']}")
        print(f"Next action: {summary['next_action']}")
    return 0


def handle_adopt_layout(args: argparse.Namespace) -> int:
    result = adopt_layout(
        args.project_dir,
        input_dir=args.input_dir,
        explicit_slug=cast(str | None, args.slug),
    )
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_adopt_layout_result(result))
    return 0 if result["success"] else 1


def handle_status(args: argparse.Namespace) -> int:
    summary = build_status(args.project_dir)
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_status(summary))
    return 0


def handle_next(args: argparse.Namespace) -> int:
    summary = build_next_step(args.project_dir)
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_next_step(summary))
    return 0


def handle_probe(args: argparse.Namespace) -> int:
    result = probe_project_media(args.project_dir)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_media_probe_result(result))
    return 0 if result["success"] else 1


def handle_extract_audio(args: argparse.Namespace) -> int:
    result = extract_project_audio(args.project_dir)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_audio_extract_result(result))
    return 0 if result["success"] else 1


def handle_transcribe(args: argparse.Namespace) -> int:
    result = transcribe_project(
        args.project_dir,
        provider=str(args.provider),
        model=str(args.model),
        language=cast(str | None, args.language),
        device=str(args.device),
        compute_type=str(args.compute_type),
    )
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_transcribe_result(result))
    return 0 if result["success"] else 1


def handle_align(args: argparse.Namespace) -> int:
    result = align_project(args.project_dir)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_alignment_result(result))
    return 0 if result["success"] else 1


def handle_alignment_review(args: argparse.Namespace) -> int:
    summary = build_alignment_review(args.project_dir)
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_alignment_review(summary))
    return 0


def handle_create_chunks(args: argparse.Namespace) -> int:
    result = create_chunks(
        args.project_dir,
        target_seconds=float(args.target_seconds),
        min_seconds=float(args.min_seconds),
        max_seconds=float(args.max_seconds),
        force=bool(args.force),
    )
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_create_chunks_result(result))
    return 0 if result["success"] else 1


def handle_chunks(args: argparse.Namespace) -> int:
    summary = build_chunks_summary(args.project_dir)
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_chunks_summary(summary))
    return 0


def handle_approve_camera_only(args: argparse.Namespace) -> int:
    result = approve_camera_only(args.project_dir, args.chunk_id)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_camera_only_result(result))
    return 0 if result["success"] else 1


def handle_plan_visuals(args: argparse.Namespace) -> int:
    result = plan_visuals_for_chunk(
        args.project_dir,
        args.chunk_id,
        max_visuals=int(args.max_visuals),
        force_generated=bool(args.force_generated),
    )
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_plan_visuals_result(result))
    return 0 if result["success"] else 1


def handle_planning_context(args: argparse.Namespace) -> int:
    result = build_planning_context(args.project_dir, args.chunk_id)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_planning_context(result))
    return 0 if result["success"] else 1


def handle_apply_visual_plan(args: argparse.Namespace) -> int:
    result = apply_visual_plan_from_json(args.project_dir, args.chunk_id, args.plan_json)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_apply_visual_plan_result(result))
    return 0 if result["success"] else 1


def handle_visual_intents(args: argparse.Namespace) -> int:
    summary = build_visual_intents_summary(
        args.project_dir,
        chunk_id=cast(str | None, args.chunk_id),
        gaps_only=bool(args.gaps_only),
    )
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_visual_intents_summary(summary))
    return 0


def handle_render_chunk(args: argparse.Namespace) -> int:
    result = render_project_chunk(args.project_dir, args.chunk_id)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_render_chunk_result(result))
    return 0 if result["success"] else 1


def handle_final(args: argparse.Namespace) -> int:
    result = compose_project_final(args.project_dir)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_final_compose_result(result))
    return 0 if result["success"] else 1


def handle_verify_final(args: argparse.Namespace) -> int:
    result = verify_project_final(args.project_dir)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_verify_final_result(result))
    return 0 if result["success"] else 1


def handle_templates(args: argparse.Namespace) -> int:
    inventory = build_inventory()
    if args.json:
        print(json.dumps(inventory, separators=(",", ":")))
    else:
        print(format_inventory(inventory))
    return 0


def handle_validate_template(args: argparse.Namespace) -> int:
    result = validate_template_file(args.template_file)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_template_validation(result))
    return 0 if result["valid"] else 1


def handle_render_template(args: argparse.Namespace) -> int:
    result = render_template_from_json(args.template_ref, args.output_path, args.params_json)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_render_template_result(result))
    return 0 if result["success"] else 1


def handle_preview(args: argparse.Namespace) -> int:
    chunk_id = cast(str | None, args.chunk_id)
    template = cast(str | None, args.template)
    params_json = cast(str | None, args.params_json)

    if chunk_id is not None:
        if template is not None or params_json is not None:
            print("error: --chunk cannot be combined with --template or --params-json", file=sys.stderr)
            return 1
        chunk_result = render_project_preview_for_chunk(args.project_dir, chunk_id)
        if args.json:
            print(json.dumps(chunk_result, separators=(",", ":")))
        else:
            print(format_chunk_preview_result(chunk_result))
        return 0 if chunk_result["success"] else 1

    if template is None or params_json is None:
        print("error: preview requires either --chunk or both --template and --params-json", file=sys.stderr)
        return 1

    result = render_project_preview_from_json(args.project_dir, template, params_json)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_preview_result(result))
    return 0 if result["success"] else 1


def handle_preview_visual(args: argparse.Namespace) -> int:
    result = render_project_preview_for_visual(args.project_dir, args.visual_id)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_preview_visual_result(result))
    return 0 if result["success"] else 1


def handle_add_visual(args: argparse.Namespace) -> int:
    result = add_visual_from_json(
        args.project_dir,
        args.template,
        args.start,
        args.end,
        args.params_json,
        chunk_id=cast(str | None, args.chunk_id),
    )
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_add_visual_result(result))
    return 0 if result["success"] else 1


def handle_visuals(args: argparse.Namespace) -> int:
    summary = build_visuals_summary(args.project_dir, chunk_id=cast(str | None, args.chunk_id))
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_visuals_summary(summary))
    return 0


def handle_update_visual(args: argparse.Namespace) -> int:
    result = update_visual_from_json(
        args.project_dir,
        args.visual_id,
        args.template,
        args.start,
        args.end,
        args.params_json,
        chunk_id=cast(str | None, args.chunk_id),
    )
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_update_visual_result(result))
    return 0 if result["success"] else 1


def handle_failures(args: argparse.Namespace) -> int:
    summary = build_failures_summary(args.project_dir, include_resolved=args.include_resolved)
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(format_failures_summary(summary))
    return 0


def handle_resolve_failure(args: argparse.Namespace) -> int:
    result = resolve_failure_by_id(args.project_dir, args.failure_id)
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(format_resolve_failure_result(result))
    return 0 if result["success"] else 1


def handle_run_logged(args: argparse.Namespace, session: LogSession) -> int:
    command = cast(list[str], args.external_command)
    return run_external_command(command, session)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    load_dotenv()
    session = create_log_session(raw_argv)
    log_only = _log_only_requested(raw_argv)
    forward_output = not log_only or not session.enabled
    started = time.monotonic()
    exit_code = 1

    if session.setup_warning is not None:
        print(f"warning: {session.setup_warning}", file=sys.stderr)
    session.start(raw_argv)
    try:
        with capture_console(session, forward=forward_output):
            try:
                exit_code = _run_cli(raw_argv, session)
            except (ProjectError, TemplateError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = 1
            except ProjectBusyError as exc:
                print(format_project_busy_error(exc), file=sys.stderr)
                exit_code = 3
            except Exception as exc:  # noqa: BLE001 - full details are preserved in the log.
                session.exception(f"unexpected {type(exc).__name__}: {exc}")
                print(f"error: Unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
                exit_code = 1
    finally:
        session.finish(exit_code, time.monotonic() - started)
        session.close()

    if log_only and session.enabled:
        print(f"Exit code: {exit_code}; log: {session.log_path}")
    return exit_code


def _run_cli(raw_argv: list[str], session: LogSession) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    if args.command == "run-logged":
        return handle_run_logged(args, session)

    if args.command == "init-from-input":
        paths = layout_paths_for_input(args.input_dir, cast(str | None, args.slug))
        try:
            paths.project_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectError(f"Could not create project directory {paths.project_dir}: {exc}") from exc
        with ProjectMutationLock(paths.project_dir, run_id=session.run_id, command=str(args.command)):
            return cast(CommandHandler, args.handler)(args)

    handler = cast(CommandHandler, args.handler)
    if not bool(args.mutates_project):
        return handler(args)

    project_dir = cast(Path, args.project_dir)
    if args.command == "init":
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectError(f"Could not create project directory {project_dir}: {exc}") from exc
    elif not project_file_for(project_dir).exists():
        return handler(args)

    with ProjectMutationLock(project_dir, run_id=session.run_id, command=str(args.command)):
        return handler(args)


def _log_only_requested(argv: list[str]) -> bool:
    for token in argv:
        if token == "--":
            return False
        if token == "--log-only":
            return True
        if not token.startswith("-"):
            return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
