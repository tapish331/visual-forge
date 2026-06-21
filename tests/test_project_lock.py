from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.project_lock import PROJECT_LOCK_FILENAME, ProjectMutationLock, read_lock_owner


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def init_project(project_dir: Path) -> None:
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr


def test_same_project_mutation_is_rejected_with_owner(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    with ProjectMutationLock(project_dir, run_id="run_locktest", command="held-command"):
        result = run_cli("resolve-failure", project_dir, "failure_9999")

    assert result.returncode == 3
    assert f"Project is busy: {project_dir}" in result.stderr
    assert "command held-command" in result.stderr
    assert "PID " in result.stderr


def test_read_only_status_succeeds_while_project_is_locked(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    with ProjectMutationLock(project_dir, run_id="run_locktest", command="held-command"):
        result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary: object = json.loads(result.stdout)
    assert isinstance(summary, dict)


def test_different_project_mutation_succeeds_while_lock_is_held(tmp_path: Path) -> None:
    first_project = tmp_path / "first"
    second_project = tmp_path / "second"
    init_project(first_project)
    init_project(second_project)

    with ProjectMutationLock(first_project, run_id="run_locktest", command="held-command"):
        result = run_cli("resolve-failure", second_project, "failure_9999")

    assert result.returncode == 1
    assert "Failure not found" in result.stdout
    assert "Project is busy" not in result.stderr


def test_project_lock_releases_after_exception(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    project_dir.mkdir()

    caught = False
    try:
        with ProjectMutationLock(project_dir, run_id="run_first", command="first"):
            raise RuntimeError("release lock")
    except RuntimeError:
        caught = True

    assert caught is True
    with ProjectMutationLock(project_dir, run_id="run_second", command="second"):
        owner = read_lock_owner(project_dir / PROJECT_LOCK_FILENAME)
        assert owner is not None
        assert owner.run_id == "run_second"
