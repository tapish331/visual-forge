from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from PIL import Image

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.layout import layout_metadata
from app.project import JsonObject, ProjectState, build_initial_project, write_project
from app.timeline import build_chunking_metadata, build_full_raw_timeline


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(
    *args: str | Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env["VISUAL_FORGE_LOG_DISABLED"] = "1"
    if env is not None:
        command_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=command_env,
    )


def test_render_chunk_json_creates_mp4_and_records_metadata(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["chunk_id"] == "chunk_001"
    output_path = Path(string_value(summary, "output_path"))
    assert output_path == project_dir / "renders" / "chunks" / "chunk_001.mp4"
    assert output_path.read_bytes() == b"fake mp4 data"

    metadata = object_dict(summary, "metadata")
    assert metadata["path"] == "renders/chunks/chunk_001.mp4"
    assert metadata["chunk_id"] == "chunk_001"
    assert metadata["duration_seconds"] == 10.0
    assert metadata["visual_ids"] == ["visual_001"]
    assert metadata["preview_ids"] == ["preview_001"]
    assert object_dict(metadata, "artifact_fingerprint")["kind"] == "stat_v1"

    project = load_project(project_dir)
    chunk = record_list(project, "chunks")[0]
    assert chunk["status"] == "rendered"
    render = object_dict(object_dict(object_dict(project, "renders"), "chunks"), "chunk_001")
    assert render["path"] == "renders/chunks/chunk_001.mp4"


def test_render_chunk_layout_project_writes_outputs(tmp_path: Path) -> None:
    project_dir, outputs_dir = prepared_layout_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    output_path = Path(string_value(json_object(result.stdout), "output_path"))
    assert output_path == outputs_dir / "renders" / "chunks" / "chunk_001.mp4"
    assert output_path.is_file()
    assert not (project_dir / "renders" / "chunks" / "chunk_001.mp4").exists()


def test_render_chunk_accepts_mp4_visual_preview(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    project = load_project(project_dir)
    preview_file = project_dir / "previews" / "preview_001.mp4"
    preview_file.write_bytes(b"fake animated preview")
    visual = record_list(project, "visuals")[0]
    visual["template_ref"] = "animated_opening_hook"
    visual["template_id"] = "animated_opening_hook"
    visual["params"] = {"title": "Animated"}
    preview = record_list(project, "previews")[0]
    preview["template_ref"] = "animated_opening_hook"
    preview["template_id"] = "animated_opening_hook"
    preview["params"] = {"title": "Animated"}
    preview["output"] = "previews/preview_001.mp4"
    preview["output_type"] = "mp4"
    preview["duration_seconds"] = 2.0
    preview["template_version"] = "1.0.0"
    preview["template_fingerprint"] = sha256_fingerprint(REPO_ROOT / "templates" / "animated_opening_hook.py")
    preview["artifact_fingerprint"] = sha256_fingerprint(preview_file)
    write_project_json(project_dir, project)
    ffmpeg = fake_ffmpeg(tmp_path)
    args_file = tmp_path / "ffmpeg-args.json"

    result = run_cli(
        "render-chunk",
        project_dir,
        "chunk_001",
        "--json",
        env=fake_env(ffmpeg, args_path=args_file),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(args_file.read_text(encoding="utf-8"))
    assert isinstance(args, list)
    assert "previews\\preview_001.mp4" in " ".join(str(item) for item in args) or "previews/preview_001.mp4" in " ".join(str(item) for item in args)
    filter_value = args[args.index("-filter_complex") + 1]
    assert "[1:v]trim=duration=2" in filter_value


def test_render_chunk_human_output_is_compact(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("render-chunk", project_dir, "chunk_001", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    assert "Chunk: chunk_001" in result.stdout
    assert "Status: rendered" in result.stdout
    assert "Output:" in result.stdout
    assert "Visuals: 1" in result.stdout


def test_render_chunk_can_rerender_already_rendered_chunk(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)
    first = run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg))

    second = run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    project = load_project(project_dir)
    assert record_list(project, "chunks")[0]["status"] == "rendered"
    assert record_list(project, "failures") == []


def test_render_chunk_uses_explicit_stream_trimming_and_output_duration(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)
    args_file = tmp_path / "ffmpeg-args.json"

    result = run_cli(
        "render-chunk",
        project_dir,
        "chunk_001",
        "--json",
        env=fake_env(ffmpeg, args_path=args_file),
    )

    assert result.returncode == 0, result.stderr
    raw: object = json.loads(args_file.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    args = cast(list[str], raw)
    filter_value = args[args.index("-filter_complex") + 1]
    assert "trim=duration=10" in filter_value
    assert "setpts=PTS-STARTPTS" in filter_value
    assert "atrim=duration=10" in filter_value
    assert "asetpts=PTS-STARTPTS" in filter_value
    assert "[a0]" in args
    assert "-shortest" not in args
    assert args[args.index("-t") + 1] == "10"


def test_render_chunk_failure_preserves_existing_output(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    output = project_dir / "renders" / "chunks" / "chunk_001.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"previous valid render")
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli(
        "render-chunk",
        project_dir,
        "chunk_001",
        "--json",
        env=fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure"),
    )

    assert result.returncode == 1
    assert output.read_bytes() == b"previous valid render"
    assert list(output.parent.glob(".chunk_001.*.mp4")) == []
    failure = only_failure(project_dir)
    assert failure["stage"] == "chunk_render"
    assert failure["scope"] == "chunk:chunk_001"


def test_render_chunk_missing_chunk_records_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)

    result = run_cli("render-chunk", project_dir, "chunk_missing", "--json")

    assert result.returncode == 1
    errors = string_list(json_object(result.stdout), "errors")
    assert "Chunk not found: chunk_missing" in errors
    failure = only_failure(project_dir)
    assert failure["stage"] == "chunk_render"
    assert failure["scope"] == "chunk:chunk_missing"
    assert failure["chunk_id"] == "chunk_missing"


def test_render_chunk_requires_previewed_chunk(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path, chunk_status="new")

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json")

    assert result.returncode == 1
    errors = string_list(json_object(result.stdout), "errors")
    assert "Chunk must be previewed before render: chunk_001" in errors
    assert only_failure(project_dir)["stage"] == "chunk_render"


def test_render_chunk_requires_current_pipeline_freshness(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    (project_dir / "raw.mp4").write_bytes(b"changed raw video")

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json")

    assert result.returncode == 1
    errors = string_list(json_object(result.stdout), "errors")
    assert any(error.startswith("raw freshness is not current: stale") for error in errors)
    assert only_failure(project_dir)["stage"] == "chunk_render"


def test_render_chunk_requires_previewed_visual(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path, visual_status="planned", preview_id=None)

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json")

    assert result.returncode == 1
    errors = string_list(json_object(result.stdout), "errors")
    assert "Visual must be previewed before chunk render: visual_001" in errors
    assert only_failure(project_dir)["stage"] == "chunk_render"


def test_render_chunk_requires_preview_png(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    (project_dir / "previews" / "preview_001.png").unlink()

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json")

    assert result.returncode == 1
    errors = string_list(json_object(result.stdout), "errors")
    assert any(error.startswith("Missing preview PNG for preview_001") for error in errors)
    assert only_failure(project_dir)["stage"] == "chunk_render"


def test_render_chunk_ffmpeg_failure_records_and_retry_resolves_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    failed = run_cli(
        "render-chunk",
        project_dir,
        "chunk_001",
        "--json",
        env=fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure"),
    )
    retry = run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg))

    assert failed.returncode == 1
    assert retry.returncode == 0, retry.stderr
    failure = only_failure(project_dir)
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def test_all_chunks_rendered_moves_status_to_ready_for_final(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)
    assert run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg)).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["state"] == "ready_for_final"
    assert summary["next_action"] == "Run final composition."
    chunks = object_dict(summary, "chunks")
    assert object_dict(chunks, "by_status")["rendered"] == 1


def test_camera_only_chunk_renders_without_visual_inputs(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    project = load_project(project_dir)
    project["visuals"] = []
    project["previews"] = []
    chunk = record_list(project, "chunks")[0]
    chunk["visual_mode"] = "camera_only"
    chunk["status"] = "previewed"
    write_project_json(project_dir, project)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    metadata = object_dict(json_object(result.stdout), "metadata")
    assert metadata["visual_ids"] == []
    assert metadata["preview_ids"] == []
    assert object_dict(metadata, "preview_fingerprints") == {}


def test_undecided_zero_visual_chunk_cannot_render(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    project = load_project(project_dir)
    project["visuals"] = []
    project["previews"] = []
    chunk = record_list(project, "chunks")[0]
    chunk["visual_mode"] = "undecided"
    chunk["status"] = "previewed"
    write_project_json(project_dir, project)

    result = run_cli("render-chunk", project_dir, "chunk_001", "--json")

    assert result.returncode == 1
    assert "Chunk visual mode is undecided: chunk_001" in string_list(json_object(result.stdout), "errors")


def prepared_legacy_project(
    tmp_path: Path,
    *,
    chunk_status: str = "previewed",
    visual_status: str = "previewed",
    preview_id: str | None = "preview_001",
) -> Path:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    script_file = project_dir / "script.txt"
    raw_file = project_dir / "raw.mp4"
    artifact_root = project_dir
    write_source_and_artifacts(script_file, raw_file, artifact_root)
    state = build_state(
        project_dir,
        script_file=script_file,
        raw_file=raw_file,
        artifact_root=artifact_root,
        chunk_status=chunk_status,
        visual_status=visual_status,
        preview_id=preview_id,
    )
    write_project(project_dir / "project.json", state)
    return project_dir


def prepared_layout_project(tmp_path: Path) -> tuple[Path, Path]:
    input_dir = tmp_path / "inputs" / "episode"
    project_dir = tmp_path / "projects" / "episode"
    outputs_dir = tmp_path / "outputs" / "episode"
    project_dir.mkdir(parents=True)
    script_file = input_dir / "script.txt"
    raw_file = input_dir / "raw.mp4"
    write_source_and_artifacts(script_file, raw_file, outputs_dir)
    layout = layout_metadata(project_dir, slug="episode", input_dir=input_dir.resolve(), outputs_dir=outputs_dir)
    state = build_state(
        project_dir,
        script_file=script_file,
        raw_file=raw_file,
        artifact_root=outputs_dir,
        layout=layout,
    )
    write_project(project_dir / "project.json", state)
    return project_dir, outputs_dir


def write_source_and_artifacts(script_file: Path, raw_file: Path, artifact_root: Path) -> None:
    script_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text("Hello world.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")

    audio_file = artifact_root / "audio" / "narration.wav"
    transcript_file = artifact_root / "transcripts" / "narration.json"
    alignment_file = artifact_root / "alignment" / "script_alignment.json"
    preview_file = artifact_root / "previews" / "preview_001.png"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"narration audio")
    atomic_write_json(
        transcript_file,
        {
            "schema_version": 1,
            "source": "audio/narration.wav",
            "text": "Hello world.",
            "segments": [],
        },
    )
    atomic_write_json(alignment_file, {"schema_version": 1, "blocks": []})
    preview_file.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), (245, 245, 245)).save(preview_file)


def build_state(
    project_dir: Path,
    *,
    script_file: Path,
    raw_file: Path,
    artifact_root: Path,
    chunk_status: str = "previewed",
    visual_status: str = "previewed",
    preview_id: str | None = "preview_001",
    layout: JsonObject | None = None,
) -> ProjectState:
    audio_file = artifact_root / "audio" / "narration.wav"
    transcript_file = artifact_root / "transcripts" / "narration.json"
    alignment_file = artifact_root / "alignment" / "script_alignment.json"
    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    state = build_initial_project(
        project_dir,
        script_source=script_file,
        video_source=raw_file,
        layout=layout,
    )
    state["media"] = {
        "raw": {
            "path": state["project"]["video"],
            "duration_seconds": 10.0,
            "size_bytes": raw_file.stat().st_size,
            "bit_rate": 8000000,
            "video": {"codec": "h264", "width": 848, "height": 480, "frame_rate": 29.97},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
            "source_fingerprint": raw_fingerprint,
        },
        "audio": {
            "narration": {
                "path": "audio/narration.wav",
                "source": state["project"]["video"],
                "source_fingerprint": raw_fingerprint,
                "artifact_fingerprint": audio_fingerprint,
            }
        },
    }
    state["transcript"] = {
        "narration": {
            "path": "transcripts/narration.json",
            "source": "audio/narration.wav",
            "status": "transcribed",
            "source_fingerprint": audio_fingerprint,
            "artifact_fingerprint": transcript_fingerprint,
        }
    }
    state["alignment"] = {
        "script": {
            "path": "alignment/script_alignment.json",
            "source_script": state["project"]["script"],
            "source_transcript": "transcripts/narration.json",
            "method": "sequence_matcher_words_v1",
            "status": "aligned",
            "blocks": 1,
            "aligned_blocks": 1,
            "needs_review_blocks": 0,
            "unmatched_blocks": 0,
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(script_file)),
                "transcript_sha256": digest_value(transcript_fingerprint),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }
    state["chunks"] = [
        {
            "id": "chunk_001",
            "start": 0.0,
            "end": 10.0,
            "status": chunk_status,
            "visual_mode": "visuals",
            "alignment_block_ids": ["block_001"],
            "warning_block_ids": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
    ]
    state["visuals"] = [
        {
            "id": "visual_001",
            "chunk_id": "chunk_001",
            "template_ref": "simple_card",
            "template_id": "simple_card",
            "params": {"title": "Key idea"},
            "start": 2.0,
            "end": 4.0,
            "status": visual_status,
            "preview_id": preview_id,
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
    ]
    if preview_id is not None:
        preview_file = artifact_root / "previews" / "preview_001.png"
        template_file = REPO_ROOT / "templates" / "simple_card.py"
        state["previews"] = [
            {
                "id": preview_id,
                "template_ref": "simple_card",
                "template_id": "simple_card",
                "params": {"title": "Key idea"},
                "output": "previews/preview_001.png",
                "status": "rendered",
                "created_at": "2026-06-20T00:00:00Z",
                "updated_at": "2026-06-20T00:00:00Z",
                "template_version": "1.0.0",
                "template_fingerprint": sha256_fingerprint(template_file),
                "artifact_fingerprint": sha256_fingerprint(preview_file),
            }
        ]
    timeline = build_full_raw_timeline(project_dir, state)
    options: JsonObject = {"target_seconds": 180.0, "min_seconds": 90.0, "max_seconds": 240.0}
    state["timeline"] = timeline
    state["chunking"] = build_chunking_metadata(state, timeline, state["chunks"], options)
    return state


def fake_ffmpeg(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "stderr = os.environ.get('FAKE_FFMPEG_STDERR', '')",
                "if stderr:",
                "    sys.stderr.write(stderr)",
                "args_path = os.environ.get('FAKE_FFMPEG_ARGS_PATH', '')",
                "if args_path:",
                "    Path(args_path).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')",
                "if os.environ.get('FAKE_FFMPEG_CREATE_OUTPUT', '1') == '1':",
                "    output = Path(sys.argv[-1])",
                "    output.parent.mkdir(parents=True, exist_ok=True)",
                "    output.write_bytes(b'fake mp4 data')",
                "raise SystemExit(int(os.environ.get('FAKE_FFMPEG_EXIT', '0')))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = tmp_path / "fake_ffmpeg.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return wrapper
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def fake_env(
    ffmpeg: Path,
    *,
    exit_code: int = 0,
    stderr: str = "",
    create_output: bool = True,
    args_path: Path | None = None,
) -> dict[str, str]:
    return {
        "VISUAL_FORGE_FFMPEG": str(ffmpeg),
        "FAKE_FFMPEG_EXIT": str(exit_code),
        "FAKE_FFMPEG_STDERR": stderr,
        "FAKE_FFMPEG_CREATE_OUTPUT": "1" if create_output else "0",
        "FAKE_FFMPEG_ARGS_PATH": str(args_path) if args_path is not None else "",
    }


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project_json(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def only_failure(project_dir: Path) -> Record:
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    return failures[0]


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def object_dict(data: ProjectJson | Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: ProjectJson | Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    records: list[Record] = []
    for item in value:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def string_list(data: Record, key: str) -> list[str]:
    value = data[key]
    assert isinstance(value, list)
    strings: list[str] = []
    for item in value:
        assert isinstance(item, str)
        strings.append(item)
    return strings


def string_value(data: Record, key: str) -> str:
    value = data[key]
    assert isinstance(value, str)
    return value


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value
