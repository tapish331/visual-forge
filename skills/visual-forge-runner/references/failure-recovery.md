# Failure Recovery

Use this reference when status is `failed` or a command returns `success: false`.

## Inspect

```powershell
python -m app.main failures <project_dir> --json
python -m app.main status <project_dir> --json
```

Read only the active failure stage, scope, errors, and recommended next action.

## Retry Model

Repeated failures with the same stage and scope update one active failure. Successful retries resolve matching failures automatically.

Do not create manual failure records. Use existing commands to retry the failed stage.

## Manual Resolution

Use manual resolution only for abandoned or intentionally ignored failures:

```powershell
python -m app.main resolve-failure <project_dir> <failure_id> --json
```

After any recovery, run:

```powershell
python -m app.main next <project_dir> --json
```
