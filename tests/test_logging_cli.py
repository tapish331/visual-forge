from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from app.logging_utils import create_log_session


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(log_dir: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = logging_environment(log_dir)
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.main", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=environment,
    )


def logging_environment(log_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["VISUAL_FORGE_LOG_DISABLED"] = "0"
    environment["VISUAL_FORGE_LOG_DIR"] = str(log_dir)
    environment["VISUAL_FORGE_LOG_MAX_BYTES"] = str(5 * 1024 * 1024)
    environment["VISUAL_FORGE_LOG_BACKUP_COUNT"] = "4"
    return environment


def test_normal_json_output_is_preserved_and_logged(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"

    result = run_cli(log_dir, "templates", "--json")

    assert result.returncode == 0, result.stderr
    summary: object = json.loads(result.stdout)
    assert isinstance(summary, dict)
    log = read_logs(log_dir)
    assert "command started" in log
    assert "stream=stdout" in log
    assert '"valid_count":1' in log
    assert "command finished exit_code=0" in log


def test_log_only_suppresses_command_output_and_prints_summary(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"

    result = run_cli(log_dir, "--log-only", "templates", "--json")

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    assert result.stdout.startswith("Exit code: 0; log: ")
    assert '"valid_count":1' not in result.stdout
    assert '"valid_count":1' in read_logs(log_dir)


def test_run_logged_streams_output_and_preserves_exit_code(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"

    success = run_cli(
        log_dir,
        "run-logged",
        "--",
        sys.executable,
        "-c",
        "print('child-output')",
    )
    failure = run_cli(
        log_dir,
        "run-logged",
        "--",
        sys.executable,
        "-c",
        "import sys; print('child-failure'); sys.exit(7)",
    )

    assert success.returncode == 0, success.stderr
    assert success.stdout == "child-output\n"
    assert failure.returncode == 7
    assert failure.stdout == "child-failure\n"
    log = read_logs(log_dir)
    assert "external command finished exit_code=0" in log
    assert "external command finished exit_code=7" in log


def test_run_logged_missing_executable_is_actionable(tmp_path: Path) -> None:
    result = run_cli(tmp_path / "logs", "run-logged", "--", "visual-forge-command-that-does-not-exist")

    assert result.returncode == 127
    assert "Could not start command" in result.stderr


def test_command_arguments_are_redacted(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"

    result = run_cli(
        log_dir,
        "run-logged",
        "--",
        sys.executable,
        "-c",
        "pass",
        "--token",
        "plain-secret-value",
        "https://username:password@example.com/path",
        '{"api_key":"json-secret-value"}',
    )

    assert result.returncode == 0, result.stderr
    log = read_logs(log_dir)
    assert "plain-secret-value" not in log
    assert "username" not in log
    assert "password@example.com" not in log
    assert "json-secret-value" not in log
    assert "***" in log


def test_parallel_processes_share_log_without_losing_markers(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    environment = logging_environment(log_dir)
    processes: list[tuple[str, subprocess.Popen[str]]] = []
    for index in range(6):
        marker = f"parallel-marker-{index}"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.main",
                "run-logged",
                "--",
                sys.executable,
                "-c",
                f"print('{marker}')",
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        processes.append((marker, process))

    for marker, process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert marker in stdout

    log = read_logs(log_dir)
    for marker, _ in processes:
        assert marker in log


def test_parallel_rotation_keeps_bounded_backup_count(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    environment = logging_environment(log_dir)
    environment["VISUAL_FORGE_LOG_MAX_BYTES"] = "4096"
    environment["VISUAL_FORGE_LOG_BACKUP_COUNT"] = "2"
    code = "\n".join(
        [
            "for index in range(80):",
            "    print(f'rotation-line-{index:03d}-' + 'x' * 80)",
        ]
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "app.main", "--log-only", "run-logged", "--", sys.executable, "-c", code],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        for _ in range(4)
    ]

    for process in processes:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr

    log_files = sorted(log_dir.glob("visual-forge.log*"))
    data_files = [path for path in log_files if not path.name.endswith(".lock")]
    assert 1 <= len(data_files) <= 3
    assert all(path.stat().st_size > 0 for path in data_files)


def test_logging_setup_failure_does_not_break_command(tmp_path: Path) -> None:
    invalid_log_dir = tmp_path / "not-a-directory"
    invalid_log_dir.write_text("file", encoding="utf-8")

    result = run_cli(invalid_log_dir, "templates", "--json")

    assert result.returncode == 0
    assert isinstance(json.loads(result.stdout), dict)
    assert "warning: Logging unavailable" in result.stderr


def test_unexpected_exception_records_traceback(tmp_path: Path) -> None:
    environment = logging_environment(tmp_path / "logs")
    with patch.dict(os.environ, environment, clear=True):
        session = create_log_session(["test-exception"])
        try:
            raise RuntimeError("traceback-marker")
        except RuntimeError:
            session.exception("unexpected runtime error")
        finally:
            session.close()

    log = read_logs(tmp_path / "logs")
    assert "Traceback" in log
    assert "RuntimeError: traceback-marker" in log


def read_logs(log_dir: Path) -> str:
    contents: list[str] = []
    for path in sorted(log_dir.glob("visual-forge.log*"), reverse=True):
        if path.is_file() and not path.name.endswith(".lock"):
            contents.append(path.read_text(encoding="utf-8"))
    return "".join(contents)
