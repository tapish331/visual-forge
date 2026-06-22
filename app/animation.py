"""Deterministic Pillow-to-MP4 animation helpers for visual templates."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from .artifacts import replace_artifact, temporary_artifact_path
from .audio import resolve_ffmpeg


FrameRenderer = Callable[[int, int], Image.Image]


def render_frames_to_mp4(
    output_path: str | Path,
    *,
    duration_seconds: float,
    frame_renderer: FrameRenderer,
    fps: int = 24,
) -> None:
    """Render deterministic RGB frames through FFmpeg into an MP4 file."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")

    frame_count = max(1, int(round(duration_seconds * fps)))
    output = Path(output_path)
    ffmpeg, error = resolve_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(error)

    with tempfile.TemporaryDirectory(prefix="visual-forge-frames-") as frame_dir_name:
        frame_dir = Path(frame_dir_name)
        for index in range(frame_count):
            image = frame_renderer(index, frame_count).convert("RGB")
            image.save(frame_dir / f"frame_{index:06d}.png", format="PNG")

        with temporary_artifact_path(output) as temporary_output:
            command = [
                ffmpeg,
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(frame_dir / "frame_%06d.png"),
                "-t",
                _seconds(duration_seconds),
                "-c:v",
                "libx264",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-colorspace",
                "bt709",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
            completed = subprocess.run(
                command,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip()
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(f"ffmpeg failed with exit code {completed.returncode}{suffix}")
            if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
                raise RuntimeError(f"ffmpeg did not create {output.name}")
            with temporary_output.open("r+b") as handle:
                os.fsync(handle.fileno())
            replace_artifact(temporary_output, output)


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
