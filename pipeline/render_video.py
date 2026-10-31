"""Burn readable captions and timed image cards into a mobile-ready MP4."""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import VIDEO_WIDTH, VIDEO_HEIGHT, IMAGE_WIDTH, IMAGE_Y_POS, IMAGE_FADE_DURATION, VISUAL_LAYOUT, CINEMATIC_SPLIT
from pipeline.select_background import get_duration_seconds, probe_media, run_media_command, video_filter, video_fps


def escape_for_ffmpeg_filter(path: Path) -> str:
    """Escape a path for an *unquoted* FFmpeg filter option.

    There are two escaping levels: option parsing and filtergraph parsing.
    The renderer avoids both by staging captions under a fixed, safe filename.
    """
    value = str(Path(path).resolve())
    value = "".join("\\" + character if character in "\\':" else character for character in value)
    return "".join("\\" + character if character in "\\'[],;" else character for character in value)


def _normalize_image_segments(image_segments: list[dict], total_duration: float) -> list[dict]:
    normalized = []
    for index, segment in enumerate(image_segments):
        try:
            start, end = float(segment["start"]), float(segment["end"])
            path = Path(segment["image_path"]).expanduser().resolve()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid image segment at index {index}.") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"Invalid image timing at index {index}: require 0 <= start < end.")
        if start >= total_duration:
            continue
        if not path.is_file() or not path.stat().st_size:
            raise FileNotFoundError(f"Overlay image is missing or empty: {path}")
        normalized.append({**segment, "image_path": str(path), "start": start, "end": min(end, total_duration)})
    return normalized


