---
name: visual-forge-runner
description: Operate Visual Forge YouTube video projects through one token-efficient runner. Use when the user asks to start, continue, inspect, preview, render, verify, fix, or recover a Visual Forge project, especially paths like projects/my-video, inputs/my-video, outputs/my-video, project.json, chunks, visuals, templates, or final.mp4.
---

# Visual Forge Runner

## Operating Rule

Start with compact project state:

```powershell
python -m app.main next <project_dir> --json
python -m app.main status <project_dir> --json
```

Prefer `next --json` for action selection and `status --json` for reporting. Do not read `project.json`, transcript artifacts, alignment artifacts, rendered media, or logs unless a compact command points to a specific reason.

When reporting files from `status --json`, use each item's `resolved_path` when present. Do not join logical `path` values to `project_dir`; layout V1 projects store generated artifacts under `outputs/<slug>/`, not under `projects/<slug>/`.

For an ordinary "continue" request, run at most one mutating checkpoint command, then report:

- command run
- success/failure
- changed output path or project state
- next recommended action

Run multiple mutating commands only when the human explicitly asks for a longer pipeline run.

## Routing

Read only the reference needed for the current task:

- Start, continue, status, or checkpoint selection: `references/project-workflow.md`
- Add, inspect, preview, or update visual plans: `references/visual-planning.md`
- Correct mistakes or rerun stale work: `references/correction-workflow.md`
- Render chunks, compose final, or verify output: `references/rendering-rules.md`
- Inspect or resolve failures: `references/failure-recovery.md`
- Validate reusable templates: `references/template-contract.md`
- Materialize a capability gap with local templates and assets: `references/capability-generation.md`

Stop for human input when `next --json` reports `human_input_required: true` and the requested work does not authorize capability creation, when visual choices need human judgment, or when a command returns active failures. When the human asks to resolve a capability gap, follow `capability-generation.md`.

## Command Discipline

Use the Python CLI as the execution layer. Commands that mutate a project are already protected by project locks and bounded logs.

Use `--json` when deciding what to do next. Use human-readable output only when reporting to the human or when the human asks for it.

Do not invent project state. If a command fails, inspect:

```powershell
python -m app.main failures <project_dir> --json
python -m app.main next <project_dir> --json
```

Then apply the narrowest correction.
