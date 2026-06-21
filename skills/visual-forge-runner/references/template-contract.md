# Template Contract

Use this reference when inspecting or creating reusable visual templates.

## Commands

List templates:

```powershell
python -m app.main templates --json
```

Validate one template:

```powershell
python -m app.main validate-template templates/<template_id>.py --json
```

Render one template directly:

```powershell
python -m app.main render-template <template_id> <output_path> --params-json '<json>' --json
```

## Required Contract

Every template must define:

```text
TEMPLATE_ID
TEMPLATE_VERSION
OUTPUT_TYPE
metadata()
validate_params(params)
required_assets(params)
render(params, output_path)
```

Templates may advertise machine-matchable intent capabilities through metadata:

```python
def metadata():
    return {
        "capabilities": ["key_point", "quote"]
    }
```

Capability IDs must be lowercase snake-case. Missing capabilities remain valid but cannot be matched automatically. Create or modify a template only after `visual-intents --gaps-only` records a concrete missing capability.

Allowed output types:

```text
png
png_sequence
mp4
```

V0 preview and chunk rendering are complete for PNG previews. Video and PNG-sequence template previews remain planned.

Each template owns its own visual style. Do not add a central art-direction system.
