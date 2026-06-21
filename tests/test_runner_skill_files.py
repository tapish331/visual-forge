from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "visual-forge-runner"


def test_runner_skill_has_valid_frontmatter_and_references() -> None:
    skill_file = SKILL_DIR / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert "name: visual-forge-runner" in frontmatter
    assert "description:" in frontmatter
    assert "[TODO" not in text

    references = set(re.findall(r"references/[a-z0-9-]+\.md", text))
    assert references == {
        "references/project-workflow.md",
        "references/visual-planning.md",
        "references/correction-workflow.md",
        "references/rendering-rules.md",
        "references/failure-recovery.md",
        "references/template-contract.md",
        "references/capability-generation.md",
    }
    for reference in references:
        assert (SKILL_DIR / reference).is_file()


def test_runner_skill_prefers_compact_cli_over_large_artifacts() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "next <project_dir> --json" in text
    assert "status <project_dir> --json" in text
    assert "Do not read `project.json`, transcript artifacts, alignment artifacts, rendered media, or logs" in text
    assert "run at most one mutating checkpoint command" in text


def test_runner_skill_reports_resolved_paths() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    workflow_text = (SKILL_DIR / "references" / "project-workflow.md").read_text(encoding="utf-8")

    assert "use each item's `resolved_path`" in skill_text
    assert "Do not join logical `path` values to `project_dir`" in skill_text
    assert "outputs.final.resolved_path" in workflow_text
    assert "verification.final.resolved_path" in workflow_text


def test_runner_skill_documents_codex_intent_planning() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    workflow_text = (SKILL_DIR / "references" / "project-workflow.md").read_text(encoding="utf-8")
    visual_text = (SKILL_DIR / "references" / "visual-planning.md").read_text(encoding="utf-8")
    capability_text = (SKILL_DIR / "references" / "capability-generation.md").read_text(encoding="utf-8")

    assert "capability-generation.md" in skill_text
    assert "planning-context" in workflow_text
    assert "apply-visual-plan" in workflow_text
    assert "python -m app.main planning-context <project_dir> --chunk <chunk_id> --json" in visual_text
    assert "python -m app.main apply-visual-plan <project_dir> --chunk <chunk_id>" in visual_text
    assert "python -m app.main plan-visuals <project_dir> --chunk <chunk_id> --json" in visual_text
    assert "Do not substitute `simple_card` merely because it exists" in visual_text
    assert "scaffold-template" in capability_text
    assert "register-asset" in capability_text
    assert "bind-visual-intent" in capability_text
    assert "do not use paid or network generation services" in capability_text
