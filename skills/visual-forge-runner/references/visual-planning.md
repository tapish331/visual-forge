# Visual Planning

Use this reference when a chunk needs visual judgment or when the human asks to inspect visual plans.

## Normal Workflow

Start from compact context:

```powershell
python -m app.main planning-context <project_dir> --chunk <chunk_id> --json
```

Plan what should appear before considering available templates. Use the returned aligned blocks, timing, existing coverage, and capability summaries. Do not read the full transcript, alignment artifact, or `project.json` by default.

Apply the resulting intent plan:

```powershell
python -m app.main apply-visual-plan <project_dir> --chunk <chunk_id> --plan-json '<json>' --json
```

Each intent must include `intent_type`, `purpose`, absolute `start`/`end`, `source_block_ids`, JSON `content`, optional `style_notes`, and an optional `binding`.

Use a binding only when a template genuinely supports the intent type and its params express the intended content. Do not substitute `simple_card` merely because it exists.

## Intent Results

Inspect intents or capability gaps:

```powershell
python -m app.main visual-intents <project_dir> --chunk <chunk_id> --json
python -m app.main visual-intents <project_dir> --chunk <chunk_id> --gaps-only --json
```

- `bound`: the intent created an executable visual; continue to chunk preview when all intents are bound.
- `unbound`: candidate templates exist; bind the intent with `bind-visual-intent`.
- `capability_gap`: no suitable ready template exists; report the exact intent type and read `capability-generation.md` when creation is authorized.

Bind one existing intent without replacing the rest of the plan:

```powershell
python -m app.main bind-visual-intent <project_dir> <intent_id> --template <template_id> --params-json '<json>' --json
```

Capability gaps are planning results, not failures.

## Fallback And Manual Commands

Use the low-judgment heuristic only when explicitly requested:

```powershell
python -m app.main plan-visuals <project_dir> --chunk <chunk_id> --json
```

Manual visual commands remain available:

```powershell
python -m app.main add-visual <project_dir> --chunk <chunk_id> --template <template_id> --start <seconds> --end <seconds> --params-json '<json>' --json
python -m app.main visuals <project_dir> --chunk <chunk_id> --json
python -m app.main preview <project_dir> --chunk <chunk_id> --json
python -m app.main approve-camera-only <project_dir> <chunk_id> --json
```

Visual times are absolute project timeline seconds. Applying a changed plan invalidates affected chunk renders, final composition, and verification through existing fingerprints.
