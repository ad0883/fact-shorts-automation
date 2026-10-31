"""Expressive Gemini 3.8 speech, with bounded downloads and verified audio conversion.

REST contract: https://ai.google.dev/gemini-api/docs/generate-content/speech-generation
"""

import base64
import binascii
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import wave

import requests

from pipeline.gemini_client import GeminiError, TRANSIENT_STATUS, _error_detail, _retry_delay

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 12 * 1024 * 1024


def _extract_wav(payload: object) -> bytes:
    """Reject partial, non-audio, malformed, or excessively long speech responses."""
    try:
        candidate = payload["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise ValueError
        parts = candidate["content"]["parts"]
        clips = [p["inlineData"] for p in parts if "inlineData" in p and not p.get("thought")]
        if len(clips) != 1 or clips[0].get("mimeType", "").split(";")[0].lower() not in {"audio/wav", "audio/x-wav"}:
            raise ValueError
        data = base64.b64decode(clips[0]["data"], validate=True)
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise ValueError
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.getnframes()
            if (reader.getnchannels() not in {1, 2} or reader.getsampwidth() != 2
                    or not 8000 <= reader.getframerate() <= 96000
                    or not 0 < frames / reader.getframerate() <= 180):
                raise ValueError
            expected = frames * reader.getnchannels() * reader.getsampwidth()
            if len(reader.readframes(frames)) != expected:
                raise ValueError
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, binascii.Error, wave.Error, EOFError):
        raise GeminiError("Gemini speech returned incomplete or invalid WAV audio (maximum duration: 180 seconds).") from None
    return data


def generate_speech(text: str, out_path: Path, *, model: str, voice: str, style: str,
                    max_attempts: int = 3, timeout: float = 180) -> None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or key == "your_key_here":
        raise GeminiError("Set GEMINI_API_KEY to use Gemini narration.")
    model = model.removeprefix("models/")
    if not re.fullmatch(r"gemini-3\.8-[A-Za-z0-9._-]+-tts", model):
        raise ValueError("GEMINI_TTS_MODEL must be a Gemini 3.8 TTS model, such as gemini-3.8-flash-tts")
    if not voice.strip() or not style.strip() or len(style) > 3000:
        raise ValueError("Gemini speech needs a voice and a delivery style of 1-3000 characters")
    payload = {
        "contents": [{"role": "user", "parts": [{
            "text": text, "speech_metadata": {"style": style},
        }]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": voice}},
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for attempt in range(max_attempts):
        delay = float(2 ** attempt)
        try:
            with requests.post(url, headers={"x-goog-api-key": key}, json=payload,
                               timeout=(10, timeout), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    if response.status_code not in TRANSIENT_STATUS or attempt + 1 == max_attempts:
                        detail = _error_detail(response, key)
                        raise GeminiError(f"Gemini speech failed (HTTP {response.status_code}). {detail}".strip())
                    delay = _retry_delay(response, attempt)
                else:
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise GeminiError("Gemini speech response exceeded the size limit")
                    try:
                        decoded = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        raise GeminiError("Gemini speech returned an invalid API response") from None
                    wav = _extract_wav(decoded)
                    break
        except requests.RequestException:
            if attempt + 1 == max_attempts:
                raise GeminiError("Could not reach Gemini speech; check your network and retry.") from None
        time.sleep(delay)
    else:
        raise GeminiError("Gemini speech did not complete")

    # The provider's WAV is decoded before conversion, never mislabeled as MP3.
    with tempfile.TemporaryDirectory(prefix=".speech-", dir=out_path.parent) as directory:
        source = Path(directory) / "speech.wav"
        source.write_bytes(wav)
        try:
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000",
                "-c:a", "libmp3lame", "-b:a", "192k", str(out_path),
            ], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("Could not convert Gemini narration; check FFmpeg and retry.") from None
        if result.returncode or not out_path.is_file() or not out_path.stat().st_size:
            raise RuntimeError("FFmpeg could not convert the generated narration to MP3.")
