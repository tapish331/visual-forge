# Capability Generation

Use this reference only when a visual intent records a capability gap and the human authorizes creating the missing reusable capability.

## Offline Workflow

1. Inspect the exact gaps:

```powershell
python -m app.main visual-intents <project_dir> --chunk <chunk_id> --gaps-only --json
```

2. Scaffold one draft template for the intent type:

```powershell
python -m app.main scaffold-template <template_id> --capability <intent_type> --json
```

3. Design the template for this capability. Keep style inside the template. For normal production capabilities, prefer `OUTPUT_TYPE = "mp4"` and animated motion that satisfies the visual-plan contract. Create required PNG assets locally with code, Pillow, or human-supplied files under `assets/`; do not use paid or network generation services.

4. Register and validate each asset:

```powershell
python -m app.main register-asset assets/images/<asset>.png --id <asset_id> --version 1.0.0 --json
python -m app.main validate-asset <asset_id> --json
```

5. Return the asset IDs from `required_assets(params)`, finish rendering, set `TEMPLATE_STATUS = "ready"`, then validate and smoke-render:

```powershell
python -m app.main validate-template templates/<template_id>.py --json
python -m app.main render-template <template_id> <smoke_output.png> --params-json '<json>' --json
python -m app.main render-template <template_id> <smoke_output.mp4> --params-json '<json>' --duration-seconds 6 --json
```

6. Bind only the affected intent:

```powershell
python -m app.main bind-visual-intent <project_dir> <intent_id> --template <template_id> --params-json '<json>' --json
```

7. Run `next <project_dir> --json`. Stop before previewing when the human requested a planning-only or capability-only run.

Do not overwrite existing templates, substitute an unsuitable template, introduce central art direction, or modify unrelated intents. Use `--replace` on asset registration only when intentionally updating the same asset ID.
