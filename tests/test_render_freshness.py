from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

from app.assets import register_asset
from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.project import JsonObject, ProjectState, build_initial_project, write_project
from app.render_freshness import (
    build_chunk_render_freshness,
    build_preview_provenance,
    build_visual_plan_fingerprint,
)
from app.status import build_status
from app.timeline import (
    build_chunking_metadata,
    build_full_raw_timeline,
    build_timeline_fingerprint,
    current_chunk_plan_fingerprint,
)


def test_template_change_makes_preview_and_chunk_render_stale(tmp_path: Path) -> None:
    project_dir, state, template_file, _, _ = prepared_complete_project(tmp_path)

    assert build_chunk_render_freshness(project_dir, state, "chunk_001")["state"] == "current"

    template_file.write_text(template_file.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    result = build_chunk_render_freshness(project_dir, state, "chunk_001")
    assert result == {"state": "stale", "reason": "upstream_stale"}


def test_preview_change_or_deletion_makes_chunk_render_stale(tmp_path: Path) -> None:
    project_dir, state, _, preview_file, _ = prepared_complete_project(tmp_path)

    preview_file.write_bytes(b"modified preview")
    assert build_chunk_render_freshness(project_dir, state, "chunk_001") == {
        "state": "stale",
        "reason": "upstream_stale",
    }

    preview_file.unlink()
    assert build_chunk_render_freshness(project_dir, state, "chunk_001") == {
        "state": "stale",
        "reason": "upstream_stale",
    }


def test_asset_change_makes_preview_chunk_final_and_verification_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, state, template_file, _, _ = prepared_complete_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assets_dir = Path("assets")
    asset_file = assets_dir / "images" / "paper.png"
    asset_file.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), "white").save(asset_file, format="PNG")
    assert register_asset(asset_file, "paper_base", "1.0.0", assets_dir=assets_dir)["success"] is True
    write_template(template_file, required_assets="['paper_base']")
    provenance = build_preview_provenance(
        project_dir,
        state,
        str(template_file),
        Path("previews/preview_001.png"),
        {"title": "Hello"},
        assets_dir=assets_dir,
    )
    previews = state.get("previews")
    assert previews is not None
    previews[0].update(provenance)
    render_metadata(state)["preview_fingerprints"] = {"preview_001": provenance["artifact_fingerprint"]}
    write_project(project_dir / "project.json", state)
    assert build_chunk_render_freshness(project_dir, state, "chunk_001", assets_dir)["state"] == "current"

    Image.new("RGB", (32, 32), "black").save(asset_file, format="PNG")

    assert build_chunk_render_freshness(project_dir, state, "chunk_001", assets_dir) == {
        "state": "stale",
        "reason": "upstream_stale",
    }
    summary = build_status(project_dir)
    assert summary["state"] == "in_progress"
    assert summary["outputs"]["final"].get("current") is False
    assert summary["verification"]["final"]["current"] is False
    assert state["failures"] == []

def test_visual_plan_change_makes_chunk_render_stale(tmp_path: Path) -> None:
    project_dir, state, _, _, _ = prepared_complete_project(tmp_path)
    visuals = state.get("visuals")
    assert visuals is not None
    visuals[0]["params"] = {"title": "Changed"}

    result = build_chunk_render_freshness(project_dir, state, "chunk_001")

    assert result == {"state": "stale", "reason": "upstream_stale"}


def test_intent_provenance_preserves_legacy_hashes_and_tracks_codex_plans(tmp_path: Path) -> None:
    _, state, _, _, _ = prepared_complete_project(tmp_path)
    visuals = state.get("visuals")
    assert visuals is not None
    baseline = build_visual_plan_fingerprint(state, "chunk_001")

    visuals[0]["planner"] = "auto_v0"
    assert build_visual_plan_fingerprint(state, "chunk_001") == baseline

    visuals[0]["planner"] = "codex_v1"
    visuals[0]["intent_id"] = "intent_001"
    assert build_visual_plan_fingerprint(state, "chunk_001") != baseline


def test_legacy_render_dependencies_are_unverified(tmp_path: Path) -> None:
    project_dir, state, _, _, _ = prepared_complete_project(tmp_path)
    previews = state.get("previews")
    assert previews is not None
    preview = previews[0]
    preview.pop("template_fingerprint")
    render = render_metadata(state)
    render.pop("visual_plan_fingerprint")

    result = build_chunk_render_freshness(project_dir, state, "chunk_001")

    assert result == {"state": "unverified", "reason": "fingerprint_missing"}


