"""Read-only environment checks; never download models or contact providers."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

from config import settings


def check_environment(*, need_generation=True, need_tts=True, need_whisper=True,
                      need_footage=True, source_path: Path | None = None, upload=False,
                      upload_required=True, need_graphics=False) -> list[dict]:
    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        add(binary, path, path or f"Install {binary} and add it to PATH")
    if shutil.which("ffmpeg"):
        try:
            filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15, check=True)
            add("Subtitle renderer", " subtitles " in filters.stdout, "FFmpeg must include the subtitles filter (libass)")
            encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15, check=True)
            add("H.264 encoder", "libx264" in encoders.stdout, "FFmpeg must include libx264")
        except (OSError, subprocess.SubprocessError) as exc:
            add("FFmpeg capabilities", False, str(exc))

    uses_gemini_speech = need_tts and settings.TTS_PROVIDER == "gemini"
    for module, package, required in (("requests", "requests", need_generation or uses_gemini_speech),
                                      ("faster_whisper", "faster-whisper", need_whisper),
                                      ("PIL", "Pillow", need_graphics)):
        found = importlib.util.find_spec(module) is not None
        if required:
            add(package, found, "Installed" if found else f"Install with: python -m pip install {package}")
    if need_tts:
        modules = {"gemini": "requests", "edge": "edge_tts", "gtts": "gtts"}
        module = modules.get(settings.TTS_PROVIDER)
        speech = bool(module and importlib.util.find_spec(module) is not None)
        add("Speech provider", speech, settings.TTS_PROVIDER if speech else "Select TTS_PROVIDER=gemini, edge, or gtts and install its dependencies")
        add("Speech fallback", settings.TTS_FALLBACK_PROVIDER in {"none", "edge", "gtts"}, settings.TTS_FALLBACK_PROVIDER)
    if need_generation or uses_gemini_speech:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        alternative = "provide a script and --audio, or select another TTS_PROVIDER" if uses_gemini_speech else "use --script"
        add("Gemini API key", bool(key and key != "your_key_here"), "Configured" if key and key != "your_key_here" else f"Set GEMINI_API_KEY in .env, or {alternative}")
    if need_footage:
        candidates = ([source_path] if source_path else
                      [p for p in settings.ASSETS_DIR.glob("*") if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}])
        good = [p for p in candidates if p.is_file() and p.stat().st_size > 0]
        add("Gameplay footage", bool(good), str(source_path or settings.ASSETS_DIR) if good else "Add footage to assets/gameplay, pass --background, or try --demo")
    if upload:
        upload_start = len(checks)
        for module, package in (("googleapiclient", "google-api-python-client"), ("google_auth_oauthlib", "google-auth-oauthlib")):
            installed = importlib.util.find_spec(module) is not None
            add(package, installed, "Installed" if installed else "Install requirements-youtube.txt")
        has_token = settings.YOUTUBE_TOKEN_FILE.is_file()
        setup = "Run: venv/bin/python main.py --connect-youtube"
        if not settings.YOUTUBE_CLIENT_SECRET_FILE.is_file():
            setup = f"Save a Desktop OAuth client as {settings.YOUTUBE_CLIENT_SECRET_FILE}, then run: venv/bin/python main.py --connect-youtube"
        add("YouTube credentials", has_token, "Saved OAuth token available" if has_token else setup)
        add("YouTube visibility", settings.YOUTUBE_PRIVACY_STATUS in {"private", "unlisted", "public"}, settings.YOUTUBE_PRIVACY_STATUS)
        for check in checks[upload_start:]:
            check.update(upload=True, required=upload_required)
    return checks


def print_checks(checks: list[dict]) -> bool:
    for check in checks:
        mark = "OK" if check["ok"] else ("FAIL" if check["required"] else "WARN")
        print(f"[{mark}] {check['name']}: {check['detail']}")
    return all(c["ok"] or not c["required"] for c in checks)
