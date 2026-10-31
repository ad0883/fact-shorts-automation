"""Readable phrase captions with an active-word highlight, plus portable SRT."""

from __future__ import annotations

import html
import math
import re
import textwrap
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import (
    VIDEO_WIDTH, VIDEO_HEIGHT, CAPTION_FONT, CAPTION_FONT_SIZE,
    CAPTION_COLOR, CAPTION_HIGHLIGHT_COLOR, CAPTION_OUTLINE_COLOR,
    VISUAL_LAYOUT, CINEMATIC_SPLIT,
)

ASS_HEADER = f"""[Script Info]
Title: Fact Short Captions
ScriptType: v4.00+
PlayResX: {VIDEO_WIDTH}
PlayResY: {VIDEO_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{CAPTION_FONT},{CAPTION_FONT_SIZE},{CAPTION_COLOR},{CAPTION_HIGHLIGHT_COLOR},{CAPTION_OUTLINE_COLOR},&H80000000,-1,0,0,0,100,100,0,0,1,5,2,5,80,80,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _time_units(seconds: float, scale: int) -> int:
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Caption times must be finite and non-negative.")
    return round(seconds * scale)


def format_ass_time(seconds: float) -> str:
    centiseconds = _time_units(seconds, 100)
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{fraction:02d}"


def format_srt_time(seconds: float) -> str:
    milliseconds = _time_units(seconds, 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{fraction:03d}"


def clean_word(word: str) -> str:
    # ASS override blocks and \N sequences must never be interpreted as code.
    return re.sub(r"\s+", " ", str(word).replace("{", "").replace("}", "")
                  .replace("\\", "＼")).strip()


def validate_words(words: list[dict]) -> list[dict]:
    """Validate timings and return sanitized copies with no overlapping words.

    Slight timestamp overlaps and zero-length ASR tokens are repaired; invalid
    numbers, reversed timing, and out-of-order words are reported explicitly.
    """
    if not isinstance(words, list):
        raise ValueError("Word timings must be a list of word/start/end objects.")
    normalized = []
    last_start = -1.0
    for index, word in enumerate(words):
        try:
            if not isinstance(word["word"], str):
                raise ValueError("word must be text")
            text = clean_word(word["word"])
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid word timing at index {index}: expected word, start, and end.") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError(f"Invalid word timing at index {index}: require 0 <= start <= end.")
        if start < last_start:
            raise ValueError(f"Word timings are out of order at index {index}.")
        last_start = start
        if not text:
            continue
        if normalized:
            previous = normalized[-1]
            # ASR can assign several tokens the same timestamp. Keep every
            # token visible for at least one representable ASS interval.
            start = max(start, previous["start"] + 0.01)
            previous["end"] = min(previous["end"], start)
        normalized.append({"word": text, "start": start, "end": max(end, start + 0.01)})
    if not normalized:
        raise ValueError("No spoken words were available to build captions.")
    return normalized


def _phrases(words: list[dict], max_words: int = 4) -> list[list[dict]]:
    groups: list[list[dict]] = []
    group: list[dict] = []
    for word in words:
        if group and (len(group) >= max_words
                      or len(" ".join(w["word"] for w in group) + " " + word["word"]) > 28
                      or word["start"] - group[-1]["end"] > 0.45
                      or word["end"] - group[0]["start"] > 2.8
                      or re.search(r"[.!?;:]$", group[-1]["word"])):
            groups.append(group)
            group = []
        group.append(word)
    if group:
        groups.append(group)
    return groups


def _color(value: str) -> str:
    return "&H" + value.removeprefix("&H").rstrip("&")[-6:] + "&"


def _phrase_text(group: list[dict], active: int) -> tuple[str, int]:
    words = [w["word"].upper() for w in group]
    split = None
    if len(" ".join(words)) > 18 and len(words) > 1:
        split = min(range(1, len(words)), key=lambda i: abs(
            len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    lines = [" ".join(words)] if split is None else [" ".join(words[:split]), " ".join(words[split:])]
    font_size = min(CAPTION_FONT_SIZE, max(12, int(VIDEO_WIDTH * 0.82 / (max(map(len, lines)) * 0.65))))
    pieces = []
    for index, word in enumerate(words):
        if index:
            pieces.append(r"\N" if index == split else " ")
        color = CAPTION_HIGHLIGHT_COLOR if index == active else CAPTION_COLOR
        pieces.append(f"{{\\c{_color(color)}}}{word}")
    return "".join(pieces), font_size


def build_ass_captions(words: list[dict], out_path: Path, *, style: str = "phrase", image_segments: list[dict] | None = None) -> None:
    """Write phrase highlights by default; style='word' keeps the pop-in look."""
    if style not in {"phrase", "word"}:
        raise ValueError("Caption style must be 'phrase' or 'word'.")
    words = validate_words(words)
    lines = [ASS_HEADER]
    cinematic = VISUAL_LAYOUT in {"story", "cinematic"} and bool(image_segments)
    if cinematic:
        for scene in image_segments:
            provenance = scene.get("provenance", {})
            if provenance.get("provider") == "nasa":
                label = "NASA / ARTIST CONCEPT" if provenance.get("usage") == "artist_concept" else "NASA / ARCHIVE"
                tag = f"{{\\an7\\pos({round(VIDEO_WIDTH*.035)},{round(VIDEO_HEIGHT*.022)})\\fs{round(VIDEO_WIDTH*.021)}\\bord1\\shad0}}"
                lines.append(f"Dialogue: 2,{format_ass_time(scene['start'])},{format_ass_time(scene['end'])},Word,,0,0,0,,{tag}{label}")
            elif provenance.get("provider") == "commons":
                credit = clean_word(f"{provenance.get('photographer', 'Wikimedia Commons')} / {provenance.get('license', '')}")
                credit_lines = textwrap.wrap(credit, width=75)
                if provenance.get("usage") == "reconstruction":
                    credit_lines.insert(0, "RECONSTRUCTION / MODEL")
                label = r"\N".join(credit_lines)
                tag = f"{{\\an7\\pos({round(VIDEO_WIDTH*.035)},{round(VIDEO_HEIGHT*.022)})\\fs{round(VIDEO_WIDTH*.021)}\\bord1\\shad0}}"
                lines.append(f"Dialogue: 2,{format_ass_time(scene['start'])},{format_ass_time(scene['end'])},Word,,0,0,0,,{tag}{label}")
    for group in _phrases(words, max_words=1 if style == "word" else 2):
        for index, word in enumerate(group):
            end = group[index + 1]["start"] if index + 1 < len(group) else word["end"]
            end = max(end, word["start"] + 0.01)
            text, font_size = _phrase_text(group, index)
            if cinematic:
                font_size = min(font_size, round(VIDEO_WIDTH * .073))
            boundaries = [word["start"], end]
            if VISUAL_LAYOUT == "story":
                boundaries += [s[key] for s in image_segments or [] for key in ("start", "end") if word["start"] < s[key] < end]
            boundaries = sorted(set(boundaries))
            for lo, hi in zip(boundaries, boundaries[1:]):
                if format_ass_time(lo) == format_ass_time(hi):
                    continue
                full = VISUAL_LAYOUT == "story" and any(s.get("layout") == "full" and s["start"] <= (lo+hi)/2 < s["end"] for s in image_segments or [])
                y = .78 if full else CINEMATIC_SPLIT if cinematic else .72
                tag = f"{{\\an5\\pos({VIDEO_WIDTH // 2},{round(VIDEO_HEIGHT * y)})\\fs{font_size}}}"
                if style == "word":
                    pop_ms = max(1, min(100, int((hi-lo)*400)))
                    tag += f"{{\\fscx70\\fscy70\\t(0,{pop_ms},\\fscx100\\fscy100)}}"
                lines.append(f"Dialogue: 0,{format_ass_time(lo)},{format_ass_time(hi)},Word,,0,0,0,,{tag}{text}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_srt_captions(words: list[dict], out_path: Path) -> None:
    """Write readable phrase-level subtitles for accessibility and re-editing."""
    groups = _phrases(validate_words(words))
    blocks = []
    for index, group in enumerate(groups, 1):
        text = html.escape(" ".join(word["word"] for word in group), quote=False)
        blocks.append(f"{index}\n{format_srt_time(group[0]['start'])} --> {format_srt_time(group[-1]['end'])}\n{text}\n")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(blocks), encoding="utf-8")


if __name__ == "__main__":
    build_ass_captions([
        {"word": "This", "start": 0, "end": 0.3},
        {"word": "is", "start": 0.3, "end": 0.5},
        {"word": "a", "start": 0.5, "end": 0.6},
        {"word": "test.", "start": 0.6, "end": 1.0},
    ], Path("test_captions.ass"))