def test_status_rolls_back_complete_project_without_creating_failure(tmp_path: Path) -> None:
    project_dir, state, _, preview_file, _ = prepared_complete_project(tmp_path)
    write_project(project_dir / "project.json", state)
    assert build_status(project_dir)["state"] == "complete"

    preview_file.write_bytes(b"modified preview")
    summary = build_status(project_dir)

    assert summary["state"] == "in_progress"
    assert summary["chunks"]["render_freshness"] == {
        "current": 0,
        "stale": 1,
        "unverified": 0,
        "missing": 0,
        "not_created": 0,
    }
    assert summary["outputs"]["final"].get("current") is False
    assert summary["verification"]["final"]["current"] is False
    assert summary["next_action"] == "Preview visuals for each changed chunk."
    assert state["failures"] == []


def test_status_rolls_back_complete_project_when_raw_or_alignment_changes(tmp_path: Path) -> None:
    project_dir, state, _, _, _ = prepared_complete_project(tmp_path)
    write_project(project_dir / "project.json", state)

    (project_dir / "raw.mp4").write_bytes(b"changed raw video")
    assert build_status(project_dir)["state"] == "ready_for_probe"

    alignment_case = tmp_path / "alignment-case"
    alignment_case.mkdir()
    project_dir, state, _, _, _ = prepared_complete_project(alignment_case)
    write_project(project_dir / "project.json", state)
    (project_dir / "script.txt").write_text("Changed script.\n", encoding="utf-8")
    assert build_status(project_dir)["state"] == "ready_for_alignment"


def test_chunk_record_change_rolls_status_back_to_chunking(tmp_path: Path) -> None:
    project_dir, state, _, _, _ = prepared_complete_project(tmp_path)
    state["chunks"][0]["end"] = 9.0
    write_project(project_dir / "project.json", state)

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_chunks"
    freshness = summary["chunking"].get("freshness")
    assert isinstance(freshness, dict)
    assert freshness.get("state") == "stale"


def test_legacy_chunks_without_provenance_are_unverified(tmp_path: Path) -> None:
    project_dir, state, _, _, _ = prepared_complete_project(tmp_path)
    state.pop("timeline")
    state.pop("chunking")
    write_project(project_dir / "project.json", state)

    summary = build_status(project_dir)

    assert summary["state"] == "ready_for_chunks"
    freshness = summary["chunking"].get("freshness")
    assert isinstance(freshness, dict)
    assert freshness.get("state") == "unverified"


