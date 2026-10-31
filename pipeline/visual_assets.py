"""Relevant visual selection, NASA assets, and reusable local storyboards."""

import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import quote, urlsplit

import requests

from pipeline.gemini_client import GeminiError, generate_json
from pipeline.run_store import file_hash, read_json

logger = logging.getLogger(__name__)


def is_space_topic(text):
    return bool(re.search(r"\b(nasa|voyager|astronomy|interstellar|heliopause|heliosphere|galaxies|galaxy|spacecraft|solar system|black holes?|supernova|nebula|mars|jupiter|saturn|neptune|hubble|webb telescope)\b", text, re.I))


def space_subject_query(text):
    """A concrete subject for NASA fallback shots when an abstract beat has no image."""
    match = re.search(r"\b(voyager|hubble|james webb|cassini|juno|perseverance|curiosity rover|"
                      r"black hole|supernova|nebula|mars|jupiter|saturn|neptune|mercury|venus|"
                      r"milky way|galaxy|heliosphere|solar wind)\b", text, re.I)
    return match.group(0) if match else None


def scientific_subject_query(text):
    """Extract only explicitly introduced binomials, not arbitrary word pairs."""
    match = re.search(r"\b(?i:species|scientifically known as)\s+(?i:known as\s+)?([A-Z][a-z]{2,}\s+[a-z]{2,})\b", text)
    return match.group(1) if match else None


def _terms(text):
    ignored = {"image", "photo", "photograph", "photography", "concept", "illustration", "space", "the", "and", "of", "in", "a",
               "vast", "empty", "dramatic", "beautiful", "amazing", "abstract", "dark", "bright", "colorful", "deep"}
    return {word.rstrip("s") for word in re.findall(r"[a-z0-9]+", str(text).lower()) if len(word) > 2 and word not in ignored}


def choose_relevant_photo(query, photos):
    """Require descriptive subject overlap; provider ranking alone is insufficient."""
    wanted = _terms(query)
    ranked = []
    for photo in photos:
        description = photo.get("alt") or photo.get("title") or ""
        matches = wanted & _terms(description)
        # A match only on 'solar' must not turn a heliopause into a garden lamp.
        score = len(matches) / max(1, len(wanted))
        if len(matches) >= min(2, len(wanted)) and score >= 0.5:
            ranked.append((score, photo))
    return max(ranked, key=lambda item: item[0])[1] if ranked else None


def subject_from_title(title, sentences):
    """Keep an explicit object name available when storyboard generation fails."""
    if ":" in title:
        prefix = re.sub(r"^(?:the|a|an)\s+", "", title.split(":", 1)[0].strip(), flags=re.I)
        if 1 <= len(prefix.split()) <= 5:
            return prefix
    text = " ".join(sentences)
    # Archive filenames often use a species' scientific name instead of a
    # broad common name such as "deep sea octopus". Only extract an explicit
    # binomial after a naming cue, avoiding arbitrary pairs from the narration.
    scientific = scientific_subject_query(text)
    if scientific:
        return scientific
    match = re.search(r"(?i:known as|called|named)\s+(?i:the\s+)?([A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,3})", text)
    return match.group(1) if match else ""


def plan_scenes(sentences, category, *, subject_hint=""):
    from pipeline.fetch_images import _fallback_query
    subject = subject_from_title(subject_hint, sentences)
    fallback = [{"query": subject or _fallback_query(sentence, category), "headline": "", "subject_query": subject} for sentence in sentences]
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return fallback
    schema = {"type": "array", "minItems": len(sentences), "maxItems": len(sentences), "items": {
        "type": "object", "properties": {"query": {"type": "string"}, "headline": {"type": "string"}, "subject_query": {"type": "string"}},
        "required": ["query", "headline", "subject_query"], "additionalProperties": False}}
    try:
        plans = generate_json(
            "Create a visual storyboard for a factual short. For each passage return a 2-3 word image query "
            "and a concise 2-5 word on-screen headline (maximum 42 characters). Keep the specific main "
            "subject in the query, e.g. 'Voyager spacecraft' or 'Voyager heliosphere'. Use things the viewer "
            "can actually see, not abstract metaphors, generic people, decorative lights, or unrelated equipment. "
            "Use the full set of passages to identify the main subject. For 'no finish line in space', "
            "search for the named spacecraft, not 'empty space' or 'finish line'. "
            "Also include subject_query: the main concrete subject's exact common name, repeated for every passage, "
            "such as 'Antikythera mechanism', 'octopus', or 'Voyager spacecraft'. This is an archive lookup, not a poetic title. "
            "For a specific species, use its scientific name from the narration when available. "
            "Headlines must summarize claims already in the script; the first should express the opening "
            "curiosity without exaggeration. No new numbers or claims. Return exactly one object per passage. "
            f"Topic: {category}\nStory title: {subject_hint}\nPassages: {json.dumps(sentences)}", schema=schema)
        if not isinstance(plans, list) or len(plans) != len(sentences):
            logger.warning("Storyboard count did not match the narration; using subject-based searches")
            return fallback
        return [{"query": " ".join(p["query"].split())[:100], "headline": " ".join(p["headline"].split())[:42],
                 "subject_query": subject or (" ".join(p.get("subject_query", "").split())[:100] if isinstance(p.get("subject_query", ""), str) else "")}
                if isinstance(p, dict) and isinstance(p.get("query"), str) and p["query"].strip()
                and isinstance(p.get("headline"), str) else fallback[i] for i, p in enumerate(plans)]
    except (GeminiError, ValueError, TypeError) as exc:
        logger.warning("Storyboard generation unavailable (%s); using subject-based searches", exc)
        return fallback


