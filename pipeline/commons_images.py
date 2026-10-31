"""Bounded Wikimedia Commons image lookup with source and license metadata.

API: https://www.mediawiki.org/wiki/API:Imageinfo
"""

import html
import json
import logging
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, urlsplit

import requests

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "FactShortsAutomation/1.0 (local educational video tool)"}
MAX_METADATA_BYTES = 2 * 1024 * 1024


def plain_text(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def trusted_asset(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"upload.wikimedia.org", "thumb.wikimedia.org"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and parsed.path.startswith("/wikipedia/commons/")
                and parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    except (ValueError, TypeError):
        return False


def search_commons_images(query, *, thumbnail_width=1280):
    """Find reusable photos of the subject, retaining attribution for every asset."""
    from pipeline.visual_assets import _terms, choose_relevant_photo
    params = {"action": "query", "generator": "search", "gsrsearch": query,
              "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo",
              "iiprop": "url|extmetadata", "iiurlwidth": thumbnail_width, "format": "json",
              "iiextmetadatalanguage": "en"}
    try:
        with requests.get("https://commons.wikimedia.org/w/api.php", params=params, headers=HEADERS,
                          stream=True, timeout=(5, 20), allow_redirects=False) as response:
            if response.status_code != 200:
                logger.warning("Commons search unavailable (HTTP %s)", response.status_code)
                return []
            body = bytearray()
            started = time.monotonic()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES or time.monotonic() - started > 30:
                    raise ValueError("Commons search response exceeded limits")
            pages = json.loads(body).get("query", {}).get("pages", {}).values()
        candidates = []
        for page in sorted(pages, key=lambda p: p.get("index", 0)):
            title = plain_text(page.get("title", "").removeprefix("File:"))
            if Path(title).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            if re.search(r"\b(wikidata|wikipedia|screenshot|logo|wordmark)\b", title, re.I):
                continue
            info = page.get("imageinfo", [{}])[0]
            meta = info.get("extmetadata", {})
            def field(name):
                return plain_text(meta.get(name, {}).get("value", ""))
            license_code, license_name = field("License").lower(), field("LicenseShortName")
            # Keep the automated fallback to PD/CC0 and attribution-only licenses.
            if not (license_code in {"pd", "cc-zero", "cc0"} or re.fullmatch(r"cc-by-(?:2\.0|2\.5|3\.0|4\.0)", license_code)):
                continue
            author = field("Artist")
            if license_code.startswith("cc-by-") and not author:
                continue
            description = field("ImageDescription")
            title_match = choose_relevant_photo(query, [{"title": title}]) is not None
            # Fragment/detail photos may omit the object's full name in their
            # filename. Require a title keyword AND its exact name in the caption.
            detail_match = bool(_terms(query) & _terms(title)) and query.casefold() in description.casefold()
            if not (title_match or detail_match):
                continue
            url = info.get("thumburl") or info.get("url", "")
            if not trusted_asset(url):
                continue
            license_url = field("LicenseUrl")
            if license_url.startswith("//"):
                license_url = "https:" + license_url
            if urlsplit(license_url).scheme not in {"http", "https"}:
                license_url = ""
            candidates.append({
                "provider": "commons", "photo_id": page.get("pageid"),
                "photo_url": "https://commons.wikimedia.org/wiki/" + quote(page["title"].replace(" ", "_"), safe=""),
                "asset_url": url, "photographer": author or "Wikimedia Commons contributor",
                "alt": title, "description": description, "license": license_name,
                "license_url": license_url, "modifications": "Cropped/resized for the video; motion may be applied.",
                "usage": "reconstruction" if re.search(r"\b(model|replica|reconstruction)\b", title, re.I) else "archive_image",
                "title_match": title_match,
            })
        candidates.sort(key=lambda p: (not p["title_match"], bool(re.search(r"fragment\s+\d", p["alt"], re.I))))
        return candidates[:6]
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, IndexError):
        logger.warning("Commons image search unavailable; no archive images accepted")
        return []


def download_commons_image(candidate, out_path):
    from pipeline.fetch_images import MAX_IMAGE_BYTES, _image_signature
    url = candidate.get("asset_url", "")
    if not trusted_asset(url):
        return False
    path = Path(out_path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, headers=HEADERS, stream=True, timeout=(5, 20), allow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get("Content-Type", "").split(";")[0].lower() not in {"image/jpeg", "image/png", "image/webp"}:
                return False
            expected = response.headers.get("Content-Length")
            if expected and int(expected) > MAX_IMAGE_BYTES:
                return False
            size, prefix, started = 0, bytearray(), time.monotonic()
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".commons-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES or time.monotonic() - started > 60:
                        raise ValueError("Image exceeded download limits")
                    prefix.extend(chunk[:max(0, 16 - len(prefix))])
                    output.write(chunk)
            if not _image_signature(prefix) or (expected and size != int(expected)):
                raise ValueError("Incomplete image")
            temporary.replace(path)
            temporary = None
        return True
    except (requests.RequestException, OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
