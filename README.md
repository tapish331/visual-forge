# Visual Forge

Visual Forge is a Codex-assisted, Python-powered production system for YouTube explainer videos. It is designed to turn an untimestamped script and a raw narration video into a finished video with well-timed visuals, reusable visual capabilities, and a growing asset library.

## What This Project Is

Visual Forge is intended to support a creator who narrates on camera and wants relevant visuals added at the right moments without manually building every graphic from scratch.

The project should eventually take:

```text
projects/my-video/script.txt
projects/my-video/raw.mp4
```

And produce:

```text
projects/my-video/final.mp4
```

Each video is treated as its own durable project. Previous scripts, raw recordings, project state, generated outputs, and final videos should not need to be deleted. Shared templates and reusable base assets live outside individual video projects so the system improves over time.

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

The script names below are intended interfaces for future implementation; most of them do not exist yet.

| Step | Stage | Human | Codex Runner | Python Script |
| ---: | --- | --- | --- | --- |
| 1 | Create project files | Add `script.txt` and `raw.mp4` to a project folder. | No action unless asked to help organize files. | None |
| 2 | Initialize project | Ask the runner to start the project. | Invoke project initialization. | `app/main.py init`, backed by `app/project.py` |
| 3 | Validate project/status | Review missing-file or setup warnings if needed. | Invoke status and explain what is missing or ready. | `app/main.py status`, backed by `app/project.py` |
| 4 | Probe raw video | No action. | Ask Python to inspect media properties instead of reasoning from the file manually. | `app/media_probe.py` |
| 5 | Extract audio | No action. | Trigger deterministic audio extraction. | `app/audio.py` |
| 6 | Transcribe narration | No action. | Run transcription and store raw transcript chunks. | `app/transcribe.py` |
| 7 | Align script to speech | Inspect later if timestamps feel wrong. | Use alignment output and intervene only when correction is needed. | `app/align.py` |
| 8 | Create chunks | No action. | Ask Python to split the long video into checkpoint-sized chunks. | `app/chunks.py` |
| 9 | Show status | Review current progress. | Summarize what is complete, failed, or next. | `app/status.py` |
| 10 | Plan visuals per chunk | Give feedback if visual choices feel wrong. | Plan useful visuals for one chunk and persist the plan. | `app/project.py` |
| 11 | Inspect template inventory | No action. | Check whether required visual capabilities already exist. | `app/templates.py` |
| 12 | Create missing template | Review generated output later. | Write or modify `templates/<template_id>.py`. | `app/templates.py` validates the template |
| 13 | Create missing base asset | Review generated output later. | Create the reusable base asset. | `app/assets.py` validates, hashes, and normalizes the asset |
| 14 | Write render items | No action. | Convert the visual plan into exact render instructions. | `app/render_plan.py` |
| 15 | Render preview | Inspect one visual or one chunk. | Run a preview before final rendering. | `app/preview.py` |
| 16 | Apply corrections | Tell the runner what is wrong. | Interpret feedback and target the correction. | `app/corrections.py` |
| 17 | Render chunk | Inspect chunk output. | Run deterministic render after templates and assets are ready. | `app/render.py` |
| 18 | Check cache | No action. | Avoid regenerating unchanged work. | `app/cache.py` |
| 19 | Compose final video | Ask for final render when ready. | Confirm all chunks are complete before composing. | `app/compose.py` |
| 20 | Verify final video | Watch the final output. | Explain final output and any mechanical warnings. | `app/verify.py` |

Codex should not load the full 25-minute project into context. Python should expose compact summaries for Codex to inspect, and Codex should open only the current chunk, template, or reference material needed for the next decision.

## Project Structure

The intended concise structure is:

```text
visual-forge/
  app/        Python CLI and deterministic pipeline code
  skills/     Runner skill plus focused reference docs
  templates/  Reusable visual generator scripts
  assets/     Reusable base assets such as frames, textures, logos, and fonts
  library/    Reusable generated material and indexed visual capabilities
  projects/   One folder per YouTube video
```

The repository is currently an early scaffold, so not every planned folder or behavior exists yet.

## Per-Video Projects

Each video should stay compact:

```text
projects/my-video/
  script.txt
  raw.mp4
  project.json
  final.mp4
```

Meaning:

- `script.txt` is the original untimestamped narration script.
- `raw.mp4` is the full camera recording.
- `project.json` is the single workflow state file.
- `final.mp4` is the finished output.

`project.json` should contain alignment data, chunks, visual plans, render items, cache keys, failures, and human-requested corrections. It should be structured so a long video can be resumed, revised, and rendered in pieces.

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
      "status": "planned",
      "transcript": "...",
      "visual_plan": [],
      "render_items": [],
      "corrections": [],
      "cache": {}
    }
  ]
}
```

Typical chunk states:

```text
new
aligned
planned
templates_ready
assets_ready
rendered
failed
```

A failed chunk should record the failed stage, the error summary, and the recommended next action. Re-running the project should resume from the correct checkpoint instead of restarting everything.

## Runner Skill and Python Scripts

The planned human-facing skill is:

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

Planned CLI shape:

```bash
python -m app.main status projects/my-video
python -m app.main init projects/my-video
python -m app.main next projects/my-video
python -m app.main preview projects/my-video --chunk chunk_001
python -m app.main final projects/my-video
```

These commands describe the intended interface and are not implemented yet.

## Template Contract

Every reusable template should have a predictable contract so the runner can inspect and use it safely.

Each template must declare:

- Template identity.
- Template version.
- Required input fields.
- Required base assets.
- Supported output type, such as PNG, PNG sequence, or MP4.
- Render entry point.

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

The only hard gate is final rendering: `final.mp4` should be composed only when all chunks are successfully completed.

The project should support:

- Corrections for transcript, timestamp, chunk, and visual-plan mistakes.
- Previewing one visual or one chunk before final render.
- Caching so unchanged outputs are not regenerated.
- Failure records with stage, error, and recommended next action.

The human owns subjective quality decisions. The system should enforce mechanical correctness: files exist, dimensions are right, durations are valid, renders succeed, and the final video encodes properly.

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

Old project inputs and outputs should not need to be removed during normal use. They can stay in project folders locally or move to external storage later.

## Current Status

Visual Forge is currently an early scaffold.

What exists today:

```text
app/main.py
app/theme.py
templates/__init__.py
inputs/sample.json
```

The current Python files and sample input are placeholders. The runner skill, project model, CLI commands, script alignment, template generation, rendering, previews, caching, final composition pipeline, and end-to-end flow scripts are not implemented yet.

## Planned First Milestone

The first useful milestone should be small and complete:

1. Add the `projects/` layout.
2. Implement project initialization.
3. Create and validate `project.json`.
4. Add a status command.
5. Add the template contract.
6. Add one simple reusable template.
7. Render one deterministic preview from JSON.
8. Document the modular runner skill structure and how it should operate the pipeline.

The goal is to establish the production loop before expanding into full transcription, alignment, asset generation, and video composition.
