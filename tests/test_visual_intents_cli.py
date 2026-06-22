from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

from app.artifacts import atomic_write_json, sha256_fingerprint, stat_fingerprint
from app.layout import layout_metadata
from app.project import JsonObject, JsonValue, ProjectState, build_initial_project, write_project
from app.render_freshness import build_visual_plan_fingerprint
from app.visual_intents import build_intent_id


REPO_ROOT = Path(__file__).resolve().parents[1]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VISUAL_FORGE_LOG_DISABLED"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
    )


def test_planning_context_is_compact_and_chunk_specific(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [
            aligned_block("block_001", 0.0, 8.0, "First meaningful visual planning statement."),
            aligned_block("block_002", 8.0, 16.0, "Second meaningful visual planning statement."),
        ],
        chunk_args=("--target-seconds", "8", "--min-seconds", "4", "--max-seconds", "10"),
    )

    result = run_cli("planning-context", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    context = json_object(result.stdout)
    assert context["success"] is True
    blocks = record_list(context, "blocks")
    assert [block["id"] for block in blocks] == ["block_001"]
    assert "segments" not in result.stdout
    assert "word_count" not in result.stdout
    templates = record_list(context, "templates")
    simple_card = next(item for item in templates if item["template_id"] == "simple_card")
    assert "key_point" in string_list(simple_card, "capabilities")


def test_planning_context_reads_layout_alignment_from_outputs(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "Layout-aware planning context remains compact.")],
        use_layout=True,
    )

    result = run_cli("planning-context", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    assert json_object(result.stdout)["success"] is True
    assert not (project_dir / "alignment" / "script_alignment.json").exists()


def test_bound_intent_creates_linked_visual_and_is_idempotent(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A key idea that should use the simple card capability.")],
    )
    plan = bound_plan("block_001", purpose="Emphasize the key claim.")

    first = run_cli("apply-visual-plan", project_dir, "--chunk", "chunk_001", "--plan-json", plan, "--json")
    second = run_cli("apply-visual-plan", project_dir, "--chunk", "chunk_001", "--plan-json", plan, "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json_object(second.stdout)["reused_existing"] is True
    project = load_project(project_dir)
    intents = record_list(project, "visual_intents")
    visuals = record_list(project, "visuals")
    assert len(intents) == 2
    assert len(visuals) == 2
    assert {intent["status"] for intent in intents} == {"bound"}
    for intent in intents:
        assert any(visual["id"] == intent["visual_id"] for visual in visuals)
    assert {visual["planner"] for visual in visuals} == {"codex_v1"}
    chunk = record_list(project, "chunks")[0]
    assert chunk["visual_mode"] == "visuals"
    assert chunk["status"] == "new"


def test_unbound_and_capability_gap_are_soft_planning_states(tmp_path: Path) -> None:
    unbound_project = prepare_project(
        tmp_path / "unbound",
        [aligned_block("block_001", 0.0, 12.0, "A key point can use an existing capability.")],
    )
    gap_project = prepare_project(
        tmp_path / "gap",
        [aligned_block("block_001", 0.0, 12.0, "A timeline map requires a new capability.")],
    )

    unbound = run_cli(
        "apply-visual-plan",
        unbound_project,
        "--chunk",
        "chunk_001",
        "--plan-json",
            unbound_plan("block_001", "animated_key_point"),
        "--json",
    )
    gap = run_cli(
        "apply-visual-plan",
        gap_project,
        "--chunk",
        "chunk_001",
        "--plan-json",
        unbound_plan("block_001", "timeline_map"),
        "--json",
    )

    assert unbound.returncode == 0, unbound.stderr
    assert json_object(unbound.stdout)["unbound_count"] == 1
    unbound_intent = next(
        intent for intent in record_list(load_project(unbound_project), "visual_intents")
        if intent["status"] == "unbound"
    )
    assert unbound_intent["candidate_template_ids"] == ["kinetic_text_beat"]
    assert gap.returncode == 0, gap.stderr
    assert json_object(gap.stdout)["capability_gap_count"] == 1
    gap_state = load_project(gap_project)
    assert record_list(gap_state, "failures") == []
    assert len(record_list(gap_state, "visuals")) == 1


def test_visual_intents_gaps_only_filters_results(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A missing timeline capability should be listed.")],
    )
    assert run_cli(
        "apply-visual-plan",
        project_dir,
        "--chunk",
        "chunk_001",
        "--plan-json",
            gap_plan("block_001", "timeline_map"),
    ).returncode == 0

    result = run_cli("visual-intents", project_dir, "--chunk", "chunk_001", "--gaps-only", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["total"] == 1
    assert record_list(summary, "intents")[0]["intent_type"] == "timeline_map"


def test_targeted_binding_preserves_unrelated_intents_and_manual_visuals(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A report and a key point need separate visual treatment.")],
    )
    plan = json.dumps(
        {
            "intents": [
                {
                    "intent_type": "animated_journey_map",
                    "purpose": "Map the creator's journey as a moving sequence.",
                    "visual_role": "context",
                    "motion": motion(),
                    "start": 0.0,
                    "end": 6.0,
                    "source_block_ids": ["block_001"],
                    "content": {"title": "Creator journey"},
                    "binding": None,
                },
                {
                    "intent_type": "animated_key_point",
                    "purpose": "Summarize the key point with motion.",
                    "visual_role": "emphasis",
                    "motion": motion(),
                    "start": 6.0,
                    "end": 12.0,
                    "source_block_ids": ["block_001"],
                    "content": {"title": "Key point"},
                    "binding": None,
                },
            ]
        }
    )
    applied = run_cli("apply-visual-plan", project_dir, "--chunk", "chunk_001", "--plan-json", plan, "--json")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    intent_id = next(
        str(intent["id"])
        for intent in record_list(load_project(project_dir), "visual_intents")
        if intent["intent_type"] == "animated_journey_map"
    )
    manual = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--start",
        "10",
        "--end",
        "11",
        "--params-json",
        '{"title":"Manual"}',
    )
    assert manual.returncode == 0, manual.stdout + manual.stderr

    first = run_cli(
        "bind-visual-intent",
        project_dir,
        intent_id,
        "--template",
        "animated_journey_map",
        "--params-json",
        '{"title":"Creator journey","stages":[{"label":"Start","detail":"The first step"},{"label":"Build","detail":"The focused work"}]}',
        "--json",
    )
    second = run_cli(
        "bind-visual-intent",
        project_dir,
        intent_id,
        "--template",
        "animated_journey_map",
        "--params-json",
        '{"title":"Creator journey","stages":[{"label":"Start","detail":"The first step"},{"label":"Build","detail":"The focused work"}]}',
        "--json",
    )

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert json_object(second.stdout)["reused_existing"] is True
    state = load_project(project_dir)
    intents = record_list(state, "visual_intents")
    assert sorted(str(intent["status"]) for intent in intents) == ["bound", "unbound"]
    visuals = record_list(state, "visuals")
    assert len(visuals) == 2
    assert any(visual.get("planner") == "codex_v1" and visual.get("intent_id") == intent_id for visual in visuals)
    assert any(visual.get("planner") is None for visual in visuals)


def test_targeted_binding_failure_deduplicates_and_success_resolves(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A newspaper report can use the registered capability.")],
    )
    assert run_cli(
        "apply-visual-plan",
        project_dir,
        "--chunk",
        "chunk_001",
        "--plan-json",
        unbound_plan("block_001", "animated_journey_map"),
    ).returncode == 0
    intent_id = next(
        str(intent["id"])
        for intent in record_list(load_project(project_dir), "visual_intents")
        if intent["intent_type"] == "animated_journey_map"
    )

    first = run_cli(
        "bind-visual-intent",
        project_dir,
        intent_id,
        "--template",
        "animated_journey_map",
        "--params-json",
        "{}",
        "--json",
    )
    second = run_cli(
        "bind-visual-intent",
        project_dir,
        intent_id,
        "--template",
        "animated_journey_map",
        "--params-json",
        "{}",
        "--json",
    )
    assert first.returncode == 1
    assert second.returncode == 1
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "visual_intent_bind"
    assert failures[0]["attempt_count"] == 2

    success = run_cli(
        "bind-visual-intent",
        project_dir,
        intent_id,
        "--template",
        "animated_journey_map",
        "--params-json",
        '{"title":"Creator journey","stages":[{"label":"Start","detail":"The first step"},{"label":"Build","detail":"The focused work"}]}',
        "--json",
    )

    assert success.returncode == 0, success.stdout + success.stderr
    resolved = record_list(load_project(project_dir), "failures")[0]
    assert resolved["status"] == "resolved"


def test_invalid_plan_and_mixed_ownership_fail_without_replacing_visual_records(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A valid block for ownership validation.")],
    )
    manual = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        "chunk_001",
        "--template",
        "simple_card",
        "--start",
        "2",
        "--end",
        "5",
        "--params-json",
        '{"title":"Manual"}',
    )
    assert manual.returncode == 0, manual.stderr
    before = record_list(load_project(project_dir), "visuals")

    mixed = run_cli(
        "apply-visual-plan",
        project_dir,
        "--chunk",
        "chunk_001",
        "--plan-json",
        bound_plan("block_001"),
        "--json",
    )

    assert mixed.returncode == 1
    assert "will not overwrite" in mixed.stdout
    assert record_list(load_project(project_dir), "visuals") == before


def test_invalid_timing_and_binding_fail_atomically(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A valid block for atomic plan validation.")],
    )
    invalid = json.dumps(
        {
            "intents": [
                {
                    "intent_type": "newspaper_headline",
                    "purpose": "Invalid binding and timing.",
                    "start": 20.0,
                    "end": 10.0,
                    "source_block_ids": ["block_missing"],
                    "content": {"headline": "Invalid"},
                    "binding": {"template_ref": "simple_card", "params": {"title": "Invalid"}},
                }
            ]
        }
    )

    result = run_cli("apply-visual-plan", project_dir, "--chunk", "chunk_001", "--plan-json", invalid, "--json")

    assert result.returncode == 1
    project = load_project(project_dir)
    assert record_list(project, "visual_intents") == []
    assert record_list(project, "visuals") == []
    assert record_list(project, "failures")[0]["stage"] == "visual_intent_plan"


def test_changed_intent_changes_visual_plan_fingerprint(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 12.0, "A bound visual whose purpose later changes.")],
    )
    assert run_cli(
        "apply-visual-plan",
        project_dir,
        "--chunk",
        "chunk_001",
        "--plan-json",
        bound_plan("block_001", purpose="First purpose."),
    ).returncode == 0
    first_state = cast(ProjectState, load_project(project_dir))
    first = build_visual_plan_fingerprint(first_state, "chunk_001")
    assert first is not None

    assert run_cli(
        "apply-visual-plan",
        project_dir,
        "--chunk",
        "chunk_001",
        "--plan-json",
        bound_plan("block_001", purpose="Changed purpose."),
    ).returncode == 0
    second_state = cast(ProjectState, load_project(project_dir))
    second = build_visual_plan_fingerprint(second_state, "chunk_001")

    assert second is not None
    assert first != second