def prepared_complete_project(
    tmp_path: Path,
) -> tuple[Path, ProjectState, Path, Path, Path]:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    script_file = project_dir / "script.txt"
    raw_file = project_dir / "raw.mp4"
    audio_file = project_dir / "audio" / "narration.wav"
    transcript_file = project_dir / "transcripts" / "narration.json"
    alignment_file = project_dir / "alignment" / "script_alignment.json"
    preview_file = project_dir / "previews" / "preview_001.png"
    chunk_file = project_dir / "renders" / "chunks" / "chunk_001.mp4"
    final_file = project_dir / "final.mp4"
    report_file = project_dir / "verification" / "final.json"
    template_file = tmp_path / "template.py"

    script_file.write_text("Hello world.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"audio")
    atomic_write_json(transcript_file, {"schema_version": 1, "text": "Hello world.", "segments": []})
    atomic_write_json(alignment_file, {"schema_version": 1, "blocks": []})
    preview_file.parent.mkdir(parents=True)
    preview_file.write_bytes(b"preview")
    chunk_file.parent.mkdir(parents=True)
    chunk_file.write_bytes(b"chunk")
    final_file.write_bytes(b"final")
    atomic_write_json(report_file, {"schema_version": 1, "passed": True})
    write_template(template_file)

    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    state = build_initial_project(project_dir)
    state["media"] = {
        "raw": {
            "path": "raw.mp4",
            "duration_seconds": 10.0,
            "source_fingerprint": raw_fingerprint,
        },
        "audio": {
            "narration": {
                "path": "audio/narration.wav",
                "source_fingerprint": raw_fingerprint,
                "artifact_fingerprint": audio_fingerprint,
            }
        },
    }
    state["transcript"] = {
        "narration": {
            "path": "transcripts/narration.json",
            "source_fingerprint": audio_fingerprint,
            "artifact_fingerprint": transcript_fingerprint,
        }
    }
    state["alignment"] = {
        "script": {
            "path": "alignment/script_alignment.json",
            "source_fingerprints": {
                "script_sha256": digest(sha256_fingerprint(script_file)),
                "transcript_sha256": digest(transcript_fingerprint),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }
    state["chunks"] = [
        {
            "id": "chunk_001",
            "start": 0.0,
            "end": 10.0,
            "status": "rendered",
            "visual_mode": "visuals",
            "alignment_block_ids": [],
            "warning_block_ids": [],
        }
    ]
    state["visuals"] = [
        {
            "id": "visual_001",
            "chunk_id": "chunk_001",
            "template_ref": str(template_file),
            "template_id": "test_template",
            "params": {"title": "Hello"},
            "start": 1.0,
            "end": 3.0,
            "status": "previewed",
            "preview_id": "preview_001",
        }
    ]
    provenance = build_preview_provenance(project_dir, state, str(template_file), Path("previews/preview_001.png"))
    state["previews"] = [
        {
            "id": "preview_001",
            "template_ref": str(template_file),
            "template_id": "test_template",
            "params": {"title": "Hello"},
            "output": "previews/preview_001.png",
            "status": "rendered",
            **provenance,
        }
    ]
    plan_fingerprint = build_visual_plan_fingerprint(state, "chunk_001")
    assert plan_fingerprint is not None
    timeline = build_full_raw_timeline(project_dir, state)
    options: JsonObject = {"target_seconds": 180.0, "min_seconds": 90.0, "max_seconds": 240.0}
    state["timeline"] = timeline
    state["chunking"] = build_chunking_metadata(state, timeline, state["chunks"], options)
    chunk_plan_fingerprint = current_chunk_plan_fingerprint(state)
    assert chunk_plan_fingerprint is not None
    preview_fingerprint = provenance["artifact_fingerprint"]
    chunk_fingerprint = stat_fingerprint(chunk_file)
    state["renders"] = {
        "chunks": {
            "chunk_001": {
                "path": "renders/chunks/chunk_001.mp4",
                "chunk_id": "chunk_001",
                "source_fingerprint": raw_fingerprint,
                "status": "rendered",
                "duration_seconds": 10.0,
                "visual_plan_fingerprint": plan_fingerprint,
                "chunk_plan_fingerprint": chunk_plan_fingerprint,
                "preview_fingerprints": {"preview_001": preview_fingerprint},
                "artifact_fingerprint": chunk_fingerprint,
            }
        },
        "final": {
            "path": "final.mp4",
            "status": "rendered",
            "chunk_ids": ["chunk_001"],
            "chunk_paths": ["renders/chunks/chunk_001.mp4"],
            "duration_seconds": 10.0,
            "source_fingerprints": {"chunk_001": chunk_fingerprint},
            "timeline_fingerprint": build_timeline_fingerprint(timeline),
            "chunk_plan_fingerprint": chunk_plan_fingerprint,
            "artifact_fingerprint": stat_fingerprint(final_file),
        },
    }
    state["verification"] = {
        "final": {
            "path": "verification/final.json",
            "source": "final.mp4",
            "status": "passed",
            "source_fingerprint": stat_fingerprint(final_file),
            "artifact_fingerprint": sha256_fingerprint(report_file),
        }
    }
    return project_dir, state, template_file, preview_file, chunk_file


def render_metadata(state: ProjectState) -> JsonObject:
    renders = state.get("renders")
    assert isinstance(renders, dict)
    chunks = renders["chunks"]
    assert isinstance(chunks, dict)
    metadata = chunks["chunk_001"]
    assert isinstance(metadata, dict)
    return metadata


def digest(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value


def write_template(path: Path, *, required_assets: str = "[]") -> None:
    path.write_text(
        "\n".join(
            [
                "TEMPLATE_ID = 'test_template'",
                "TEMPLATE_VERSION = '1.0.0'",
                "OUTPUT_TYPE = 'png'",
                "def metadata(): return {}",
                "def validate_params(params): return []",
                f"def required_assets(params): return {required_assets}",
                "def render(params, output_path): pass",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
