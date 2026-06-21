# Visual Forge

Visual Forge is a Codex-assisted, Python-powered production system for YouTube explainer videos. It is designed to turn a plain or timestamp/speaker-annotated script and a raw narration video into a finished video with well-timed visuals, reusable visual capabilities, and a growing asset library.

## What This Project Is

Visual Forge is intended to support a creator who narrates on camera and wants relevant visuals added at the right moments without manually building every graphic from scratch.

The project should eventually take:

```text
inputs/my-video/script.txt
inputs/my-video/raw.mp4
```

And produce:

```text
outputs/my-video/final.mp4
```

Each video is treated as its own durable project. Previous scripts, raw recordings, project state, generated outputs, and final videos should not need to be deleted. Shared templates and reusable base assets live outside individual video projects so the system improves over time.

## Installation

Visual Forge requires Python 3.11 or newer. FFmpeg and ffprobe are required for media ingestion, and an internet connection is required the first time `faster-whisper` downloads a model.

```powershell
python -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set machine-specific paths in `.env` when FFmpeg and ffprobe are not available on `PATH`:

```text
VISUAL_FORGE_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
VISUAL_FORGE_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
HF_TOKEN=
```

`HF_TOKEN` is optional for public models but avoids unauthenticated Hugging Face download limits. Model files are cached under `models/faster-whisper/` and can be reused offline afterward.

## Quick Start

Create a project from existing source files and run the currently implemented deterministic pipeline:

```powershell
$py = ".\.venv\Scripts\python.exe"
$project = "projects/my-video"