def test_next_routes_context_unbound_gap_and_bound_states(tmp_path: Path) -> None:
    context_project = prepare_project(
        tmp_path / "context",
        [aligned_block("block_001", 0.0, 12.0, "Context routing starts before intent creation.")],
    )
    context_next = json_object(run_cli("next", context_project, "--json").stdout)
    assert context_next["recommended_command"] == [
        "python", "-m", "app.main", "planning-context", str(context_project), "--chunk", "chunk_001", "--json"
    ]

    assert run_cli(
        "apply-visual-plan",
        context_project,
        "--chunk",
        "chunk_001",
        "--plan-json",
            unbound_plan("block_001", "animated_key_point"),
    ).returncode == 0
    unbound_next = json_object(run_cli("next", context_project, "--json").stdout)
    assert "visual-intents" in cast(list[object], unbound_next["recommended_command"])

    gap_project = prepare_project(
        tmp_path / "gap",
        [aligned_block("block_001", 0.0, 12.0, "Gap routing names the missing capability.")],
    )
    assert run_cli(
        "apply-visual-plan",
        gap_project,
        "--chunk",
        "chunk_001",
        "--plan-json",
        unbound_plan("block_001", "timeline_map"),
    ).returncode == 0
    gap_next = json_object(run_cli("next", gap_project, "--json").stdout)
    assert gap_next["human_input_required"] is True
    assert "timeline_map" in cast(str, gap_next["recommended_action"])

    bound_project = prepare_project(
        tmp_path / "bound",
        [aligned_block("block_001", 0.0, 12.0, "Bound routing advances to preview.")],
    )
    assert run_cli(
        "apply-visual-plan",
        bound_project,
        "--chunk",
        "chunk_001",
        "--plan-json",
        bound_plan("block_001"),
    ).returncode == 0
    bound_next = json_object(run_cli("next", bound_project, "--json").stdout)
    assert "preview" in cast(list[object], bound_next["recommended_command"])


