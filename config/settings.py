"""
Central settings for the fact-shorts pipeline. Edit values here rather
than digging through pipeline code.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    _env_file = PROJECT_ROOT / ".env"
    if _env_file.exists():
        with open(_env_file, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v

def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_path(name: str, default: Path) -> Path:
    path = Path(os.environ.get(name, str(default))).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


ASSETS_DIR = env_path("GAMEPLAY_DIR", PROJECT_ROOT / "assets" / "gameplay")
OUTPUT_DIR = env_path("OUTPUT_DIR", PROJECT_ROOT / "output")

# Vertical video for Shorts/Reels/TikTok
VIDEO_WIDTH = env_int("VIDEO_WIDTH", 1080, 180, 2160)
VIDEO_HEIGHT = env_int("VIDEO_HEIGHT", 1920, 320, 3840)
VIDEO_FPS = env_int("VIDEO_FPS", 30, 12, 60)
if VIDEO_WIDTH % 2 or VIDEO_HEIGHT % 2:
    raise ValueError("VIDEO_WIDTH and VIDEO_HEIGHT must be even for H.264 output")

# faster-whisper model size: "tiny", "base", "small", "medium".
# "base" is a good speed/accuracy tradeoff on a laptop CPU.
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# Specific topics encourage curiosity without directing the model to invent drama.
FACT_CATEGORIES = [
    "animal adaptations and unusual ecosystems",
    "deep ocean exploration and marine biology",
    "documented historical discoveries and inventions",
    "space and astronomy",
    "geology and Earth's natural phenomena",
    "perception, memory, and cognitive science",
    "archaeology and ancient technology",
    "everyday physics and materials science",
]


# Caption styling (used when building the .ass subtitle file)
CAPTION_FONT = os.environ.get("CAPTION_FONT", "Arial Black")
CAPTION_FONT_SIZE = env_int("CAPTION_FONT_SIZE", round(VIDEO_WIDTH / 12), 16, 180)
CAPTION_COLOR = "&H00FFFFFF"       # white, ASS format is &HAABBGGRR
CAPTION_HIGHLIGHT_COLOR = "&H0000D7FF"  # gold/yellow accent
CAPTION_OUTLINE_COLOR = "&H00000000"    # black outline

# Expressive narration; basic speech is an explicit choice, never a silent downgrade.
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "gemini").strip().lower()
TTS_FALLBACK_PROVIDER = os.environ.get("TTS_FALLBACK_PROVIDER", "none").strip().lower()
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
GEMINI_TTS_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Achird")
TTS_STYLE = os.environ.get("TTS_STYLE", (
    "Warm, conversational storytelling, like explaining something fascinating to a friend. "
    "Use connected phrasing and varied intonation, with gentle emphasis on the key discovery. "
    "Speak at an easy, engaged pace, roughly 155 to 170 words per minute. "
    "Use brief pauses at sentence boundaries and a small pause after the opening hook. "
    "Let the final sentence land naturally. Avoid a sales pitch, announcer delivery, "
    "exaggerated excitement, and choppy pauses between words."
))
# Optional Edge voice settings (used only when Edge is selected).
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AndrewNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")
TTS_PITCH = os.environ.get("TTS_PITCH", "+0Hz")

# Image overlay settings (contextual images from Pexels)
VISUAL_LAYOUT = os.environ.get("VISUAL_LAYOUT", "story").strip().lower()
CINEMATIC_SPLIT = 0.48  # Fact visual above; gameplay fills the remaining height.
if VISUAL_LAYOUT not in {"story", "cinematic", "cards"}:
    raise ValueError("VISUAL_LAYOUT must be story, cinematic or cards")
IMAGE_WIDTH = round(VIDEO_WIDTH * 0.65)
IMAGE_Y_POS = round(VIDEO_HEIGHT * 0.1)
IMAGE_CORNER_RADIUS = 30    # Rounded corner radius in pixels
IMAGE_FADE_DURATION = 0.3   # Seconds for fade-in / fade-out transitions

# YouTube Upload configuration (YouTube Data API v3)
YOUTUBE_CLIENT_SECRET_FILE = PROJECT_ROOT / os.environ.get("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
YOUTUBE_TOKEN_FILE = PROJECT_ROOT / os.environ.get("YOUTUBE_TOKEN_FILE", "token.json")
YOUTUBE_PRIVACY_STATUS = os.environ.get("YOUTUBE_PRIVACY_STATUS", "public")  # "private", "unlisted", or "public"
YOUTUBE_CATEGORY_ID = os.environ.get("YOUTUBE_CATEGORY_ID", "27")              # 27 = Education, 28 = Science & Tech
AUTO_UPLOAD_YOUTUBE = os.environ.get("AUTO_UPLOAD_YOUTUBE", "true").lower() in ("true", "1", "yes")
YOUTUBE_CHANNEL_FILE = env_path("YOUTUBE_CHANNEL_FILE", PROJECT_ROOT / "youtube_channel.json")
YOUTUBE_LANGUAGE = os.environ.get("YOUTUBE_LANGUAGE", "en").strip()

# Snapchat is opt-in until Public Profile API access has been approved.
AUTO_UPLOAD_SNAPCHAT = os.environ.get("AUTO_UPLOAD_SNAPCHAT", "false").lower() in ("true", "1", "yes")

AUTO_UPLOAD_INSTAGRAM = os.environ.get("AUTO_UPLOAD_INSTAGRAM", "false").lower() in ("true", "1", "yes")

AUTO_UPLOAD_FACEBOOK = os.environ.get("AUTO_UPLOAD_FACEBOOK", "false").lower() in ("true", "1", "yes")