& $py -m app.main init-from-input "inputs/my-video" --json
& $py -m app.main probe $project --json
& $py -m app.main extract-audio $project --json
& $py -m app.main transcribe $project --json
& $py -m app.main align $project --json
& $py -m app.main alignment-review $project
& $py -m app.main create-chunks $project --json
& $py -m app.main chunks $project
# Repeat visual planning, preview, and rendering for every chunk.
& $py -m app.main planning-context $project --chunk chunk_001 --json
# The runner uses this compact context to author and apply a visual intent plan.
# & $py -m app.main apply-visual-plan $project --chunk chunk_001 --plan-json $planJson --json
& $py -m app.main visuals $project --chunk chunk_001
& $py -m app.main preview $project --chunk chunk_001 --json
& $py -m app.main render-chunk $project chunk_001 --json
& $py -m app.main final $project --json
& $py -m app.main verify-final $project --json
& $py -m app.main status $project --json
```

The expected state after successful final verification is `complete`. Codex-authored visual intents, deterministic fallback planning, manual visual correction, chunk rendering, final composition, and mechanical final verification are implemented. Template and asset generation are still planned.

`complete` means the rendered chunks cover the full canonical raw-video timeline without gaps or overlaps, all provenance is current, and final mechanical verification passed. It does not replace subjective human review.

Every chunk must either contain reviewed visuals or be explicitly approved as camera-only:

```powershell
& $py -m app.main approve-camera-only $project chunk_002 --json
```

The preferred naming convention is one stable lowercase kebab-case project slug shared across all project roots:

```text
inputs/my-video/
projects/my-video/
outputs/my-video/
```

`init-from-input` derives the slug from the input folder name by default, so `inputs/Neha_much/` becomes project slug `neha-much`.

## Core Idea

The system separates creative judgment from deterministic rendering.

- The human talks to one runner skill: `visual-forge-runner`.
- The runner skill acts as the director and toolmaker.
- Python scripts are deterministic animators.
- Base assets are reusable raw material.
- Templates combine base assets plus structured instructions into visuals.
- `project.json` stores progress, decisions, checkpoints, corrections, and render instructions for one video.

The long-term loop is simple: every video can teach Visual Forge a new visual capability, and future videos can reuse that capability cheaply.

## Workflow

The intended workflow is:

1. Create a video project with a script and raw narration video.
2. Initialize `project.json` for that project.
3. Extract and align timestamps from the raw video against the script.
4. Split the video into manageable chunks.
5. Plan useful visuals for each chunk.
6. Check whether the required templates and base assets already exist.
7. Create or update missing templates and base assets.
8. Write render instructions into `project.json`.
9. Render previews or chunk visuals with Python.
10. Compose the final video only after all chunks are complete.

The creator should normally interact with the runner skill, not a pile of manual scripts.

## End-to-End Flow

The human normally talks only to `visual-forge-runner`. Codex makes judgment-heavy decisions and creates or modifies templates and base assets. Python performs deterministic work and updates `project.json`.

The deterministic pipeline through final verification exists today. Later rows describe intended interfaces that are still planned.

| Step | Stage | Status | Human | Codex Runner | Python Script |
| ---: | --- | --- | --- | --- | --- |
| 1 | Initialize with inputs | Implemented | Supply an input folder or explicit script/video paths. | Initialize the project with canonical naming and input references. | `app/main.py init-from-input` or `init`, backed by `app/project.py` |
| 2 | Validate project/status | Implemented | Review missing-file or setup warnings if needed. | Invoke status and explain what is missing or ready. | `app/status.py`, backed by `app/project.py` |
| 3 | Probe raw video | Implemented | No action. | Ask Python to inspect media properties. | `app/media_probe.py` |
| 4 | Extract audio | Implemented | No action. | Trigger deterministic audio extraction. | `app/audio.py` |
| 5 | Transcribe narration | Implemented | No action. | Run offline transcription and store timestamps. | `app/transcribe.py` |
| 6 | Align script to speech | Implemented | Inspect warnings and correct source text when needed. | Use compact alignment review output. | `app/align.py` |
| 7 | Create chunks | Implemented | Approve camera-only chunks when no visuals are needed. | Split the full timeline into contiguous checkpoint-sized chunks and record provenance. | `app/chunks.py` |
| 8 | Show status | Implemented | Review current progress. | Summarize what is complete, stale, failed, or next. | `app/status.py` |
| 8a | Recommend next checkpoint | Implemented | Ask what should happen next. | Use compact state to choose the next deterministic command or stop for human input. | `app/next_step.py` |
| 9 | Plan visuals per chunk | Partial | Give feedback if visual choices feel wrong. | Author template-independent intents from compact context; heuristic and manual planning remain available. | `app/visual_intents.py`, `app/visual_planner.py`, `app/visuals.py` |
| 10 | Inspect template inventory | Implemented | No action. | Check whether required visual capabilities already exist. | `app/templates.py` |
| 11 | Create missing template | Partial | Review generated output later. | Template creation remains manual Codex work with no creation CLI; Python validates and renders it. | `app/templates.py` |
| 12 | Create missing base asset | Planned | Review generated output later. | Create the reusable base asset. | `app/assets.py` |
| 13 | Write render items | Planned | No action. | Convert visual plans into exact render instructions. | `app/render_plan.py` |
| 14 | Render preview | Implemented | Inspect one PNG visual or chunk storyboard. | Render one template, planned visual, or chunk visual storyboard; video and PNG-sequence previews remain planned. | `app/preview.py` |
| 15 | Apply corrections | Partial | Tell the runner what is wrong. | Visual-plan correction exists; other correction workflows are planned. | `app/visuals.py` |
| 16 | Render chunk | Implemented | Inspect chunk output. | Run deterministic rendering after dependencies are ready. | `app/render.py` |
| 17 | Check cache | Planned | No action. | Avoid regenerating unchanged work. | `app/cache.py` |
| 18 | Compose final video | Implemented | Ask for final render when ready. | Confirm all chunks are complete before composing. | `app/compose.py` |
| 19 | Verify final video | Implemented | Watch the final output after mechanical checks pass. | Explain final output errors and recommendation warnings. | `app/verify.py` |

Codex should not load the full 25-minute project into context. Python should expose compact summaries for Codex to inspect, and Codex should open only the current chunk, template, or reference material needed for the next decision.

## Project Structure

The intended concise structure is:

```text
visual-forge/
  app/        Python CLI and deterministic pipeline code
  skills/     Runner skill plus focused reference docs
  templates/  Reusable visual generator scripts
  assets/     Planned reusable base assets such as frames, textures, logos, and fonts
  library/    Planned reusable generated material and indexed visual capabilities
  inputs/     One source-material folder per YouTube video
  projects/   One checkpoint/control folder per YouTube video
  outputs/    One generated-artifact folder per YouTube video
```

The repository has an implemented deterministic production foundation and Runner Skill V0. Reusable asset management, automatic template creation, and several planned folders remain future work.

## Per-Video Projects

Each video should use one canonical slug everywhere. The slug is lowercase kebab-case, such as `my-video` or `neha-much`.

The preferred V1 shape is:

```text
inputs/my-video/
  script.txt
  raw.mp4

projects/my-video/
  project.json

outputs/my-video/
  audio/
    narration.wav
  transcripts/
    narration.json
  alignment/
    script_alignment.json
  previews/
    preview_<hash>.png
  chunk-previews/
    chunk_001.png
    chunk_001.json
  renders/
    chunks/
      chunk_001.mp4
  final.mp4
  verification/
    final.json
