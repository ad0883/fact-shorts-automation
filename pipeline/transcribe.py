"""Transcribe narration locally and validate word timestamps for captions."""

import math
import re
from difflib import SequenceMatcher
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import WHISPER_MODEL_SIZE

_model = None


def get_model():
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("Install faster-whisper to generate word captions.") from exc
        print(f"Loading whisper model ({WHISPER_MODEL_SIZE})... this may take a moment on first run.")
        _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def transcribe_words(audio_path: Path, *, initial_prompt: str | None = None) -> list[dict]:
    """Return non-empty, ordered {word, start, end} records.

    ``initial_prompt`` can supply the narration text to help recognize unusual
    names; Whisper still determines timestamps from the audio itself.
    """
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise FileNotFoundError(f"Narration audio not found: {audio_path}")
    if audio_path.stat().st_size == 0:
        raise ValueError(f"Narration audio is empty: {audio_path}")
    if initial_prompt is not None and not isinstance(initial_prompt, str):
        raise ValueError("initial_prompt must be text.")

    options = {"word_timestamps": True, "language": "en", "condition_on_previous_text": False}
    if initial_prompt:
        options["initial_prompt"] = initial_prompt.strip()
    segments, _ = get_model().transcribe(str(audio_path), **options)
    words = []
    for segment in segments:
        for word in segment.words or []:
            token = word.word.strip() if isinstance(word.word, str) else ""
            if not token:
                continue
            try:
                start, end = float(word.start), float(word.end)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid caption timestamp for {token!r}.") from exc
            if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end < start:
                raise ValueError(f"Invalid caption interval for {token!r}: {start}–{end}.")
            words.append({"word": token, "start": start, "end": end})
    if not words:
        raise RuntimeError("No spoken words were detected in the narration. Check the audio before rendering.")
    # Use the renderer's normalization for zero-length ASR tokens and overlaps,
    # so both imported timings and Whisper output follow identical rules.
    from pipeline.build_captions import validate_words
    result = validate_words(words)
    if initial_prompt:
        # Some Whisper runs treat the supplied passage as already-spoken context
        # and omit the opening. One unprompted local retry can recover those words.
        expected = re.findall(r"\w+", initial_prompt.casefold())
        def score(items):
            actual = re.findall(r"\w+", " ".join(w["word"] for w in items).casefold())
            matching = sum(b.size for b in SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks())
            return matching/max(1,len(expected)), -items[0]["start"]
        if score(result)[0] < .9 or result[0]["start"] > 1.5:
            print("  Caption alignment: checking one unprompted transcription for missing opening words.", flush=True)
            alternative = transcribe_words(audio_path)
            if score(alternative) > score(result):
                result = alternative
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract word timestamps from narration audio.")
    parser.add_argument("audio_file", type=Path)
    args = parser.parse_args()
    for word in transcribe_words(args.audio_file):
        print(f"{word['start']:.2f}-{word['end']:.2f}: {word['word']}")
