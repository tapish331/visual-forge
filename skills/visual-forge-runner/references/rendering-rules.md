# Rendering Rules

Use this reference for chunk preview, chunk render, final composition, and final verification.

## Chunk Render Loop

For visual chunks:

```powershell
python -m app.main visual-plan-review <project_dir> --chunk <chunk_id> --json
python -m app.main preview <project_dir> --chunk <chunk_id> --json
python -m app.main render-chunk <project_dir> <chunk_id> --json
```

Normal Codex-authored visual chunks should pass `visual-plan-review` before preview. Static PNG visuals remain valid for manual or legacy plans, but production Codex plans should bind animated MP4 templates unless the human explicitly asks for a legacy/static treatment.

For camera-only chunks:

```powershell
python -m app.main approve-camera-only <project_dir> <chunk_id> --json
python -m app.main render-chunk <project_dir> <chunk_id> --json
```

Do not render undecided chunks.

## Final Output

Compose only when `next --json` or `status --json` reports `ready_for_final`:

```powershell
python -m app.main final <project_dir> --json
```

Verify only when state is `ready_for_verification`:

```powershell
python -m app.main verify-final <project_dir> --json
```

`complete` means mechanical verification passed. It does not mean the human liked the result.

## Stale Outputs

Stale previews, chunk renders, final videos, and verification reports are retained. Refresh the earliest stale checkpoint instead of deleting generated files.