def _build_image_overlay_filter(image_segments: list[dict], total_duration: float) -> tuple[list[str], str]:
    """Return image inputs and an overlay chain rooted at input zero.

    Cards are constrained to the upper portion of the frame so portrait images
    cannot cover captions. All inputs share the same frame rate and time base.
    """
    if not image_segments:
        return [], ""
    image_segments = _normalize_image_segments(image_segments, total_duration)
    if not image_segments:
        return [], ""
    if VISUAL_LAYOUT in {"story", "cinematic"}:
        return _cinematic_overlay_filter(image_segments)
    input_args: list[str] = []
    filter_parts = [f"[0:v]{video_filter()},setpts=PTS-STARTPTS[base]"]
    image_width = max(2, min(IMAGE_WIDTH, int(VIDEO_WIDTH * 0.86)) // 2 * 2)
    image_height = max(2, int(VIDEO_HEIGHT * 0.40) // 2 * 2)
    y_pos = max(0, min(IMAGE_Y_POS, round(VIDEO_HEIGHT * 0.12)))
    for index, segment in enumerate(image_segments):
        source_options = ["-stream_loop", "-1"] if segment.get("media_type") == "video" else ["-loop", "1", "-framerate", str(video_fps())]
        input_args.extend([*source_options, "-t", f"{total_duration+.1:.6f}", "-i", segment["image_path"]])
        start, end = segment["start"], segment["end"]
        fade = max(0.0, min(IMAGE_FADE_DURATION, (end - start) * 0.3))
        offset = start if segment.get("media_type") == "video" else 0
        image_filter = (f"[{index + 2}:v]setpts=PTS-STARTPTS+{offset:.6f}/TB,"
                        f"scale={image_width}:{image_height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        "setsar=1,format=rgba")
        if fade:
            image_filter += (f",fade=t=in:st={start:.6f}:d={fade:.6f}:alpha=1"
                             f",fade=t=out:st={end - fade:.6f}:d={fade:.6f}:alpha=1")
        filter_parts.append(image_filter + f"[img{index}]")
    current = "[base]"
    for index, segment in enumerate(image_segments):
        label = f"v{index}" if index < len(image_segments) - 1 else "vimg"
        filter_parts.append(
            f"{current}[img{index}]overlay=x=(W-w)/2:y={y_pos}:"
            f"eof_action=pass:shortest=0:enable='between(t,{segment['start']:.6f},{segment['end']:.6f})'[{label}]"
        )
        current = f"[{label}]"
    return input_args, ";\n".join(filter_parts)


def _cinematic_overlay_filter(segments):
    """Two balanced panels with captions across the seam, preserving more gameplay."""
    width = VIDEO_WIDTH
    panel = int(VIDEO_HEIGHT * CINEMATIC_SPLIT) // 2 * 2
    game_y = panel
    game_h = VIDEO_HEIGHT - game_y
    fps = video_fps()
    inputs = []
    filters = [f"[0:v]{video_filter()},setpts=PTS-STARTPTS,split=2[canvas][game]",
               f"[game]crop={width}:{game_h}:0:(ih-oh)/2[gamecrop]",
               f"[canvas][gamecrop]overlay=x=0:y={game_y}[base]"]
    current = "[base]"
    for index, segment in enumerate(segments):
        start, end = segment["start"], segment["end"]
        # Use one common frame grid for both source PTS and overlay visibility.
        # Keeping the source alive through its gated endpoint prevents framesync
        # from exposing the gameplay on a fractional final frame.
        first = math.ceil(start*fps-1e-7)
        stop = math.ceil(end*fps-1e-7)
        duration = max(1, stop-first)/fps
        scene_height = VIDEO_HEIGHT if VISUAL_LAYOUT == "story" and segment.get("layout") == "full" else panel
        is_video = segment.get("media_type") == "video"
        source_options = ["-stream_loop", "-1"] if is_video else ["-loop", "1", "-framerate", str(fps)]
        if is_video and segment.get("source_offset", 0):
            source_options.extend(["-ss", f"{float(segment['source_offset']):.6f}"])
        inputs.extend([*source_options, "-t", f"{duration+2/fps:.6f}", "-i", segment["image_path"]])
        # Keep diagrams intact against a quiet background. Enlarged blurred
        # copies competed with the image and made the panel feel letterboxed.
        if segment.get("fit", "contain") == "contain":
            filters.append(f"[{index+2}:v]scale={width}:{scene_height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                           f"pad={width}:{scene_height}:(ow-iw)/2:(oh-ih)/2:color=0x080b12,setsar=1[plate{index}]")
        else:
            filters.append(f"[{index+2}:v]scale={width}:{scene_height}:force_original_aspect_ratio=increase,"
                           f"crop={width}:{scene_height}:(iw-ow)/2:(ih-oh)*0.28,setsar=1[plate{index}]")
        # Gentle motion starts at each scene, not at the beginning of the whole film.
        # Keep data graphics still so their labels don't creep off the edges.
        progress = f"min(on/{max(1, duration*fps):.6f},1)"
        camera = segment.get("camera", "push")
        zoom = "1" if segment.get("fit", "contain") == "contain" else (
            f"1.13-0.11*{progress}" if camera=="pull" else
            "1.12" if camera.startswith("pan_") else f"1.02+0.11*{progress}")
        x = (f"(iw-iw/zoom)*{progress}" if camera=="pan_right" else
             f"(iw-iw/zoom)*(1-{progress})" if camera=="pan_left" else "iw/2-iw/zoom/2")
        # Hard editorial cuts keep the fact panel solid; fading to the gameplay
        # beneath would produce a distracting flash at every scene change.
        motion = f"fps={fps}" if is_video else (
            f"scale={width*2}:{scene_height*2},"
            f"zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d=1:s={width}x{scene_height}:fps={fps}")
        if is_video and segment.get("fit") == "cover" and camera in {"pull", "pan_right"}:
            # A real change in framing at the cut, not a metadata-only division
            # of continuous footage. Keep contain-mode diagrams untouched.
            close_w,close_h=round(width*1.12)//2*2,round(scene_height*1.12)//2*2
            motion += f",scale={close_w}:{close_h},crop={width}:{scene_height}:(iw-ow)/2:(ih-oh)/2"
        filters.append(f"[plate{index}]{motion},format=rgba,settb=1/{fps},setpts=PTS-STARTPTS+{first}[img{index}]")
        label = "vimg" if index == len(segments)-1 else f"scene{index}"
        filters.append(f"{current}[img{index}]overlay=x=0:y=0:eof_action=repeat:repeatlast=1:shortest=0:"
                       f"enable='gte(t*{fps},{first}-.001)*lt(t*{fps},{stop}-.001)'[{label}]")
        current = f"[{label}]"
    return inputs, ";\n".join(filters)


def render_final_video(background_path: Path, audio_path: Path, captions_path: Path,
                       out_path: Path, image_segments: list[dict] | None = None) -> None:
    """Render exactly the narration's duration, safely handling arbitrary paths.

    Both branches map the narration explicitly, loop shorter video sources, and
    write the same H.264/yuv420p/AAC format. A failed render preserves any prior
    output and removes temporary files.
    """
    background_path = Path(background_path).expanduser().resolve()
    audio_path = Path(audio_path).expanduser().resolve()
    captions_path = Path(captions_path).expanduser().resolve()
    out_path = Path(out_path).expanduser().resolve()
    if out_path in {background_path, audio_path, captions_path}:
        raise ValueError("Final output must not overwrite an input file.")
    if not captions_path.is_file() or not captions_path.stat().st_size:
        raise FileNotFoundError(f"Caption file is missing or empty: {captions_path}")
    background = probe_media(background_path)
    audio = probe_media(audio_path)
    if not any(stream.get("codec_type") == "video" for stream in background["streams"]):
        raise ValueError("The background must contain a video stream.")
    if not any(stream.get("codec_type") == "audio" for stream in audio["streams"]):
        raise ValueError("The narration must contain an audio stream.")
    total_duration = get_duration_seconds(audio_path)
    image_inputs, image_filters = _build_image_overlay_filter(image_segments or [], total_duration)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".render-", dir=out_path.parent) as temp_dir:
        work_dir = Path(temp_dir)
        # Never interpolate an arbitrary filesystem path into filter syntax.
        shutil.copyfile(captions_path, work_dir / "captions.ass")
        output = work_dir / "video.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-stream_loop", "-1", "-i", str(background_path), "-i", str(audio_path), *image_inputs]
        if image_filters:
            command.extend(["-filter_complex", image_filters + ";\n[vimg]subtitles=filename=captions.ass[vout]",
                            "-map", "[vout]"])
        else:
            command.extend(["-vf", video_filter() + ",setpts=PTS-STARTPTS,subtitles=filename=captions.ass",
                            "-map", "0:v:0"])
        command.extend([
            "-map", "1:a:0", "-t", f"{total_duration:.6f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-af", "aresample=async=1:first_pts=0", "-movflags", "+faststart",
            "-map_metadata", "-1", str(output),
        ])
        run_media_command(command, cwd=work_dir)
        metadata = probe_media(output)
        stream_types = {stream.get("codec_type") for stream in metadata["streams"]}
        if not {"video", "audio"}.issubset(stream_types):
            raise RuntimeError("Rendered output is missing a video or narration stream.")
        rendered_duration = get_duration_seconds(output)
        if abs(rendered_duration - total_duration) > max(0.2, 2 / video_fps()):
            raise RuntimeError(f"Rendered duration ({rendered_duration:.2f}s) does not match narration ({total_duration:.2f}s).")
        output.replace(out_path)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit("Usage: python render_video.py <background.mp4> <audio.mp3> <captions.ass> <out.mp4>")
    render_final_video(*(Path(argument) for argument in sys.argv[1:5]))
    print(f"Saved to {sys.argv[4]}")
