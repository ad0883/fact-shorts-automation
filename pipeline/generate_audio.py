"""Generate narration with bounded retries and atomic output replacement."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import (TTS_VOICE, TTS_RATE, TTS_PITCH, TTS_PROVIDER,
                             TTS_FALLBACK_PROVIDER, GEMINI_TTS_MODEL,
                             GEMINI_TTS_VOICE, TTS_STYLE)


async def _generate_audio_edge(
    text: str,
    out_path: Path,
    voice: str,
    rate: str,
    pitch: str,
) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
    await communicate.save(str(out_path))


async def _generate_with_timeout(text, path, voice, rate, pitch, timeout):
    await asyncio.wait_for(
        _generate_audio_edge(text, path, voice=voice, rate=rate, pitch=pitch),
        timeout=timeout,
    )


def _verify_audio(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("The speech provider returned an empty audio file.")


def _generate_legacy(text, path, *, provider, voice, rate, pitch, max_attempts, timeout):
    attempts = max_attempts if provider == "edge" else min(max_attempts, 2)
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            if provider == "edge":
                asyncio.run(_generate_with_timeout(text, path, voice, rate, pitch, timeout))
            else:
                from gtts import gTTS
                gTTS(text=text, lang="en", slow=False, timeout=(10, min(timeout, 60))).save(str(path))
            _verify_audio(path)
            return
        except (ImportError, ValueError):
            raise
        except Exception as exc:
            if attempt + 1 == attempts or getattr(exc, "status", None) in {400, 401, 403, 404}:
                raise
            time.sleep(min(2 ** attempt, 8))


def generate_audio(
    text: str,
    out_path: Path,
    voice: str = TTS_VOICE,
    rate: str = TTS_RATE,
    pitch: str = TTS_PITCH,
    *,
    provider: str = TTS_PROVIDER,
    fallback: str = TTS_FALLBACK_PROVIDER,
    gemini_model: str = GEMINI_TTS_MODEL,
    gemini_voice: str = GEMINI_TTS_VOICE,
    style: str = TTS_STYLE,
    max_attempts: int = 3,
    timeout: float = 180,
) -> dict:
    """Write a complete MP3, preserving an existing file if all providers fail.

    Gemini provides expressive narration by default. Edge and basic gTTS are
    explicit alternatives; fallbacks must be configured. Return the actual
    provider settings for the run's narration metadata.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Narration text must be a non-empty string.")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer.")
    if not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a positive finite number.")
    if provider not in {"gemini", "edge", "gtts"}:
        raise ValueError("TTS_PROVIDER must be gemini, edge, or gtts")
    if fallback not in {"none", "edge", "gtts"}:
        raise ValueError("TTS_FALLBACK_PROVIDER must be none, edge, or gtts")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{out_path.stem}-", suffix=".mp3", dir=out_path.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    providers = [provider] + ([fallback] if fallback not in {"none", provider} else [])
    failures = []
    try:
        for selected in providers:
            try:
                temporary_path.unlink(missing_ok=True)
                if selected == "gemini":
                    from pipeline.gemini_speech import generate_speech
                    print(f"  Narration: Gemini / {gemini_voice} ({gemini_model})", flush=True)
                    generate_speech(text.strip(), temporary_path, model=gemini_model, voice=gemini_voice,
                                    style=style, max_attempts=max_attempts, timeout=timeout)
                    metadata = {"provider": selected, "model": gemini_model, "voice": gemini_voice, "style": style}
                else:
                    print(f"  Narration: {selected} / {voice if selected == 'edge' else 'English (basic voice)'}", flush=True)
                    _generate_legacy(text.strip(), temporary_path, provider=selected, voice=voice,
                                     rate=rate, pitch=pitch, max_attempts=max_attempts, timeout=timeout)
                    metadata = {"provider": selected, "voice": voice if selected == "edge" else "en"}
                    if selected == "edge":
                        metadata.update(rate=rate, pitch=pitch)
                _verify_audio(temporary_path)
                os.replace(temporary_path, out_path)
                metadata["fallback_used"] = selected != provider
                return metadata
            except Exception as exc:
                # Edge exceptions contain signed URLs; never print their raw messages.
                detail = str(exc) if selected == "gemini" and isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
                status = getattr(exc, "status", None)
                failures.append(f"{selected}: {detail}" + (f" (HTTP {status})" if status else ""))
                if selected != providers[-1]:
                    print(f"  {selected} narration unavailable; using configured {fallback} fallback.", file=sys.stderr)
        raise RuntimeError(
            "Narration generation failed. " + "; ".join(failures)
            + ". Retry, provide --audio, or explicitly choose TTS_PROVIDER=edge or gtts."
        ) from None
    finally:
        temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate narration audio from text.")
    parser.add_argument("text", help="Narration to speak")
    parser.add_argument("--output", type=Path, default=Path("narration.mp3"))
    args = parser.parse_args()
    generate_audio(args.text, args.output)
    print(f"Saved to {args.output}")
