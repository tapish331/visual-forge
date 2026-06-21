from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from app import project as project_module
from app.project import ProjectError, build_initial_project, write_project


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def test_init_creates_valid_project_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"

    result = run_cli("init", project_dir)

    assert result.returncode == 0, result.stderr
    project_file = project_dir / "project.json"
    assert project_file.exists()

    data = json.loads(project_file.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["project"]["name"] == "my-video"
    assert data["project"]["script"] == "script.txt"
    assert data["project"]["video"] == "raw.mp4"
    assert data["project"]["final"] == "final.mp4"
    assert data["chunks"] == []
    assert data["corrections"] == []
    assert data["cache"] == {}
    assert data["failures"] == []
    assert data["media"] == {}
    assert data["transcript"] == {}
    assert data["alignment"] == {}
    assert data["previews"] == []
    assert data["visual_intents"] == []
    assert data["visuals"] == []


def test_repeated_init_is_non_destructive(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"

    first = run_cli("init", project_dir)
    assert first.returncode == 0, first.stderr

    project_file = project_dir / "project.json"
    data = json.loads(project_file.read_text(encoding="utf-8"))
    data["chunks"].append({"id": "chunk_001", "status": "planned"})
    project_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    second = run_cli("init", project_dir)

    assert second.returncode == 0, second.stderr
    assert "Project already initialized" in second.stdout
    unchanged = json.loads(project_file.read_text(encoding="utf-8"))
    assert unchanged["chunks"] == [{"id": "chunk_001", "status": "planned"}]


def test_init_accepts_external_script_and_video_paths(tmp_path: Path) -> None:
    inputs_dir = tmp_path / "source inputs"
    inputs_dir.mkdir()
    script_source = inputs_dir / "episode.mp4.txt"
    video_source = inputs_dir / "episode.mp4"
    script_source.write_text("Raw narration script.\n", encoding="utf-8")
    video_source.write_bytes(b"raw video")
    project_dir = tmp_path / "projects" / "episode"

    result = run_cli("init", project_dir, "--script", script_source, "--video", video_source)

    assert result.returncode == 0, result.stderr
    data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert data["project"]["script"] == "../../source inputs/episode.mp4.txt"
    assert data["project"]["video"] == "../../source inputs/episode.mp4"
    assert not (project_dir / "script.txt").exists()
    assert not (project_dir / "raw.mp4").exists()
    assert "Script input: ../../source inputs/episode.mp4.txt" in result.stdout
    assert "Video input: ../../source inputs/episode.mp4" in result.stdout

    status_result = run_cli("status", project_dir, "--json")
    assert status_result.returncode == 0, status_result.stderr
    status = json.loads(status_result.stdout)
    assert status["state"] == "ready_for_probe"
    assert status["inputs"]["script"]["exists"] is True
    assert status["inputs"]["video"]["exists"] is True
    assert status["next_action"] == "Run media probing for ../../source inputs/episode.mp4."


def test_repeated_init_with_same_external_inputs_is_safe(tmp_path: Path) -> None:
    script_source = tmp_path / "script.txt"
    video_source = tmp_path / "video.mp4"
    script_source.write_text("Script.\n", encoding="utf-8")
    video_source.write_bytes(b"video")
    project_dir = tmp_path / "project"
    assert run_cli("init", project_dir, "--script", script_source, "--video", video_source).returncode == 0

    repeated = run_cli("init", project_dir, "--script", script_source, "--video", video_source)

    assert repeated.returncode == 0, repeated.stderr
    assert "Project already initialized" in repeated.stdout


def test_init_rejects_missing_input_source(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"

    result = run_cli("init", project_dir, "--script", tmp_path / "missing.txt")

    assert result.returncode == 1
    assert "error: Input script file does not exist" in result.stderr
    assert not (project_dir / "project.json").exists()


def test_init_does_not_repoint_existing_project_inputs(tmp_path: Path) -> None:
    first_script = tmp_path / "first.txt"
    second_script = tmp_path / "second.txt"
    first_script.write_text("First.\n", encoding="utf-8")
    second_script.write_text("Second.\n", encoding="utf-8")
    project_dir = tmp_path / "project"
    assert run_cli("init", project_dir, "--script", first_script).returncode == 0

    result = run_cli("init", project_dir, "--script", second_script)

    assert result.returncode == 1
    assert "Project is already initialized with script input" in result.stderr
    data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert data["project"]["script"] == "../first.txt"


def test_status_reports_missing_inputs(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    assert run_cli("init", project_dir).returncode == 0

    result = run_cli("status", project_dir)

    assert result.returncode == 0, result.stderr
    assert "State: missing_inputs" in result.stdout
    assert "script: script.txt (missing)" in result.stdout
    assert "video: raw.mp4 (missing)" in result.stdout


def test_status_json_is_compact_machine_readable(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    assert run_cli("init", project_dir).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["state"] == "missing_inputs"
    assert summary["inputs"]["script"]["exists"] is False
    assert summary["inputs"]["video"]["exists"] is False
    assert summary["chunks"]["total"] == 0
    assert summary["media"]["raw"]["probed"] is False
    assert summary["visuals"]["total"] == 0
    assert summary["failures"]["total"] == 0


def test_status_ready_for_probe_when_inputs_exist(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    assert run_cli("init", project_dir).returncode == 0
    (project_dir / "script.txt").write_text("Hello world\n", encoding="utf-8")
    (project_dir / "raw.mp4").write_bytes(b"not a real video yet")

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["state"] == "ready_for_probe"
    assert summary["next_action"] == "Run media probing for raw.mp4."


def test_malformed_project_json_returns_nonzero(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    (project_dir / "project.json").write_text("{not-json", encoding="utf-8")

    result = run_cli("status", project_dir)

    assert result.returncode != 0
    assert "error: Invalid JSON" in result.stderr


def test_missing_project_json_returns_nonzero(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()

    result = run_cli("status", project_dir)

    assert result.returncode != 0
    assert "error: Missing project.json" in result.stderr


def test_atomic_project_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"

    result = run_cli("init", project_dir)

    assert result.returncode == 0, result.stderr
    assert list(project_dir.glob(".project.json.*.tmp")) == []
    data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1


def test_atomic_project_write_failure_preserves_original(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()
    project_file = project_dir / "project.json"
    original = '{"sentinel":true}\n'
    project_file.write_text(original, encoding="utf-8")

    def fail_replace(source: object, destination: object) -> None:
        _ = source, destination
        raise OSError("simulated replace failure")

    raised_error: ProjectError | None = None
    with patch.object(project_module.os, "replace", side_effect=fail_replace):
        try:
            write_project(project_file, build_initial_project(project_dir))
        except ProjectError as exc:
            raised_error = exc

    assert raised_error is not None
    assert "simulated replace failure" in str(raised_error)
    assert project_file.read_text(encoding="utf-8") == original
    assert list(project_dir.glob(".project.json.*.tmp")) == []
