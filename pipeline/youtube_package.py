"""Local, reviewable publishing assets. No model calls or network access."""

from pathlib import Path

from pipeline.run_store import write_json
from pipeline.select_background import probe_media


def attribution_text(segments: list[dict]) -> str:
    lines = []
    for segment in segments:
        source = segment.get("provenance", {})
        name = source.get("photographer") or segment.get("photographer") or segment.get("attribution")
        if not name:
            continue
        parts = [str(name), source.get("provider", ""), source.get("photo_url", ""),
                 source.get("license", ""), source.get("license_url", ""),
                 source.get("modifications", "")]
        line = " | ".join(str(part) for part in parts if part)
        if line not in lines:
            lines.append(line)
    return "Image credits:\n" + "\n".join(lines) if lines else ""


def validate_short(video: Path) -> float:
    metadata = probe_media(video)
    streams = metadata.get("streams", [])
    visual = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    width, height = visual.get("width", 0), visual.get("height", 0)
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not width or height < width or not 0 < duration <= 180:
        raise ValueError("A YouTube Short must be square or vertical and no longer than 180 seconds.")
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        raise ValueError("The final Short has no audio stream.")
    return duration


def prepare_package(directory: Path, fact: dict, segments: list[dict], privacy: str) -> list[Path]:
    from pipeline.upload_youtube import prepare_metadata
    video = directory / "video.mp4"
    duration = validate_short(video)
    metadata = prepare_metadata(fact, privacy, image_segments=segments)
    metadata_path = directory / "youtube_metadata.json"
    write_json(metadata_path, {**metadata, "duration_seconds": duration, "upload_mode": "video_only"})
    return [metadata_path]
