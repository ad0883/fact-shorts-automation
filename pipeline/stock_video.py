"""Bounded Pexels footage downloads with subject matching and retained credits."""

import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

import requests

from pipeline.select_background import probe_media
from pipeline.visual_assets import choose_relevant_photo

logger = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 45 * 1024 * 1024


def trusted_video_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "videos.pexels.com"
                and p.port in {None, 443} and not p.username and not p.password
                and p.path.startswith("/video-files/") and p.path.endswith(".mp4")
                and not p.fragment)
    except (TypeError, ValueError):
        return False


def context_matches(context, title):
    """Reject an explicit contradiction; this is not general visual fact checking."""
    context, title = context.casefold(), title.casefold()
    if "bubble" in context and re.search(r"\b(free|floating|sphere|spherical)\b", context):
        if re.search(r"\b(foam|glass|drain|underwater|gum)\b", title):
            return False
    return True


def download_pexels_video(query, out_path, *, metadata=None, exclude_ids=(), context=""):
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return False
    temporary = None
    try:
        with requests.get("https://api.pexels.com/v1/videos/search", headers={"Authorization": key},
                          params={"query": query, "per_page": 8, "size": "medium", "orientation": "portrait"},
                          timeout=(5, 15), allow_redirects=False) as response:
            if response.status_code != 200:
                return False
            videos = response.json().get("videos", [])
        candidates = []
        for video in videos:
            if video.get("id") in exclude_ids:
                continue
            page = urlsplit(video.get("url", ""))
            if page.hostname != "www.pexels.com" or not page.path.startswith("/video/"):
                continue
            # Video responses have no photo-style alt field. The canonical page
            # slug identifies the pictured subject; never accept search rank alone.
            title = re.sub(r"-\d+/?$", "", page.path.removeprefix("/video/")).replace("-", " ")
            if not context_matches(context, title):
                continue
            duration = float(video.get("duration", 0))
            files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"
                     and trusted_video_url(f.get("link"))
                     and 480 <= min(f.get("width") or 0, f.get("height") or 0)
                     and max(f.get("width") or 0, f.get("height") or 0) <= 1920]
            if files and math.isfinite(duration) and 2 <= duration <= 90:
                source = min(files, key=lambda f: abs(min(f["width"], f["height"])-720))
                candidates.append({"title": title, "video": video, "source": source})
        chosen = choose_relevant_photo(query, candidates)
        if chosen is None:
            return False
        destination = Path(out_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = chosen["source"]["link"]
        with requests.get(url, stream=True, timeout=(5, 20), allow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get("Content-Type", "").split(";")[0] not in {"video/mp4", "application/octet-stream"}:
                return False
            expected = int(response.headers.get("Content-Length", "0"))
            if expected > MAX_VIDEO_BYTES:
                return False
            size, started = 0, time.monotonic()
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".stock-", suffix=".mp4", delete=False) as out:
                temporary = Path(out.name)
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_VIDEO_BYTES or time.monotonic()-started > 60:
                        raise ValueError("Footage exceeds download limit")
                    out.write(chunk)
            if not size or expected and size != expected:
                raise ValueError("Incomplete footage download")
        probe = probe_media(temporary)
        if not any(s.get("codec_type") == "video" for s in probe["streams"]):
            return False
        duration = float(probe.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0:
            return False
        temporary.replace(destination)
        temporary = None
        if metadata is not None:
            video = chosen["video"]
            author = video.get("user", {})
            metadata.update(provider="pexels", photo_id=video["id"], photo_url=video["url"],
                            asset_url=url, photographer=author.get("name", ""),
                            photographer_url=author.get("url", ""), alt=chosen["title"],
                            usage="illustrative_stock_video", duration_seconds=duration)
        return True
    except (requests.RequestException, OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        logger.warning("Stock footage unavailable; trying a subject photo")
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
