# Correction Workflow

Use this reference when the human says something is wrong.

## Visual Corrections

Use `update-visual` for template, params, timing, or chunk assignment changes:

```powershell
python -m app.main update-visual <project_dir> <visual_id> --params-json '<json>' --json
python -m app.main update-visual <project_dir> <visual_id> --start <seconds> --end <seconds> --json
python -m app.main update-visual <project_dir> <visual_id> --chunk <chunk_id> --json
```

Then preview the affected chunk again:

```powershell
python -m app.main preview <project_dir> --chunk <chunk_id> --json
```

## Source Corrections

If the human edits `script.txt` or the referenced script file, run:

```powershell
python -m app.main status <project_dir> --json
python -m app.main align <project_dir> --json
python -m app.main create-chunks <project_dir> --force --json
```

Use `create-chunks --force` only after checking that existing visuals remain valid. The command fails atomically if visuals would become orphaned or out of range.

## Alignment Review

Use:

```powershell
python -m app.main alignment-review <project_dir> --json
```

Treat `needs_review` and `unmatched` as human quality warnings, not automatic failures.