def _nasa_asset(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "images-assets.nasa.gov"
                and not p.username and not p.password and p.port in {None, 443}
                and p.path.startswith("/image/") and p.path.lower().endswith((".jpg", ".jpeg", ".png")))
    except (TypeError, ValueError):
        return False


def download_nasa_image(query, out_path, *, metadata=None, nasa_id=None):
    """Use the official NASA image API; no credentials are sent to asset hosts."""
    from pipeline.fetch_images import _image_signature, MAX_IMAGE_BYTES
    temporary = None
    try:
        params = {"media_type": "image", "page_size": 12}
        params["nasa_id" if nasa_id else "q"] = nasa_id or query
        with requests.get("https://images-api.nasa.gov/search", params=params,
                          timeout=(5, 20), allow_redirects=False) as response:
            if response.status_code != 200:
                return False
            items = response.json().get("collection", {}).get("items", [])
        candidates = []
        for item in items:
            data = item.get("data", [{}])[0]
            links = [link for link in item.get("links", []) if _nasa_asset(link.get("href", ""))]
            if not links:
                continue
            link = max(links, key=lambda l: ("~large." in l["href"], "~orig." in l["href"], "~medium." in l["href"]))
            # Long archive descriptions mention many unrelated missions. The
            # title must identify the pictured subject; otherwise use the
            # explicit subject fallback instead of accepting incidental words.
            candidates.append({"title": data.get("title", ""), "data": data, "asset": link["href"]})
        chosen = candidates[0] if nasa_id and candidates else choose_relevant_photo(query, candidates)
        if chosen is None:
            return False
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(chosen["asset"], stream=True, timeout=(5, 20), allow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get("Content-Type", "").split(";")[0] not in {"image/jpeg", "image/png"}:
                return False
            size, prefix, started = 0, bytearray(), time.monotonic()
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".nasa-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES or time.monotonic() - started > 60:
                        raise ValueError("Image download limit exceeded")
                    if len(prefix) < 16:
                        prefix.extend(chunk[:16 - len(prefix)])
                    output.write(chunk)
            expected = response.headers.get("Content-Length")
            if not _image_signature(prefix) or (expected and size != int(expected)):
                raise ValueError("Incomplete image")
            temporary.replace(path)
            temporary = None
        data = chosen["data"]
        if metadata is not None:
            description = data.get("description", "")
            metadata.update(provider="nasa", photo_id=data.get("nasa_id"),
                            photo_url="https://images.nasa.gov/details/" + quote(data.get("nasa_id", ""), safe=""),
                            asset_url=chosen["asset"], photographer=data.get("photographer") or "NASA/JPL-Caltech",
                            alt=data.get("title", ""), description=description,
                            usage="artist_concept" if re.search(r"artist|illustration|concept", description + data.get("title", ""), re.I) else "agency_image")
        return True
    except (requests.RequestException, OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def visual_plan_option(path):
    """Snapshot local storyboard inputs so changed source files cannot be silently reused."""
    path = Path(path).expanduser().resolve()
    data = read_json(path)
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 24:
        raise ValueError("A visual plan needs 1-24 scenes")
    assets = []
    previous_end = 0
    for scene in scenes:
        try:
            start, end = float(scene["start"]), float(scene["end"])
            source = (path.parent / scene["image_path"]).resolve()
            headline = scene.get("headline", "")
            if (not math.isfinite(start) or not math.isfinite(end) or start < previous_end or end <= start
                    or not isinstance(headline, str) or len(headline) > 80
                    or scene.get("fit", "contain") not in {"cover", "contain"}
                    or scene.get("layout", "split") not in {"split", "full"}
                    or not isinstance(scene.get("provenance", {}), dict)
                    or source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"}
                    or not source.is_file() or not source.stat().st_size):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid visual scene: use ordered, non-overlapping times, a local image or video, and a short headline") from None
        previous_end = end
        assets.append({"path": str(source), "sha256": file_hash(source)})
    return {"path": str(path), "sha256": file_hash(path), "assets": assets}


def import_visual_plan(option, images_dir, duration):
    if visual_plan_option(option["path"]) != option:
        raise ValueError("Visual plan or source images changed; start a new run with the updated --visual-plan")
    scenes = read_json(Path(option["path"]))["scenes"]
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    segments = []
    for index, (scene, asset) in enumerate(zip(scenes, option["assets"])):
        start, end = float(scene["start"]), float(scene["end"])
        if start >= duration:
            continue
        destination = images_dir / f"scene_{index:02d}{Path(asset['path']).suffix.lower()}"
        shutil.copyfile(asset["path"], destination)
        segments.append({"image_path": str(destination), "start": start, "end": min(end, duration),
                         **({"media_type": "video"} if destination.suffix in {".mp4", ".mov", ".webm"} else {}),
                         "headline": scene.get("headline", ""), "fit": scene.get("fit", "contain"),
                         "layout": scene.get("layout", "full" if index in {0, len(scenes)-1} else "split"),
                         "provenance": scene.get("provenance", {"provider": "local", "usage": "user_supplied"})})
    if not segments:
        raise ValueError("No visual scenes overlap the narration")
    return segments