def test_visual_plan_review_rejects_sparse_static_plan(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 145.0, "A long chunk needs dense animated visual coverage.")],
    )
    project = load_project(project_dir)
    project["visual_intents"] = [
        quality_intent(index, start, start + 5.0, "title_card", "simple_card")
        for index, start in enumerate((20.0, 45.0, 80.0, 120.0), start=1)
    ]
    write_project(project_dir / "project.json", cast(ProjectState, project))

    result = run_cli("visual-plan-review", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 1
    review = json_object(result.stdout)
    assert review["passed"] is False
    messages = [str(check["message"]) for check in record_list(review, "checks") if check.get("passed") is False]
    assert any("needs at least 15 visuals" in message for message in messages)
    assert any("must start with a visual" in message for message in messages)
    assert any("static/non-MP4" in message for message in messages)


def test_visual_plan_review_accepts_dense_animated_plan(tmp_path: Path) -> None:
    project_dir = prepare_project(
        tmp_path,
        [aligned_block("block_001", 0.0, 145.0, "A long chunk can pass with dense animated coverage.")],
    )
    project = load_project(project_dir)
    intent_types = [
        "animated_opening_hook",
        "kinetic_text_beat",
        "animated_journey_map",
        "timeline_motion",
        "evidence_reveal",
        "contrast_scene",
        "recap_motion",
    ]
    templates = ["animated_opening_hook", "kinetic_text_beat", "animated_journey_map"]
    intents: list[Record] = []
    visual_count = 20
    for index in range(visual_count):
        start = round(index * 145.0 / visual_count, 3)
        end = round((index + 1) * 145.0 / visual_count, 3)
        intents.append(
            quality_intent(
                index + 1,
                start,
                end,
                intent_types[index % len(intent_types)],
                templates[index % len(templates)],
            )
        )
    project["visual_intents"] = intents
    write_project(project_dir / "project.json", cast(ProjectState, project))

    result = run_cli("visual-plan-review", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    review = json_object(result.stdout)
    assert review["passed"] is True
    counts = object_dict(review, "counts")
    assert counts["intent_count"] == 20
    assert counts["distinct_intent_types"] == 7
    animated_checks = [check for check in record_list(review, "checks") if check.get("id") == "animated_binding"]
    assert animated_checks
    assert all(check.get("passed") is True for check in animated_checks)
    assert all("animated MP4 template" in str(check["message"]) for check in animated_checks)
    assert all("static/non-MP4" not in str(check["message"]) for check in animated_checks)


def test_intent_id_excludes_timing_and_binding() -> None:
    first = build_intent_id("chunk_001", "key_point", "Purpose", {"title": "Key"}, None, ["block_001"])
    second = build_intent_id("chunk_001", "key_point", "Purpose", {"title": "Key"}, None, ["block_001"])
    assert first == second


def quality_intent(
    index: int,
    start: float,
    end: float,
    intent_type: str,
    template_ref: str,
    *,
    output_motion: bool = True,
) -> Record:
    motion_record = motion() if output_motion else None
    binding: Record = {
        "template_ref": template_ref,
        "template_id": template_ref,
        "params": {"title": f"Visual {index}", "text": f"Visual {index}"},
    }
    return {
        "id": f"intent_quality_{index:03d}",
        "chunk_id": "chunk_001",
        "start": start,
        "end": end,
        "source_block_ids": ["block_001"],
        "intent_type": intent_type,
        "purpose": f"Quality review visual {index}.",
        "content": {"title": f"Visual {index}"},
        "style_notes": None,
        "visual_role": "hook" if index == 1 else "emphasis",
        "motion": motion_record,
        "status": "bound",
        "candidate_template_ids": [template_ref],
        "binding": binding,
        "visual_id": f"visual_quality_{index:03d}",
        "planner": "codex_v1",
        "created_at": "2026-06-20T00:00:00Z",
        "updated_at": "2026-06-20T00:00:00Z",
    }


def prepare_project(
    tmp_path: Path,
    blocks: list[Record],
    *,
    chunk_args: tuple[str, ...] = (),
    use_layout: bool = False,
) -> Path:
    if use_layout:
        input_dir = tmp_path / "inputs" / "my-video"
        project_dir = tmp_path / "projects" / "my-video"
        artifact_root = tmp_path / "outputs" / "my-video"
        input_dir.mkdir(parents=True)
        project_dir.mkdir(parents=True)
        artifact_root.mkdir(parents=True)
        script_file = input_dir / "script.txt"
        raw_file = input_dir / "raw.mp4"
        layout = layout_metadata(project_dir, slug="my-video", input_dir=input_dir, outputs_dir=artifact_root)
        state = build_initial_project(project_dir, script_source=script_file, video_source=raw_file, layout=layout)
    else:
        project_dir = tmp_path / "my-video"
        project_dir.mkdir(parents=True)
        artifact_root = project_dir
        script_file = project_dir / "script.txt"
        raw_file = project_dir / "raw.mp4"
        state = build_initial_project(project_dir)

    script_file.write_text("Script.\n", encoding="utf-8")
    raw_file.write_bytes(b"raw video")
    audio_file = artifact_root / "audio" / "narration.wav"
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"narration audio")
    transcript_file = artifact_root / "transcripts" / "narration.json"
    atomic_write_json(transcript_file, {"schema_version": 1, "text": "Script.", "segments": []})
    alignment_file = artifact_root / "alignment" / "script_alignment.json"
    atomic_write_json(
        alignment_file,
        {
            "schema_version": 1,
            "source_script": state["project"]["script"],
            "source_transcript": "transcripts/narration.json",
            "method": "sequence_matcher_words_v1",
            "blocks": records_json(blocks),
        },
    )

    raw_fingerprint = stat_fingerprint(raw_file)
    audio_fingerprint = stat_fingerprint(audio_file)
    transcript_fingerprint = sha256_fingerprint(transcript_file)
    state["media"] = {
        "raw": {"path": state["project"]["video"], "duration_seconds": alignment_duration(blocks), "source_fingerprint": raw_fingerprint},
        "audio": {"narration": {"path": "audio/narration.wav", "source_fingerprint": raw_fingerprint, "artifact_fingerprint": audio_fingerprint}},
    }
    state["transcript"] = {
        "narration": {"path": "transcripts/narration.json", "status": "transcribed", "source_fingerprint": audio_fingerprint, "artifact_fingerprint": transcript_fingerprint}
    }
    state["alignment"] = {
        "script": {
            "path": "alignment/script_alignment.json",
            "status": "aligned",
            "blocks": len(blocks),
            "aligned_blocks": len(blocks),
            "source_fingerprints": {
                "script_sha256": digest_value(sha256_fingerprint(script_file)),
                "transcript_sha256": digest_value(transcript_fingerprint),
            },
            "artifact_fingerprint": sha256_fingerprint(alignment_file),
        }
    }
    write_project(project_dir / "project.json", state)
    result = run_cli("create-chunks", project_dir, *chunk_args)
    assert result.returncode == 0, result.stdout + result.stderr
    return project_dir


def aligned_block(block_id: str, start: float, end: float, text: str) -> Record:
    return {"id": block_id, "status": "aligned", "start": start, "end": end, "text": text}


def bound_plan(block_id: str, *, purpose: str = "Emphasize the key claim.") -> str:
    return json.dumps(
        {
            "intents": [
                {
                    "intent_type": "animated_opening_hook",
                    "purpose": purpose,
                    "visual_role": "hook",
                    "motion": motion(),
                    "start": 0.0,
                    "end": 6.0,
                    "source_block_ids": [block_id],
                    "content": {"title": "Key idea"},
                    "style_notes": None,
                    "binding": {"template_ref": "animated_opening_hook", "params": {"title": "Key idea"}},
                },
                {
                    "intent_type": "kinetic_text_beat",
                    "purpose": "Carry the second animated beat.",
                    "visual_role": "emphasis",
                    "motion": motion(),
                    "start": 6.0,
                    "end": 12.0,
                    "source_block_ids": [block_id],
                    "content": {"text": "Second idea"},
                    "style_notes": None,
                    "binding": {"template_ref": "kinetic_text_beat", "params": {"text": "Second idea"}},
                },
            ]
        }
    )


def unbound_plan(block_id: str, intent_type: str) -> str:
    return json.dumps(
        {
            "intents": [
                {
                    "intent_type": "animated_opening_hook",
                    "purpose": "Open the chunk with immediate motion.",
                    "visual_role": "hook",
                    "motion": motion(),
                    "start": 0.0,
                    "end": 6.0,
                    "source_block_ids": [block_id],
                    "content": {"title": "Key idea"},
                    "style_notes": None,
                    "binding": {"template_ref": "animated_opening_hook", "params": {"title": "Key idea"}},
                },
                {
                    "intent_type": intent_type,
                    "purpose": "Show the idea with an appropriate visual capability.",
                    "visual_role": "emphasis",
                    "motion": motion(),
                    "start": 6.0,
                    "end": 12.0,
                    "source_block_ids": [block_id],
                    "content": {"title": "Key idea"},
                    "style_notes": None,
                    "binding": None,
                },
            ]
        }
    )


def gap_plan(block_id: str, intent_type: str) -> str:
    return unbound_plan(block_id, intent_type)


def motion() -> Record:
    return {
        "preferred_output_type": "mp4",
        "beats": [0.0, 3.0, 6.0],
        "transition_in": "cut",
        "transition_out": "fade",
        "animation_notes": "Animate text and layout elements over the beat.",
    }


def alignment_duration(blocks: list[Record]) -> float:
    return max(float(cast(int | float, block["end"])) for block in blocks)


def records_json(records: list[Record]) -> list[JsonValue]:
    return [cast(JsonValue, record) for record in records]


def digest_value(fingerprint: JsonObject) -> str:
    value = fingerprint.get("sha256")
    assert isinstance(value, str)
    return value


def load_project(project_dir: Path) -> Record:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(Record, raw)


def json_object(raw_json: str) -> Record:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(Record, raw)


def record_list(data: Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    return [cast(Record, item) for item in value if isinstance(item, dict)]


def string_list(data: Record, key: str) -> list[str]:
    value = data[key]
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return cast(list[str], value)


def object_dict(data: Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)
