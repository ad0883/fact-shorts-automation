"""Prepare licensed gameplay footage for a complete, vertical narration."""

from __future__ import annotations

import json
import math
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import settings
from config.settings import ASSETS_DIR, VIDEO_HEIGHT, VIDEO_WIDTH

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def run_media_command(command: list[str], *, cwd: Path | None = None,
                      timeout: float = 1800) -> subprocess.CompletedProcess:
    """Run a media tool with bounded output and an actionable failure."""
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{command[0]} is not installed or is not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{command[0]} exceeded its {timeout:g}-second timeout.") from exc
    if result.returncode:
        detail = result.stderr.strip()[-4000:] or "No diagnostic output."
        raise RuntimeError(f"{command[0]} failed (exit {result.returncode}):\n{detail}")
    return result


def probe_media(path: Path) -> dict:
    """Read stream metadata without treating malformed files as valid media."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file does not exist: {path}")
    if not path.stat().st_size:
        raise ValueError(f"Media file is empty: {path}")
    result = run_media_command([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ], timeout=30)
    try:
        metadata = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"ffprobe returned invalid metadata for {path.name}.") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("streams"), list):
        raise ValueError(f"No playable streams found in {path}.")
    return metadata


def get_duration_seconds(path: Path) -> float:
    metadata = probe_media(path)
    candidates = [metadata.get("format", {}).get("duration")]
    candidates.extend(stream.get("duration") for stream in metadata["streams"])
    for raw_duration in candidates:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise ValueError(f"Could not determine a positive duration for {path}.")


def pick_source_clip() -> Path:
    clips = sorted(path for path in ASSETS_DIR.iterdir()
                   if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS) if ASSETS_DIR.is_dir() else []
    if not clips:
        raise FileNotFoundError(
            f"No gameplay video found in {ASSETS_DIR}. Add licensed MP4, MOV, M4V, MKV, or WebM footage."
        )
    return random.choice(clips)


def video_fps() -> int:
    fps = int(getattr(settings, "VIDEO_FPS", 30))
    if not 1 <= fps <= 120:
        raise ValueError("VIDEO_FPS must be between 1 and 120.")
    return fps


def video_filter() -> str:
    if VIDEO_WIDTH <= 0 or VIDEO_HEIGHT <= 0 or VIDEO_WIDTH % 2 or VIDEO_HEIGHT % 2:
        raise ValueError("Video width and height must be positive even numbers for H.264.")
    return (f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,fps={video_fps()},format=yuv420p")


def select_background_clip(target_duration: float, out_path: Path, *,
                           source_path: Path | None = None,
                           rng: random.Random | None = None) -> Path:
    """Crop a random segment, looping short clips to cover every narrated word.

    An explicit source bypasses gameplay discovery. The optional random generator
    makes segment selection reproducible without changing global random state.
    Output is replaced atomically only after FFmpeg succeeds.
    """
    target_duration = float(target_duration)
    if not math.isfinite(target_duration) or target_duration <= 0:
        raise ValueError("Target duration must be a positive, finite number.")
    source = Path(source_path) if source_path is not None else pick_source_clip()
    source = source.expanduser().resolve()
    out_path = Path(out_path)
    if source == out_path.resolve():
        raise ValueError("Background output must be different from the source footage.")
    metadata = probe_media(source)
    if not any(stream.get("codec_type") == "video" for stream in metadata["streams"]):
        raise ValueError(f"The selected footage has no video stream: {source}")
    source_duration = get_duration_seconds(source)
    start = (rng or random).uniform(0, max(0, source_duration - target_duration))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".background-", dir=out_path.parent) as temp_dir:
        temporary_output = Path(temp_dir) / "background.mp4"
        run_media_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-stream_loop", "-1", "-ss", f"{start:.6f}", "-i", str(source),
            "-t", f"{target_duration:.6f}", "-map", "0:v:0", "-vf", video_filter(),
            "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary_output),
        ])
        if not temporary_output.is_file() or not temporary_output.stat().st_size:
            raise RuntimeError("FFmpeg did not create a background video.")
        temporary_output.replace(out_path)
    return out_path


if __name__ == "__main__":
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    print(f"Saved to {select_background_clip(duration, Path('test_background.mp4'))}")
