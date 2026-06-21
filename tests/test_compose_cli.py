from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.layout import layout_metadata
from app.project import JsonObject, JsonValue, ProjectState, build_initial_project, write_project
from app.render_freshness import build_visual_plan_fingerprint
from app.timeline import build_chunking_metadata, build_full_raw_timeline, current_chunk_plan_fingerprint


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def test_final_json_creates_final_mp4_and_records_metadata(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("final", project_dir, "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    output_path = Path(string_value(summary, "output_path"))
    assert output_path == project_dir / "final.mp4"
    assert output_path.read_bytes().startswith(b"fake final\n")

    metadata = object_dict(summary, "metadata")
    assert metadata["path"] == "final.mp4"
    assert metadata["status"] == "rendered"
    assert metadata["chunk_ids"] == ["chunk_001"]
    assert metadata["chunk_paths"] == ["renders/chunks/chunk_001.mp4"]
    assert metadata["duration_seconds"] == 10.0
    assert object_dict(metadata, "artifact_fingerprint")["kind"] == "stat_v1"
    assert "chunk_001" in object_dict(metadata, "source_fingerprints")

    project = load_project(project_dir)
    final = object_dict(object_dict(project, "renders"), "final")
    assert final["path"] == "final.mp4"


def test_final_layout_project_writes_outputs_final(tmp_path: Path) -> None:
    project_dir, outputs_dir = prepared_layout_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("final", project_dir, "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    output_path = Path(string_value(json_object(result.stdout), "output_path"))
    assert output_path == outputs_dir / "final.mp4"
    assert output_path.is_file()
    assert not (project_dir / "final.mp4").exists()


def test_final_composes_chunks_in_timeline_order(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(
        tmp_path,
        chunks=[
            chunk_record("chunk_002", 10.0, 20.0),
            chunk_record("chunk_001", 0.0, 10.0),
        ],
    )
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("final", project_dir, "--json", env=fake_env(ffmpeg))

    assert result.returncode == 0, result.stderr
    final_bytes = (project_dir / "final.mp4").read_bytes()
    first = final_bytes.index(b"chunk_001.mp4")
    second = final_bytes.index(b"chunk_002.mp4")
    assert first < second


def test_status_moves_to_ready_for_verification_after_final_composition(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)
    assert run_cli("final", project_dir, "--json", env=fake_env(ffmpeg)).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "ready_for_verification"
    assert status["next_action"] == "Verify final video."
    final = object_dict(object_dict(status, "outputs"), "final")
    assert final["exists"] is True
    assert final["current"] is True


def test_final_missing_chunks_records_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path, chunks=[])

    result = run_cli("final", project_dir, "--json")

    assert result.returncode == 1
    assert "No chunks exist. Run create-chunks before final composition." in string_list(json_object(result.stdout), "errors")
    failure = only_failure(project_dir)
    assert failure["stage"] == "final_compose"
    assert failure["scope"] == "final:final.mp4"


def test_final_requires_rendered_chunks(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path, chunks=[chunk_record("chunk_001", 0.0, 10.0, status="previewed")])

    result = run_cli("final", project_dir, "--json")

    assert result.returncode == 1
    assert "Chunk must be rendered before final composition: chunk_001" in string_list(json_object(result.stdout), "errors")
    assert only_failure(project_dir)["stage"] == "final_compose"


def test_final_requires_render_metadata(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    project = load_project(project_dir)
    object_dict(object_dict(project, "renders"), "chunks").pop("chunk_001")
    write_project_json(project_dir, project)

    result = run_cli("final", project_dir, "--json")

    assert result.returncode == 1
    assert "Missing rendered chunk metadata: chunk_001" in string_list(json_object(result.stdout), "errors")
    assert only_failure(project_dir)["stage"] == "final_compose"


def test_final_requires_existing_current_chunk_mp4(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    (project_dir / "renders" / "chunks" / "chunk_001.mp4").write_bytes(b"changed chunk")

    result = run_cli("final", project_dir, "--json")

    assert result.returncode == 1
    assert "Rendered chunk file is stale: chunk_001" in string_list(json_object(result.stdout), "errors")
    assert only_failure(project_dir)["stage"] == "final_compose"


def test_final_missing_ffmpeg_records_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)

    result = run_cli(
        "final",
        project_dir,
        "--json",
        env={"VISUAL_FORGE_FFMPEG": str(tmp_path / "missing-ffmpeg.exe")},
    )

    assert result.returncode == 1
    assert string_list(json_object(result.stdout), "errors")[0].startswith("ffmpeg not found")
    assert only_failure(project_dir)["stage"] == "final_compose"


def test_final_ffmpeg_failure_preserves_existing_final(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    final_file = project_dir / "final.mp4"
    final_file.write_bytes(b"previous final")
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli(
        "final",
        project_dir,
        "--json",
        env=fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure"),
    )

    assert result.returncode == 1
    assert final_file.read_bytes() == b"previous final"
    assert list(project_dir.glob(".final.*.mp4")) == []
    assert only_failure(project_dir)["stage"] == "final_compose"


def test_final_missing_output_after_success_records_failure(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)

    result = run_cli("final", project_dir, "--json", env=fake_env(ffmpeg, create_output=False))

    assert result.returncode == 1
    assert "FFmpeg completed but did not create final.mp4" in string_list(json_object(result.stdout), "errors")
    assert only_failure(project_dir)["stage"] == "final_compose"


def test_final_repeated_failure_and_successful_retry_lifecycle(tmp_path: Path) -> None:
    project_dir = prepared_legacy_project(tmp_path)
    ffmpeg = fake_ffmpeg(tmp_path)
    failing_env = fake_env(ffmpeg, exit_code=7, stderr="simulated ffmpeg failure")

    first = run_cli("final", project_dir, "--json", env=failing_env)
    second = run_cli("final", project_dir, "--json", env=failing_env)
    retry = run_cli("final", project_dir, "--json", env=fake_env(ffmpeg))

    assert first.returncode == 1
    assert second.returncode == 1
    assert retry.returncode == 0, retry.stderr
    failure = only_failure(project_dir)
    assert failure["attempt_count"] == 2
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)


def prepared_legacy_project(tmp_path: Path, *, chunks: list[Record] | None = None) -> Path:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    script_file = project_dir / "script.txt"
    raw_file = project_dir / "raw.mp4"
    artifact_root = project_dir
    selected_chunks = [chunk_record("chunk_001", 0.0, 10.0)] if chunks is None else chunks
    write_source_and_artifacts(script_file, raw_file, artifact_root, selected_chunks)
    state = build_state(
        project_dir,
        script_file=script_file,
        raw_file=raw_file,
        artifact_root=artifact_root,
        chunks=selected_chunks,
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
    chunks = [chunk_record("chunk_001", 0.0, 10.0)]
    write_source_and_artifacts(script_file, raw_file, outputs_dir, chunks)
    layout = layout_metadata(project_dir, slug="episode", input_dir=input_dir.resolve(), outputs_dir=outputs_dir)
    state = build_state(
        project_dir,
        script_file=script_file,
        raw_file=raw_file,
        artifact_root=outputs_dir,
        chunks=chunks,
        layout=layout,
    )
    write_project(project_dir / "project.json", state)
    return project_dir, outputs_dir


def write_source_and_artifacts(script_file: Path, raw_file: Path, artifact_root: Path, chunks: list[Record]) -> None:
    script_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text("Hello world.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")

    audio_file = artifact_root / "audio" / "narration.wav"
    transcript_file = artifact_root / "transcripts" / "narration.json"
    alignment_file = artifact_root / "alignment" / "script_alignment.json"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"narration audio")
    atomic_write_json(transcript_file, {"schema_version": 1, "text": "Hello world.", "segments": []})
    atomic_write_json(alignment_file, {"schema_version": 1, "blocks": []})

    for chunk in chunks:
        chunk_id = string_value(chunk, "id")
        chunk_file = artifact_root / "renders" / "chunks" / f"{chunk_id}.mp4"
        chunk_file.parent.mkdir(parents=True, exist_ok=True)
        chunk_file.write_bytes(f"rendered {chunk_id}".encode("utf-8"))


def build_state(
    project_dir: Path,
    *,
    script_file: Path,
    raw_file: Path,
    artifact_root: Path,
    chunks: list[Record],
    layout: JsonObject | None = None,
) -> ProjectState:
    audio_file = artifact_root / "audio" / "narration.wav"
    transcript_file = artifact_root / "transcripts" / "narration.json"
    alignment_file = artifact_root / "alignment" / "script_alignment.json"
    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    state = build_initial_project(project_dir, script_source=script_file, video_source=raw_file, layout=layout)
    state["media"] = {
        "raw": {
            "path": state["project"]["video"],
            "duration_seconds": max((number_value(chunk, "end") for chunk in chunks), default=20.0),
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
    state["chunks"] = chunks_json(chunks)
    visuals: list[JsonObject] = []
    previews: list[JsonObject] = []
    template_file = REPO_ROOT / "templates" / "simple_card.py"
    template_fingerprint = sha256_fingerprint(template_file)
    for chunk in chunks:
        chunk_id = string_value(chunk, "id")
        preview_id = f"preview_{chunk_id}"
        preview_path = artifact_root / "previews" / f"{preview_id}.png"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(f"preview {chunk_id}".encode("utf-8"))
        visuals.append(
            {
                "id": f"visual_{chunk_id}",
                "chunk_id": chunk_id,
                "template_ref": "simple_card",
                "template_id": "simple_card",
                "params": {"title": chunk_id},
                "start": number_value(chunk, "start"),
                "end": number_value(chunk, "end"),
                "status": "previewed",
                "preview_id": preview_id,
            }
        )
        previews.append(
            {
                "id": preview_id,
                "template_ref": "simple_card",
                "template_id": "simple_card",
                "params": {"title": chunk_id},
                "output": f"previews/{preview_id}.png",
                "status": "rendered",
                "template_version": "1.0.0",
                "template_fingerprint": template_fingerprint,
                "artifact_fingerprint": sha256_fingerprint(preview_path),
            }
        )
    state["visuals"] = visuals
    state["previews"] = previews
    chunk_plan_fingerprint: JsonObject | None = None
    if chunks:
        timeline = build_full_raw_timeline(project_dir, state)
        options: JsonObject = {"target_seconds": 180.0, "min_seconds": 90.0, "max_seconds": 240.0}
        state["timeline"] = timeline
        state["chunking"] = build_chunking_metadata(state, timeline, state["chunks"], options)
        chunk_plan_fingerprint = current_chunk_plan_fingerprint(state)
        assert chunk_plan_fingerprint is not None
    render_chunks: JsonObject = {}
    for chunk in chunks:
        chunk_id = string_value(chunk, "id")
        chunk_file = artifact_root / "renders" / "chunks" / f"{chunk_id}.mp4"
        if chunk_file.exists():
            assert chunk_plan_fingerprint is not None
            plan_fingerprint = build_visual_plan_fingerprint(state, chunk_id)
            assert plan_fingerprint is not None
            preview_id = f"preview_{chunk_id}"
            preview = next(item for item in previews if item.get("id") == preview_id)
            render_chunks[chunk_id] = {
                "path": f"renders/chunks/{chunk_id}.mp4",
                "chunk_id": chunk_id,
                "source_fingerprint": raw_fingerprint,
                "duration_seconds": number_value(chunk, "end") - number_value(chunk, "start"),
                "status": "rendered",
                "visual_plan_fingerprint": plan_fingerprint,
                "chunk_plan_fingerprint": chunk_plan_fingerprint,
                "preview_fingerprints": {
                    preview_id: preview["artifact_fingerprint"],
                },
                "artifact_fingerprint": stat_fingerprint(chunk_file),
            }
    state["renders"] = {"chunks": render_chunks}
    return state


def chunk_record(chunk_id: str, start: float, end: float, *, status: str = "rendered") -> Record:
    return {
        "id": chunk_id,
        "start": start,
        "end": end,
        "status": status,
        "visual_mode": "visuals",
        "alignment_block_ids": ["block_001"],
        "warning_block_ids": [],
        "created_at": "2026-06-20T00:00:00Z",
        "updated_at": "2026-06-20T00:00:00Z",
    }


def fake_ffmpeg(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "import sys",
                "from pathlib import Path",
                "stderr = os.environ.get('FAKE_FFMPEG_STDERR', '')",
                "if stderr:",
                "    sys.stderr.write(stderr)",
                "list_path = Path(sys.argv[sys.argv.index('-i') + 1])",
                "output = Path(sys.argv[-1])",
                "if os.environ.get('FAKE_FFMPEG_CREATE_OUTPUT', '1') == '1':",
                "    output.parent.mkdir(parents=True, exist_ok=True)",
                "    output.write_bytes(b'fake final\\n' + list_path.read_bytes())",
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


def fake_env(ffmpeg: Path, *, exit_code: int = 0, stderr: str = "", create_output: bool = True) -> dict[str, str]:
    return {
        "VISUAL_FORGE_FFMPEG": str(ffmpeg),
        "FAKE_FFMPEG_EXIT": str(exit_code),
        "FAKE_FFMPEG_STDERR": stderr,
        "FAKE_FFMPEG_CREATE_OUTPUT": "1" if create_output else "0",
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


def chunks_json(chunks: list[Record]) -> list[JsonObject]:
    output: list[JsonObject] = []
    for chunk in chunks:
        output.append(cast(JsonObject, chunk))
    return output


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


def number_value(data: Record, key: str) -> float:
    value = data[key]
    assert isinstance(value, int | float)
    return float(value)


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value
