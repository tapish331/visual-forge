# Project Workflow

Use this reference for starting, continuing, or reporting project progress.

## First Commands

For an existing project:

```powershell
python -m app.main next <project_dir> --json
python -m app.main status <project_dir> --json
```

For a new input folder:

```powershell
python -m app.main init-from-input <input_dir> --json
```

## Next-Step Contract

`next --json` is read-only. Trust its `recommended_command` when `human_input_required` is false.

If `human_input_required` is true:

- `missing_inputs`: tell the human which files are missing.
- `in_progress` with a capability gap: report it, or read `capability-generation.md` when the human asked to create missing capabilities.
- `in_progress` after heuristic fallback found no candidates: ask the human to add visuals or approve camera-only.
- `complete`: tell the human to review the final video.

## State Map

Run the recommended command for these states:

```text
ready_for_probe          -> probe
ready_for_audio          -> extract-audio
ready_for_transcription  -> transcribe
ready_for_alignment      -> align
ready_for_chunks         -> create-chunks
ready_for_final          -> final
ready_for_verification   -> verify-final
failed                   -> failures --json
```

For `in_progress`, operate on the earliest chunk needing work:

- `new` + `visuals`: `preview <project> --chunk <chunk_id> --json`
- `new` + no intents: run `planning-context`, author intents, then run `apply-visual-plan`
- `new` + unbound intents: inspect `visual-intents` and reapply with suitable bindings
- `new` + capability gaps: report the missing capability and route to `capability-generation.md`
- `new` + planner `no_candidates`: stop for manual visuals or camera-only approval
- `previewed`: `render-chunk <project> <chunk_id> --json`
- stale rendered visual chunk: refresh `preview --chunk`, then render later

## Reporting

Report the command, result, key output path, state, failures count, and next action. Keep output concise.

Use `resolved_path` from `status --json` for file locations. Logical `path` values describe project metadata, while `resolved_path` is the location to show the human. In layout V1 projects, generated artifacts resolve under `outputs/<slug>/`; never report them by joining `path` to `project_dir`.

For `complete`, report the final video and verification report from:

```text
outputs.final.resolved_path
verification.final.resolved_path
```