```

Meaning:

- `inputs/<slug>/script.txt` is the original narration script. It may be plain or contain recognized timestamp/speaker headers.
- `inputs/<slug>/raw.mp4` is the full camera recording.
- `projects/<slug>/project.json` is the single workflow state file.
- `outputs/<slug>/audio/`, `transcripts/`, `alignment/`, `previews/`, `chunk-previews/`, and `renders/` contain deterministic generated artifacts.
- `outputs/<slug>/final.mp4` is the planned finished output.

Project folders store compact state; output folders store generated media and derived artifacts. Legacy projects without layout metadata still read and write generated artifacts under their project folder, but new projects should use the three-root layout.

The preferred initializer is:

```powershell
python -m app.main init-from-input inputs/my-video
```

If canonical names are absent, `init-from-input` accepts one `.txt` script and one supported video file from that input folder. When several candidates exist, pass explicit files:

```powershell
python -m app.main init-from-input inputs/my-video --script inputs/my-video/raw-script.txt --video inputs/my-video/raw-video.mp4
```

Large inputs do not have to be copied. Project initialization can reference an existing script and video anywhere on disk:

```powershell
python -m app.main init projects/my-video --script inputs/my-video/raw-script.txt --video inputs/my-video/raw-video.mp4
```

Visual Forge stores paths relative to the project directory when possible and uses absolute paths only when a relative path cannot be represented, such as across Windows drives. Existing projects are never silently repointed to different inputs; create a new project when changing source files.

Existing legacy projects can adopt the V1 layout:

```powershell
python -m app.main adopt-layout projects/my-video --input-dir inputs/my-video
```

This moves existing generated artifact directories into `outputs/<slug>/` only when the destination is empty. It does not rename raw input folders or change visual, chunk, or failure IDs.

`project.json` contains alignment data, chunks, visual plans, previews, render metadata, verification metadata, failures, and human-requested corrections. A future render-plan and cache layer may normalize and optimize rendering, but the current renderer intentionally consumes visual and preview records directly.

The canonical V1 timeline preserves the full probed raw video from `0` through its duration. Trimming is not implicit; a future explicit trim feature may intentionally change those bounds.

## Checkpoints and Resumability

Long videos should not be processed in one pass. A 25-minute video should be handled chunk by chunk, with resumable checkpoints stored in `project.json`.

Example shape:

```json
{
  "project": {
    "name": "my-video",
    "script": "script.txt",
    "video": "raw.mp4"
  },
  "chunks": [
    {
      "id": "chunk_001",
      "start": 0,
      "end": 180,
      "status": "new",
      "alignment_block_ids": [],
      "warning_block_ids": []
    }
  ]
}
```

Chunks remain compact and reference source alignment blocks by ID. Visuals, corrections, cache entries, and future render items remain project-level collections that reference `chunk_id`; they should not be duplicated inside each chunk.

Typical chunk states:

```text
new
previewed
rendered
```

Chunk `visual_mode` is independent of render status:

```text
undecided
visuals
camera_only
```

Chunks are contiguous workflow checkpoints, not editorial cuts. The first chunk begins at timeline start, the last ends at timeline end, and adjacent chunks share one midpoint boundary inside the pause between their aligned block groups. This preserves leading footage, trailing footage, and every inter-chunk pause exactly once.

Failed operations are recorded in the project-level failure lifecycle with stage, scope, error summary, and recommended next action. Re-running the project should resume from the correct checkpoint instead of restarting everything.

`project.json`, transcript JSON, and alignment JSON are written atomically so an interrupted replacement does not destroy the previous valid checkpoint. Audio extraction also writes to a temporary WAV before replacing the previous narration artifact. Failure records have stable project-local IDs and an `active` or `resolved` lifecycle. Repeating the same failed operation updates its active record, successful retries resolve matching failures, and resolved history remains available without blocking progress.

Pipeline stages carry provenance fingerprints. Large video/audio files use size and nanosecond modification time so status remains fast; small script and JSON artifacts use SHA-256. Changing or deleting an upstream artifact marks dependent stages stale and rolls status back to the earliest trustworthy checkpoint without deleting downstream work or creating a failure record.

Chunking records its alignment fingerprint, canonical timeline fingerprint, duration options, complete coverage summary, and deterministic chunk-plan fingerprint. Legacy chunks without this provenance are `unverified` and must be regenerated once with `create-chunks --force`.

Render dependencies use the same rule. Preview records fingerprint the template source and generated PNG. Each chunk render records a canonical fingerprint of its chunk-scoped visual plan plus the fingerprints of every preview it consumed. Adding or updating a visual, changing a template, or changing/removing a preview makes the affected chunk render stale; final composition and verification then become stale through dependency propagation.

Generated files are retained when they become stale. The status checkpoint rolls back, but old previews, chunk MP4s, `final.mp4`, and verification reports remain available for inspection until successful commands replace them.

Artifacts created before provenance fingerprints are reported as `unverified`. Pipeline artifacts must pass once through `probe`, `extract-audio`, `transcribe`, and `align`; legacy render chains must pass once through chunk preview, chunk render, final composition, and final verification.

## Runner Skill and Python Scripts

The implemented human-facing skill is:

```text
visual-forge-runner
```

The creator should be able to say things like:

```text
Start this project.
Continue the next checkpoint.
Redo chunk 3 visuals.
Preview chunk 2.
Render the final video.
Show current status.
```

The runner skill should inspect compact project summaries, decide the next action, update or create templates when needed, and run the appropriate Python scripts.

Runner Skill V0 is implemented as repo-local skill source:

```text
skills/visual-forge-runner/
  SKILL.md
  references/
    project-workflow.md
    visual-planning.md
    template-contract.md
    correction-workflow.md
    rendering-rules.md
    failure-recovery.md
