from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def init_project(project_dir: Path, *, with_inputs: bool = False) -> None:
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr
    if with_inputs:
        (project_dir / "script.txt").write_text("Hello\n", encoding="utf-8")
        (project_dir / "raw.mp4").write_bytes(b"placeholder")


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_repeated_failure_updates_active_record(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    first = invalid_preview(project_dir)
    second = invalid_preview(project_dir)

    assert first.returncode != 0
    assert second.returncode != 0
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    failure = failures[0]
    assert failure["id"] == "failure_0001"
    assert failure["stage"] == "preview"
    assert failure["scope"] == "template:simple_card"
    assert failure["status"] == "active"
    assert failure["attempt_count"] == 2
    assert failure["resolved_at"] is None


def test_successful_preview_resolves_failure_and_restores_status(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir, with_inputs=True)
    assert invalid_preview(project_dir).returncode != 0

    corrected = run_cli(
        "preview",
        project_dir,
        "--template",
        "simple_card",
        "--params-json",
        '{"title":"Corrected title"}',
    )

    assert corrected.returncode == 0, corrected.stderr
    failure = record_list(load_project(project_dir), "failures")[0]
    assert failure["status"] == "resolved"
    assert isinstance(failure["resolved_at"], str)

    status_result = run_cli("status", project_dir, "--json")
    assert status_result.returncode == 0, status_result.stderr
    status = json_object(status_result.stdout)
    assert status["state"] == "ready_for_probe"
    failure_summary = object_dict(status, "failures")
    assert failure_summary["total"] == 1
    assert failure_summary["active"] == 0
    assert failure_summary["resolved"] == 1
    assert failure_summary["items"] == []


def test_failures_default_and_all_filter_history(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    assert invalid_preview(project_dir).returncode != 0
    assert valid_preview(project_dir).returncode == 0

    active_result = run_cli("failures", project_dir, "--json")
    all_result = run_cli("failures", project_dir, "--all", "--json")

    assert active_result.returncode == 0, active_result.stderr
    active_summary = json_object(active_result.stdout)
    assert active_summary["active_count"] == 0
    assert active_summary["resolved_count"] == 1
    assert active_summary["total_count"] == 1
    assert active_summary["failures"] == []

    assert all_result.returncode == 0, all_result.stderr
    all_summary = json_object(all_result.stdout)
    assert len(record_list(all_summary, "failures")) == 1


def test_unrelated_active_failure_keeps_project_failed(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir, with_inputs=True)
    assert invalid_preview(project_dir).returncode != 0
    missing_visual = run_cli(
        "update-visual",
        project_dir,
        "visual_missing",
        "--params-json",
        '{"title":"Missing"}',
    )
    assert missing_visual.returncode != 0
    assert valid_preview(project_dir).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    status = json_object(result.stdout)
    assert status["state"] == "failed"
    failures = object_dict(status, "failures")
    assert failures["total"] == 2
    assert failures["active"] == 1
    assert failures["resolved"] == 1


def test_successful_add_visual_resolves_template_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    invalid = run_cli(
        "add-visual",
        project_dir,
        "--template",
        "simple_card",
        "--start",
        "1",
        "--end",
        "2",
        "--params-json",
        '{"subtitle":"Missing"}',
    )
    assert invalid.returncode != 0

    valid = add_visual(project_dir)

    assert valid.returncode == 0, valid.stderr
    failure = record_list(load_project(project_dir), "failures")[0]
    assert failure["status"] == "resolved"


def test_noop_update_resolves_matching_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    visual_id = visual_id_from(add_visual(project_dir))
    failed = run_cli("update-visual", project_dir, visual_id)
    assert failed.returncode != 0

    resolved = run_cli(
        "update-visual",
        project_dir,
        visual_id,
        "--params-json",
        '{"title":"Key idea"}',
        "--json",
    )

    assert resolved.returncode == 0, resolved.stderr
    summary = json_object(resolved.stdout)
    assert summary["changes"] == []
    failure = record_list(load_project(project_dir), "failures")[0]
    assert failure["status"] == "resolved"


def test_preview_visual_retry_resolves_matching_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    visual_id = visual_id_from(add_visual(project_dir))
    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    visual["params"] = {"subtitle": "Missing title"}
    write_project(project_dir, data)
    assert run_cli("preview-visual", project_dir, visual_id).returncode != 0

    correction = run_cli(
        "update-visual",
        project_dir,
        visual_id,
        "--params-json",
        '{"title":"Corrected"}',
    )
    retry = run_cli("preview-visual", project_dir, visual_id)

    assert correction.returncode == 0, correction.stderr
    assert retry.returncode == 0, retry.stderr
    failures = record_list(load_project(project_dir), "failures")
    preview_failure = next(item for item in failures if item["stage"] == "preview_visual")
    assert preview_failure["status"] == "resolved"


def test_legacy_failure_can_be_resolved_by_generated_id(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    data = load_project(project_dir)
    data["failures"] = [
        {
            "stage": "preview_visual",
            "visual_id": "visual_old",
            "errors": ["legacy failure"],
            "recommended_next_action": "Retry it.",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    write_project(project_dir, data)

    listed = run_cli("failures", project_dir, "--json")

    assert listed.returncode == 0, listed.stderr
    listed_failure = record_list(json_object(listed.stdout), "failures")[0]
    assert listed_failure["id"] == "failure_0001"
    assert listed_failure["status"] == "active"
    assert "id" not in record_list(load_project(project_dir), "failures")[0]

    resolved = run_cli("resolve-failure", project_dir, "failure_0001", "--json")
    repeated = run_cli("resolve-failure", project_dir, "failure_0001", "--json")

    assert resolved.returncode == 0, resolved.stderr
    assert json_object(resolved.stdout)["already_resolved"] is False
    assert repeated.returncode == 0, repeated.stderr
    assert json_object(repeated.stdout)["already_resolved"] is True
    stored = record_list(load_project(project_dir), "failures")[0]
    assert stored["id"] == "failure_0001"
    assert stored["status"] == "resolved"


def test_resolve_missing_failure_returns_nonzero_without_new_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli("resolve-failure", project_dir, "failure_9999", "--json")

    assert result.returncode != 0
    summary = json_object(result.stdout)
    assert summary["success"] is False
    assert load_project(project_dir)["failures"] == []


def invalid_preview(project_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "preview",
        project_dir,
        "--template",
        "simple_card",
        "--params-json",
        '{"subtitle":"Missing title"}',
    )


def valid_preview(project_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "preview",
        project_dir,
        "--template",
        "simple_card",
        "--params-json",
        '{"title":"Corrected title"}',
    )


def add_visual(project_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "add-visual",
        project_dir,
        "--template",
        "simple_card",
        "--start",
        "1",
        "--end",
        "2",
        "--params-json",
        '{"title":"Key idea"}',
        "--json",
    )


def visual_id_from(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    visual_id = json_object(result.stdout)["visual_id"]
    assert isinstance(visual_id, str)
    return visual_id


def json_object(raw_json: str) -> ProjectJson:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def object_dict(data: ProjectJson | Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: ProjectJson | Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    records: list[Record] = []
    for item in items:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records