```

The repo-local skill files are the source of truth. To make the skill available to a fresh Codex session through normal skill discovery, copy `skills/visual-forge-runner/` into `%USERPROFILE%\.codex\skills\visual-forge-runner\`.

One runner skill does not mean one huge instruction file. The efficient model is one front door, many internal modules, and many Python tools.

### Internal Skill Structure

The runner skill should stay thin and route to focused references only when needed:

```text
visual-forge-runner/
  SKILL.md
  references/
    project-workflow.md
    visual-planning.md
    template-contract.md
    correction-workflow.md
    rendering-rules.md
    failure-recovery.md
```

`SKILL.md` should define the operating rules: inspect status, choose the current workflow, read only the relevant reference file, run Python for deterministic work, and report what changed.

For V1, specialist workflows should be focused reference docs or workflow modules inside the runner skill rather than separate user-facing Codex skills calling each other like services. Python scripts are the real callable execution layer. If the project outgrows this structure later, the workflow can be packaged as a plugin or split into separate skills without changing the normal human-facing entry point.

Python should do deterministic work:

- Extract audio.
- Chunk media.
- Validate JSON.
- Hash inputs and outputs.
- Render visuals.
- Compose video.
- Produce compact status summaries.

Codex should spend tokens only where judgment or code generation is needed:

- Plan visuals.
- Decide whether a template or base asset is missing.
- Create or modify template scripts.
- Create reusable base assets when needed.
- Interpret human correction requests.
- Explain what changed after a run.

Codex should not load every workflow at once. It should prefer compact Python CLI summaries over reading large `project.json` files or media-derived artifacts, and it should open only the reference docs needed for the current stage.

Implemented CLI:

```bash
python -m app.main status projects/my-video
python -m app.main status projects/my-video --json
python -m app.main next projects/my-video
python -m app.main next projects/my-video --json
python -m app.main init projects/my-video
python -m app.main init projects/my-video --script inputs/my-video/raw-script.txt --video inputs/my-video/raw-video.mp4
python -m app.main init-from-input inputs/my-video
python -m app.main init-from-input inputs/my-video --script inputs/my-video/raw-script.txt --video inputs/my-video/raw-video.mp4 --json
python -m app.main adopt-layout projects/my-video --input-dir inputs/my-video
python -m app.main adopt-layout projects/my-video --input-dir inputs/my-video --json
python -m app.main probe projects/my-video
python -m app.main probe projects/my-video --json
python -m app.main extract-audio projects/my-video
python -m app.main extract-audio projects/my-video --json
python -m app.main transcribe projects/my-video --json
python -m app.main transcribe projects/my-video --provider faster-whisper --model large-v3 --json
python -m app.main transcribe projects/my-video --provider faster-whisper --model large-v3-turbo --json
python -m app.main transcribe projects/my-video --provider faster-whisper --model large-v3 --device cpu --compute-type int8 --json
python -m app.main transcribe projects/my-video --provider mock --json
python -m app.main align projects/my-video
python -m app.main align projects/my-video --json
python -m app.main alignment-review projects/my-video
python -m app.main alignment-review projects/my-video --json
python -m app.main create-chunks projects/my-video
python -m app.main create-chunks projects/my-video --json
python -m app.main create-chunks projects/my-video --force --json
python -m app.main chunks projects/my-video
python -m app.main chunks projects/my-video --json
python -m app.main approve-camera-only projects/my-video chunk_002
python -m app.main approve-camera-only projects/my-video chunk_002 --json
python -m app.main plan-visuals projects/my-video --chunk chunk_001
python -m app.main plan-visuals projects/my-video --chunk chunk_001 --json
python -m app.main plan-visuals projects/my-video --chunk chunk_001 --max-visuals 3 --json
python -m app.main plan-visuals projects/my-video --chunk chunk_001 --force-generated --json
python -m app.main planning-context projects/my-video --chunk chunk_001 --json
python -m app.main apply-visual-plan projects/my-video --chunk chunk_001 --plan-json $planJson --json
python -m app.main visual-intents projects/my-video --chunk chunk_001 --json
python -m app.main visual-intents projects/my-video --chunk chunk_001 --gaps-only --json
python -m app.main templates
python -m app.main templates --json
python -m app.main validate-template templates/simple_card.py
python -m app.main validate-template templates/simple_card.py --json
python -m app.main render-template simple_card outputs/simple_card.png --params-json "{\"title\":\"Hello\"}"
python -m app.main render-template templates/simple_card.py outputs/simple_card.png --params-json "{\"title\":\"Hello\"}" --json
python -m app.main preview projects/my-video --template simple_card --params-json "{\"title\":\"Hello\"}"
python -m app.main preview projects/my-video --template templates/simple_card.py --params-json "{\"title\":\"Hello\"}" --json
python -m app.main preview projects/my-video --chunk chunk_001
python -m app.main preview projects/my-video --chunk chunk_001 --json
python -m app.main render-chunk projects/my-video chunk_001
python -m app.main render-chunk projects/my-video chunk_001 --json
python -m app.main final projects/my-video
python -m app.main final projects/my-video --json
python -m app.main verify-final projects/my-video
python -m app.main verify-final projects/my-video --json
python -m app.main add-visual projects/my-video --template simple_card --start 12.5 --end 18 --params-json "{\"title\":\"Key idea\"}"
python -m app.main add-visual projects/my-video --chunk chunk_001 --template simple_card --start 12.5 --end 18 --params-json "{\"title\":\"Key idea\"}" --json
python -m app.main visuals projects/my-video
python -m app.main visuals projects/my-video --json
python -m app.main visuals projects/my-video --chunk chunk_001
python -m app.main visuals projects/my-video --chunk chunk_001 --json
python -m app.main preview-visual projects/my-video visual_e21c2f420ec1
python -m app.main preview-visual projects/my-video visual_e21c2f420ec1 --json
python -m app.main update-visual projects/my-video visual_e21c2f420ec1 --params-json "{\"title\":\"Better title\"}"
python -m app.main update-visual projects/my-video visual_e21c2f420ec1 --start 14 --end 19
python -m app.main update-visual projects/my-video visual_e21c2f420ec1 --chunk chunk_001
python -m app.main update-visual projects/my-video visual_e21c2f420ec1 --template simple_card --params-json "{\"title\":\"Reworked visual\"}" --json
python -m app.main failures projects/my-video
python -m app.main failures projects/my-video --json
python -m app.main failures projects/my-video --all
python -m app.main resolve-failure projects/my-video failure_0001
python -m app.main resolve-failure projects/my-video failure_0001 --json
python -m app.main --log-only run-logged -- python -m pytest
python -m app.main --log-only run-logged -- python -m compileall app templates tests
```

`next` is read-only. It reports whether human input is required and, when the next checkpoint is deterministic, returns the exact command list the runner can execute.

`status --json` exposes both logical project paths and layout-aware resolved paths. Use `path` for project metadata and `resolved_path` when reporting file locations to humans. For layout V1 projects, generated artifacts resolve under `outputs/<slug>/`; for legacy projects, they resolve under the project folder.

## Visual Intents And Capability Gaps

The normal runner planning path is template-independent:

```text
planning-context -> Codex judgment -> apply-visual-plan -> preview
```

`planning-context` exposes only the requested chunk's aligned text, timing, existing visual coverage, and compact template capabilities. Codex writes project-level `visual_intents` containing purpose, content, timing, source block IDs, intent type, and optional style notes.

Intent states are:

```text
bound          valid template binding created an executable visual
unbound        compatible templates exist but Codex has not selected a binding
capability_gap no existing template advertises the required intent type
```

Capability gaps are planning results rather than project failures. The runner must report them and stop before preview/rendering. `plan-visuals` remains available as an explicit low-judgment heuristic fallback and is no longer the normal runner recommendation.

## Template Contract

Every reusable template should have a predictable contract so the runner can inspect and use it safely.

Each template must declare:

- `TEMPLATE_ID`
- `TEMPLATE_VERSION`
- `OUTPUT_TYPE`, using `png`, `png_sequence`, or `mp4`
- `metadata()`
- `validate_params(params)`
- `required_assets(params)`
- `render(params, output_path)`

Template inventory, validation, contract-level template rendering, and PNG project preview are implemented. `simple_card` can render a deterministic `1920x1080` PNG. Chunk rendering, final composition, and final verification are implemented. Video and PNG-sequence template previews remain planned.

Template metadata may advertise lowercase snake-case capability IDs such as `key_point`, `quote`, or `newspaper_headline`. Visual intents use exact capability matching to find candidate templates. Missing capability metadata remains backward compatible but prevents automatic matching.

`required_assets(params)` is declared and contract-validated, but returned asset paths are not yet resolved, hashed, or enforced. Asset inventory and provenance belong to the planned Template And Asset Generation milestone.

Templates choose their own visual style. There is no central art-direction system in V1 because each visual type may need a different treatment. Global rules should only cover mechanical constraints such as resolution, duration, safe margins, and output encoding.

## Output Targets

The intended media backbone is FFmpeg and ffprobe.

Default YouTube-oriented output target:

```text
Resolution: 1920 x 1080
Aspect ratio: 16:9
Container: MP4
Video codec: H.264
Profile: High Profile
Scan type: Progressive
Frame rate: Same as the recorded source
Bitrate, 1080p SDR: 8 Mbps for 24/25/30 fps, 12 Mbps for 48/50/60 fps
Audio codec: AAC-LC
Audio sample rate: 48 kHz
Audio bitrate: 384 kbps stereo
Color space, SDR: BT.709 / Rec.709
```

These defaults follow YouTube's recommended upload encoding settings:

```text
https://support.google.com/youtube/answer/1722171
```

## Project Control

Human review happens naturally after each runner response. The runner reports what it did, the human inspects the project outputs, and the human asks for fixes if something is wrong.

Final composition is the only human-requested production gate: `final.mp4` should be composed only when all chunks are successfully completed. The current CLI does not prompt for confirmation; the runner checks readiness before invoking it. Earlier commands enforce mechanical prerequisites such as current fingerprints, timeline coverage, valid timing, preview availability, and required media streams.

Mechanical completion requires current raw media, transcript, alignment, timeline, chunk plan, chunk renders, final composition, and verification. Human approval remains the creator watching the resulting video and requesting corrections.

The project is intended to support:

- Corrections for transcript, timestamp, chunk, and visual-plan mistakes.
- Previewing one visual or one chunk storyboard before final render.
- Caching so unchanged outputs are not regenerated.
- Active and resolved failure records with stage, error, retry count, and recommended next action.

Codex-authored visual intents, capability-gap detection, deterministic fallback planning, and visual-plan correction through `update-visual` are implemented. Chunk visual storyboard preview is implemented under `outputs/<slug>/chunk-previews/`. Chunk MP4 rendering is implemented under `outputs/<slug>/renders/chunks/`, final composition joins rendered chunks into `outputs/<slug>/final.mp4`, and `verify-final` writes mechanical checks to `outputs/<slug>/verification/final.json`. Transcript, timestamp, chunk, and general correction commands remain planned; until then, the human corrects the source script and reruns the affected deterministic stage.

Visual changes invalidate only their affected chunk checkpoints. A changed chunk returns to `new`, `preview --chunk` advances it to `previewed`, and `render-chunk` advances it to `rendered` while recording current dependency fingerprints. A project reaches `ready_for_final` only when every chunk render is current, and reaches `complete` only when the resulting final video has current passing verification.

Transcription V0 stores full transcript data in `transcripts/narration.json` and stores only compact summary metadata in `project.json`. The normal transcription path is offline through `faster-whisper`, with `large-v3` as the best-quality default, `large-v3-turbo` as the faster fallback, and CPU `int8` as the reliable fallback when CUDA setup is unavailable. This keeps project status cheap for Codex to inspect while preserving the timestamped transcript needed by alignment and future chunking.

Alignment V1 maps normalized spoken-content tokens onto `faster-whisper` word timestamps with a deterministic sequence matcher. Recognized timestamp/speaker headers are retained as metadata but excluded from confidence. `aligned` blocks have canonical timing, `needs_review` blocks expose candidate timing only, and `unmatched` blocks remain untimed. `alignment-review` provides compact inspection without mutating the project.

Alignment metadata includes SHA-256 fingerprints for the script and transcript. Editing either source marks alignment stale and returns project status to `ready_for_alignment`; review and unmatched blocks remain soft warnings rather than project failures.

The human owns subjective quality decisions. The system should enforce mechanical correctness: files exist, dimensions are right, durations are valid, renders succeed, and the final video encodes properly.

## Logging and Parallel Commands

Every Visual Forge CLI run appends its command, textual output, errors, exit code, and duration to a process-safe rotating log:

```text
logs/visual-forge.log
logs/visual-forge.log.1
...
logs/visual-forge.log.4
```

Each file is limited to 5 MB by default, keeping the total log budget near 25 MB. Rotation is safe across parallel terminals, and the oldest backup is removed automatically. Generated logs are local and are not committed.

Use `--log-only` when terminal output would be too large:

```powershell
$py = ".\.venv\Scripts\python.exe"
$stamp = Get-Date -Format "yyyyMMddHHmmss"
& $py -m app.main --log-only run-logged -- $py -m pytest --basetemp="outputs/pytest-temp-$stamp" -o "cache_dir=outputs/pytest-cache-$stamp"
& $py -m app.main --log-only run-logged -- $py -m compileall app templates tests
Get-Content logs/visual-forge.log -Tail 100
Get-ChildItem logs | Select-Object Name,Length
```

Use a fresh pytest temp/cache path per run so stale Windows locks or sandbox-owned generated directories do not block cleanup.

Commands for different projects may mutate their projects in parallel. A second mutating command for the same project exits with code `3` instead of risking lost updates. Read-only commands such as `status`, `visuals`, and `failures` may still read an atomic checkpoint while a mutation is running.

The defaults can be overridden with `VISUAL_FORGE_LOG_DIR`, `VISUAL_FORGE_LOG_MAX_BYTES`, `VISUAL_FORGE_LOG_BACKUP_COUNT`, `VISUAL_FORGE_LOG_LEVEL`, and `VISUAL_FORGE_LOG_DISABLED`.

## Local Secrets

Visual Forge CLI commands automatically load `.env` from the repository root before running. Copy `.env.example` to `.env` and fill in only the values needed on your machine.

Use `.env` for local secrets and machine-specific paths:

```text
HF_TOKEN=hf_...
VISUAL_FORGE_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
VISUAL_FORGE_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
```

`.env` is ignored by Git. `.env.example` is committed as the safe template.

## Example Visual Capability

Suppose a video needs a newspaper headline visual.

The runner should check whether the project already has:

```text
templates/newspaper_headline.py
assets/images/newspaper_base.png
```

If not, Codex should create the missing reusable template and the missing base newspaper asset. After that, Python can animate the result deterministically:

```text
blank newspaper base asset
+ headline text
+ date
+ layout parameters
+ motion settings
= finished newspaper visual
```

Future videos should reuse the same newspaper capability instead of rebuilding it from scratch.

## Git and Media Policy

Commit source material that helps the system improve:

- Code.
- README and docs.
- Codex skill instructions.
- Template scripts.
- Small reusable base assets.
- Small project metadata when useful.

Do not commit heavy or regenerable media by default:

- Raw narration videos.
- Final rendered videos.
- Render caches.
- Heavy generated media.
- Rotating command logs.

Old project inputs and outputs should not need to be removed during normal use. New projects should keep source material under `inputs/<slug>/`, compact checkpoints under `projects/<slug>/`, and generated artifacts under `outputs/<slug>/`. Legacy project-local artifacts remain supported and can be adopted into the three-root layout when useful.

## Current Status

Visual Forge currently has a tested project-control, media-ingestion, alignment, chunking, Codex intent-planning, deterministic fallback planning, template-preview, chunk-rendering, final-composition, final-verification, and runner-skill foundation. Template and asset generation remain scaffolds.

What exists today:

```text
pyproject.toml
app/__init__.py
app/align.py
app/artifacts.py
app/audio.py
app/chunks.py
app/compose.py
app/env.py
app/failures.py
app/layout.py
app/logging_utils.py
app/main.py
app/media_probe.py
app/next_step.py
app/preview.py
app/project.py
app/project_lock.py
app/render.py
app/render_freshness.py
app/render_template.py
app/status.py
app/templates.py
app/timeline.py
app/transcribe.py
app/visual_intents.py
app/visual_planner.py
app/visuals.py
app/verify.py
skills/visual-forge-runner/SKILL.md
skills/visual-forge-runner/agents/openai.yaml
skills/visual-forge-runner/references/correction-workflow.md
skills/visual-forge-runner/references/failure-recovery.md
skills/visual-forge-runner/references/project-workflow.md
skills/visual-forge-runner/references/rendering-rules.md
skills/visual-forge-runner/references/template-contract.md
skills/visual-forge-runner/references/visual-planning.md
templates/__init__.py
templates/simple_card.py
```

Project initialization with canonical or external input references, three-root slug-based layout metadata, legacy layout adoption, atomic checkpoint writes, hybrid artifact fingerprints, stale dependency detection, status reporting, read-only next-step recommendation, raw media probing with `ffprobe`, narration audio extraction with FFmpeg, offline narration transcription through `faster-whisper` or deterministic mock providers, metadata-aware alignment with compact review, full-timeline contiguous chunk creation, camera-only chunk approval, active/resolved failure management, process-safe bounded logging, project mutation locks, template contract validation, template capability inventory, one deterministic template render path, PNG project preview, Codex-authored visual intents, capability-gap detection, deterministic fallback planning, project-level and chunk-scoped visual items, preview-by-visual-ID, chunk storyboard preview, render dependency fingerprinting, exact-duration chunk MP4 rendering, final composition, final verification, visual correction through `update-visual`, and Runner Skill V0 are implemented. Template generation, asset management, caching, video/sequence templates, and most later flow scripts are not implemented yet.

## Implemented Foundation

The first useful milestone is the project-control and template-contract foundation. Its first slices are now implemented:

1. Initialize a project directory with canonical files or external script/video references.
2. Initialize from an `inputs/<slug>/` folder and write generated artifacts under `outputs/<slug>/`.
3. Adopt the three-root layout for existing legacy projects without changing IDs.
4. Create and validate `project.json`.
5. Report human-readable project status.
6. Report compact JSON status for future runner skill use.
7. Keep raw/final media ignored while allowing `script.txt` and `project.json` to remain trackable.
8. Validate the V1 template contract.
9. List template inventory in human-readable and compact JSON formats.
10. Provide one valid template, `simple_card`.
11. Render `simple_card` to a deterministic PNG from JSON params.
12. Render a single-template project preview and record it in `project.json`.
13. Add and list project-level planned visual items.
14. Preview a planned visual by ID and update its status.
15. Update a planned visual in place, clear stale preview linkage, and record a correction.
16. Write checkpoints atomically and manage active/resolved failures across retries.
17. Capture bounded process-safe logs and reject overlapping mutations of one project.
18. Probe `raw.mp4` with `ffprobe` and store compact media metadata.
19. Extract transcription-friendly narration audio to `audio/narration.wav`.
20. Transcribe narration audio offline into `transcripts/narration.json` and store compact metadata in `project.json`.
21. Align `script.txt` to transcript word timestamps in `alignment/script_alignment.json`.
22. Parse timestamp/speaker annotations, fingerprint alignment sources, and inspect warnings with `alignment-review`.
23. Track the video-to-alignment provenance chain and roll stale projects back to the earliest trustworthy stage.
24. Preserve prior audio, transcript, and alignment artifacts until atomic replacements succeed.
25. Create deterministic chunk checkpoints from current alignment output.
26. Add, list, update, and preview manual visual records associated with a specific `chunk_id`.
27. Preview one chunk's planned visuals together as a storyboard under `chunk-previews/`.
28. Render a previewed chunk into an MP4 segment under `renders/chunks/`.
29. Compose rendered chunks into `final.mp4`.
30. Verify final container, streams, timing, codecs, color metadata, and recommendation warnings with `verify-final`.
31. Fingerprint preview templates, preview PNGs, visual plans, and chunk-render inputs so visual changes invalidate final output mechanically.
32. Preserve the full raw-video timeline with contiguous midpoint chunk boundaries and verified zero-gap coverage.
33. Fingerprint chunk plans against alignment and timeline inputs so upstream changes roll status back correctly.
34. Explicitly approve camera-only chunks without requiring artificial visuals.
35. Recommend the next deterministic checkpoint with the read-only `next` CLI.
36. Provide a repo-local `visual-forge-runner` skill with focused workflow references.
37. Plan starter visuals for one chunk with deterministic `plan-visuals` using existing templates only.
38. Persist template-independent Codex visual intents, match template capabilities, and record explicit capability gaps.

The next slice should implement template and asset generation so the runner can create new visual capabilities when existing templates are insufficient.

The goal is to expand from deterministic operation into creative planning, reusable asset generation, and richer template capabilities.
