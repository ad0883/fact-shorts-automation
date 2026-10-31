"""Timed visuals from stock photography and subject-specific public archives."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import requests

from pipeline.gemini_client import GeminiError, generate_json

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 12
_STOPWORDS = set("a an the is are was were be been being this that these those it its and or but of to in on for from with at as by your you we our their there here what how when why can could will would than then they them has have had not just into about".split())


def _tokens(text: str) -> list[str]:
    return [word.replace("'", "").replace("’", "").casefold()
            for word in re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text, flags=re.UNICODE)]


def split_into_sentences(text: str) -> list[str]:
    """Keep short sentences and avoid splitting decimal numbers or common titles."""
    protected = re.sub(r"\b(Mr|Mrs|Ms|Dr|Prof|St)\.(?=\s)", lambda match: match[1] + "\u2024", text.strip())
    return [part.replace("\u2024", ".").strip()
            for part in re.split(r"(?<=[.!?])\s+", protected) if part.strip()]


def map_sentences_to_timestamps(sentences: list[str], words: list[dict]) -> list[dict]:
    """Globally align token sequences instead of repeatedly matching common prefixes.

    Exact matches anchor sentence boundaries. Missing words are interpolated between
    anchors; wholly unmatched text uses proportional timing and is marked estimated.
    Every boundary is monotonic, so repeated words and ASR omissions cannot rewind.
    """
    timeline = []
    previous_start = 0.0
    for word in words:
        if not isinstance(word, dict):
            continue
        try:
            start, end = float(word["start"]), float(word["end"])
            tokens = _tokens(str(word["word"]))
        except (KeyError, ValueError, TypeError):
            continue
        if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start or start < previous_start:
            continue
        previous_start = start
        for index, token in enumerate(tokens):
            duration = (end - start) / len(tokens)
            timeline.append({"token": token, "start": start + index * duration, "end": start + (index + 1) * duration})
    sentence_tokens = [_tokens(sentence) for sentence in sentences]
    script_tokens = [token for tokens in sentence_tokens for token in tokens]
    if not timeline or not script_tokens or len(timeline) > 10000 or len(script_tokens) > 10000:
        return []
    matcher = SequenceMatcher(None, script_tokens, [entry["token"] for entry in timeline], autojunk=False)
    matches = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matches[block.a + offset] = block.b + offset
    # Endpoints anchor interpolation even when the recognizer misses entire clauses.
    anchors = [(-1, -1), *sorted(matches.items()), (len(script_tokens), len(timeline))]
    boundaries = [0]
    offset = 0
    for tokens in sentence_tokens[:-1]:
        offset += len(tokens)
        if offset in matches:
            boundary = matches[offset]
        else:
            right_index = next(index for index, pair in enumerate(anchors) if pair[0] >= offset)
            left, right = anchors[right_index - 1], anchors[right_index]
            fraction = (offset - left[0]) / (right[0] - left[0])
            boundary = round(left[1] + fraction * (right[1] - left[1]))
        boundaries.append(min(len(timeline), max(boundaries[-1], boundary)))
    boundaries.append(len(timeline))
    segments = []
    offset = 0
    for index, sentence in enumerate(sentences):
        first, stop = boundaries[index:index + 2]
        tokens = sentence_tokens[index]
        matched = sum(position in matches for position in range(offset, offset + len(tokens)))
        offset += len(tokens)
        if first >= stop:
            continue
        start = timeline[first]["start"]
        end = timeline[stop - 1]["end"]
        if stop < len(timeline):
            end = min(end, timeline[stop]["start"])
        if end > start:
            segments.append({"sentence": sentence, "start": start, "end": end,
                             "alignment": "matched" if matched == len(tokens) else "estimated"})
    return segments


def _fallback_query(sentence: str, category: str) -> str:
    tokens = [word for word in _tokens(sentence) if word not in _STOPWORDS and len(word) > 2]
    if not tokens:
        tokens = [word for word in _tokens(category) if word not in _STOPWORDS]
    return " ".join(list(dict.fromkeys(tokens))[:4])[:100] or "nature"


def generate_search_queries(sentences: list[str], fact_category: str) -> list[str]:
    """Always return one valid query per sentence, including when Gemini is unavailable."""
    fallback = [_fallback_query(sentence, fact_category) for sentence in sentences]
    if not sentences or not os.environ.get("GEMINI_API_KEY", "").strip():
        return fallback
    prompt = (
        "Find illustrative stock photos for an educational video. Return one 2-4 word "
        "concrete visual search query per sentence. Avoid quotes, dates, abstract ideas, "
        "and implying that stock imagery depicts the actual historical event. "
        f"Topic: {fact_category}\nSentences: {json.dumps(sentences)}"
    )
    try:
        queries = generate_json(prompt, schema={"type": "array", "items": {"type": "string"},
                                               "minItems": len(sentences), "maxItems": len(sentences)})
        if not isinstance(queries, list) or len(queries) != len(sentences):
            raise ValueError("Wrong number of image queries")
        return [" ".join(query.split()) if isinstance(query, str) and 1 <= len(query.strip()) <= 100
                else fallback[index] for index, query in enumerate(queries)]
    except (GeminiError, ValueError, TypeError):
        logger.warning("Image query generation unavailable; using local keyword queries")
        return fallback


def _trusted_image_url(url: object) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "images.pexels.com"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and parsed.path.startswith("/photos/") and not parsed.fragment)
    except ValueError:
        return False


def _image_signature(prefix: bytes) -> bool:
    return (prefix.startswith(b"\xff\xd8\xff") or prefix.startswith(b"\x89PNG\r\n\x1a\n")
            or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"))


def download_pexels_image(query: str, out_path: Path, orientation: str = "portrait", *, metadata: dict | None = None) -> bool:
    """Download a bounded image from a Pexels API asset URL; optionally return credits.

    API authentication is only sent to api.pexels.com. Downloads do not follow
    redirects and files are atomically replaced only after a complete valid response.
    """
    api_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not api_key:
        return False
    temporary_path = None
    try:
        with requests.get(
            "https://api.pexels.com/v1/search", headers={"Authorization": api_key},
            params={"query": query, "size": "medium", "per_page": 15,
                    **({"orientation": orientation} if orientation else {})},
            timeout=(5, 15), allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                logger.warning("Pexels search unavailable (HTTP %s); image skipped", response.status_code)
                return False
            data = response.json()
        photos = data.get("photos", []) if isinstance(data, dict) else []
        if not isinstance(photos, list):
            return False
        from pipeline.visual_assets import choose_relevant_photo
        candidates = [item for item in photos if isinstance(item, dict)
                      and isinstance(item.get("src"), dict)
                      and _trusted_image_url(item["src"].get("large"))]
        photo = choose_relevant_photo(query, candidates)
        if photo is None:
            logger.info("No sufficiently relevant photo for %r; skipped", query)
            return False
        asset_url = photo["src"]["large"]
        with requests.get(asset_url, timeout=(5, 20), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                return False
            mime_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                return False
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                return False
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            prefix = bytearray()
            started = time.monotonic()
            with tempfile.NamedTemporaryFile(dir=out_path.parent, prefix=f".{out_path.name}.", suffix=".part", delete=False) as output:
                temporary_path = Path(output.name)
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES or time.monotonic() - started > 60:
                        raise ValueError("Image exceeded download limits")
                    if len(prefix) < 16:
                        prefix.extend(chunk[:16 - len(prefix)])
                    output.write(chunk)
            if not size or not _image_signature(bytes(prefix)):
                raise ValueError("Response was not a supported image")
            if length and size != int(length):
                raise ValueError("Incomplete image download")
            temporary_path.replace(out_path)
            temporary_path = None
        if metadata is not None:
            metadata.update({
                "provider": "pexels", "photo_id": photo.get("id"),
                "photo_url": photo.get("url", ""), "asset_url": asset_url,
                "photographer": photo.get("photographer", ""),
                "photographer_url": photo.get("photographer_url", ""),
                "alt": photo.get("alt", ""), "usage": "illustrative_stock_image",
            })
        return True
    except (requests.RequestException, OSError, ValueError, TypeError):
        logger.warning("Pexels image unavailable; continuing without this image")
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove incomplete image: %s", temporary_path.name)


def fetch_images_for_fact(fact_text: str, fact_category: str, words: list[dict], images_dir: Path, *, report: dict | None = None, subject_hint: str = "", storyboard: list[dict] | None = None) -> list[dict]:
    """Return timed image cards with provenance; optional network failures yield fewer cards."""
    from pipeline.visual_assets import is_space_topic, space_subject_query, scientific_subject_query, plan_scenes, download_nasa_image
    from pipeline.commons_images import search_commons_images, download_commons_image
    space_topic = is_space_topic(fact_category + " " + fact_text)
    subject_query = space_subject_query(fact_text) if space_topic else None
    if storyboard is not None:
        segments = [{**beat, "sentence": beat["narration"]} for beat in storyboard if beat["visual"]["kind"] == "photo"]
        plans = [{"query": b["visual"]["query"], "headline": b["visual"]["headline"], "subject_query": ""} for b in segments]
        if any(len(p["query"].split()) > 4 for p in plans):
            revised = plan_scenes([b["sentence"] for b in segments], fact_category, subject_hint=subject_hint)
            plans = [{**p, "query": r["query"], "subject_query": r.get("subject_query", "")}
                     if len(p["query"].split()) > 4 else p for p,r in zip(plans,revised)]
    else:
        sentences = split_into_sentences(fact_text)
        if len(sentences) > MAX_IMAGES:
            group_size = math.ceil(len(sentences) / MAX_IMAGES)
            sentences = [" ".join(sentences[index:index + group_size]) for index in range(0, len(sentences), group_size)]
        segments = map_sentences_to_timestamps(sentences, words)
        plans = plan_scenes([segment["sentence"] for segment in segments], fact_category, subject_hint=subject_hint) if segments else []
    if not segments:
        return []
    if report is not None:
        report.update(requested_scenes=len(segments), plans=plans, archive_searches=[])
    species_subject = scientific_subject_query(fact_text)
    archive_subject = species_subject or next((p["subject_query"] for p in plans if p.get("subject_query")), "")
    archive_first = bool(species_subject or re.search(r"archaeolog|ancient|histor|museum|artifact", fact_category, re.I))
    image_segments = []
    cache = {}
    archive_cache = {}
    video_offsets = {}
    video_variants = {}
    variant_attempts = set()

    def archive_image(query, index):
        if query not in archive_cache:
            loaded = []
            candidates = search_commons_images(query)
            attempted_assets, loaded_ids = set(), set()

            def accept(candidate):
                asset = candidate.get("asset_url")
                identity = candidate.get("photo_id", candidate.get("alt"))
                if asset and asset in attempted_assets or identity in loaded_ids:
                    return
                if asset:
                    attempted_assets.add(asset)
                path = Path(images_dir) / f"archive_{len(archive_cache):02d}_{len(loaded):02d}.jpg"
                if download_commons_image(candidate, path):
                    loaded.append((str(path), candidate))
                    loaded_ids.add(identity)

            for candidate in candidates:
                accept(candidate)
            # Some Commons results link to full originals instead of previews.
            # Try one smaller preview batch if too few of those downloads work,
            # without repeating failed URLs or downloading successful photos twice.
            target = min(3, len(candidates))
            thumbnail_fallback = len(loaded) < target
            if thumbnail_fallback:
                for candidate in search_commons_images(query, thumbnail_width=640):
                    accept(candidate)
                    if len(loaded) >= target:
                        break
            archive_cache[query] = loaded
            if report is not None:
                entry = {"query": query, "candidates": len(candidates), "downloaded": len(loaded)}
                if thumbnail_fallback:
                    entry["smaller_thumbnails_requested"] = True
                report["archive_searches"].append(entry)
        pool = archive_cache[query]
        return pool[index % len(pool)] if pool else None

    for index, (segment, plan) in enumerate(zip(segments, plans)):
        query = plan["query"]
        key = query.casefold()
        cached = archive_image(archive_subject or query, index) if archive_first and not space_topic else None
        if cached is None and key not in cache:
            path = Path(images_dir) / f"img_{index:02d}.jpg"
            provenance = {}
            found = False
            if storyboard is not None and not space_topic and not species_subject and not archive_first:
                from pipeline.stock_video import download_pexels_video
                video_path = path.with_suffix(".mp4")
                found = download_pexels_video(query, video_path, metadata=provenance, context=segment["sentence"])
                if found:
                    path = video_path
            if not found and space_topic:
                found = download_nasa_image(query, path, metadata=provenance)
            if not found and not space_topic and not species_subject:
                # Portrait and square photos also fit the renderer. Restricting
                # searches to landscape needlessly excludes relevant subjects.
                found = download_pexels_image(query, path, orientation="", metadata=provenance)
            cache[key] = (str(path), provenance) if found else None
        actual_query = (archive_subject or query) if cached else query
        cached = cached or cache.get(key)
        if cached and Path(cached[0]).suffix.lower() == ".mp4":
            pool = video_variants.setdefault(key, [])
            if not pool:
                pool.append(cached)
            elif key not in variant_attempts:
                # Two camera angles per repeated search; bounded requests and
                # downloads, with the existing clip retained if no alternative fits.
                variant_attempts.add(key)
                from pipeline.stock_video import download_pexels_video
                alternate = Path(images_dir) / f"img_{index:02d}_alternate.mp4"
                details = {}
                if download_pexels_video(query, alternate, metadata=details,
                                         exclude_ids=[p[1].get("photo_id") for p in pool], context=segment["sentence"]):
                    pool.append((str(alternate),details))
            from pipeline.stock_video import context_matches
            compatible = [p for p in pool if context_matches(segment["sentence"],p[1].get("alt", ""))]
            cached = compatible[index % len(compatible)] if compatible else None
        if cached is None and subject_query:
            fallback_key = "nasa-subject:" + subject_query.casefold()
            if fallback_key not in cache:
                path = Path(images_dir) / "subject.jpg"
                provenance = {}
                found = download_nasa_image(subject_query, path, metadata=provenance)
                cache[fallback_key] = (str(path), provenance) if found else None
            cached = cache[fallback_key]
            actual_query = subject_query
        if cached is None and not space_topic:
            actual_query = archive_subject or query
            cached = archive_image(actual_query, index)
            # A cached miss for the shared subject must not suppress more
            # specific scene searches (for example a species' scientific name).
            if cached is None and not species_subject and query.casefold() != actual_query.casefold():
                actual_query = query
                cached = archive_image(query, index)
        if cached is not None:
            path, provenance = cached
            is_video = Path(path).suffix.lower() == ".mp4"
            offset = video_offsets.get(path, 0)
            if is_video:
                clip_duration = provenance.get("duration_seconds", 0)
                if clip_duration:
                    offset %= clip_duration
                video_offsets[path] = offset + segment["end"] - segment["start"]
            image_segments.append({
                "image_path": path, "start": segment["start"], "end": segment["end"],
                "query": actual_query, "requested_query": query,
                "alignment": segment["alignment"], "provenance": provenance,
                "headline": plan["headline"],
                **({"media_type": "video", "source_offset": offset} if is_video else {}),
                **({"beat_index": segment["beat_index"]} if "beat_index" in segment else {}),
                "fit": "contain" if re.search(r"\b(graph|graphic|chart|diagram|plot|map)\b", provenance.get("description", "") + " " + provenance.get("alt", ""), re.I) else "cover",
            })
    # Give uncovered passages one bounded batch of alternative concrete searches.
    # Species retain exact archive matching; NASA keeps its existing subject fallback.
    covered = {(s["start"], s["end"]) for s in image_segments}
    missing = [(i, s, p) for i, (s, p) in enumerate(zip(segments, plans))
               if (s["start"], s["end"]) not in covered]
    if missing and not space_topic and not species_subject:
        alternatives = plan_scenes(
            [s["sentence"] for _, s, _ in missing], fact_category,
            subject_hint=subject_hint + "\nFind simpler concrete visual searches. Avoid these unsuccessful queries: "
            + ", ".join(p["query"] for _, _, p in missing))
        for (index, segment, original), alternative in zip(missing, alternatives):
            query = alternative["query"]
            key = query.casefold()
            cached = cache.get(key)
            if key not in cache:
                path = Path(images_dir) / f"img_{index:02d}_retry.jpg"
                provenance = {}
                found = download_pexels_image(query, path, orientation="", metadata=provenance) if not archive_first else False
                cache[key] = (str(path), provenance) if found else None
                cached = cache[key]
            if cached is None:
                cached = archive_image(query, index)
            if cached is None:
                continue
            path, provenance = cached
            if Path(path).suffix.lower() == ".mp4":
                from pipeline.stock_video import context_matches
                if not context_matches(segment["sentence"], provenance.get("alt", "")):
                    continue
            image_segments.append({
                "image_path": path, "start": segment["start"], "end": segment["end"],
                "query": query, "requested_query": original["query"],
                "alignment": segment["alignment"], "provenance": provenance,
                "headline": "", "fit": "contain",
                **({"media_type": "video"} if Path(path).suffix.lower() == ".mp4" else {}),
                **({"beat_index": segment["beat_index"]} if "beat_index" in segment else {}),
            })
        if report is not None:
            report["alternative_searches"] = len(alternatives)
    image_segments.sort(key=lambda scene: scene["start"])
    # Keep a scene through short spoken pauses instead of flashing gameplay
    # between every sentence. Missing-image passages still retain gameplay.
    if storyboard is None and image_segments and image_segments[0]["start"] < 1:
        image_segments[0]["start"] = 0.0
    for current, following in zip(image_segments, image_segments[1:]):
        if storyboard is None and 0 <= following["start"] - current["end"] <= 1.0:
            current["end"] = following["start"]
    if report is not None:
        report.update(selected_scenes=len(image_segments), unique_images=len({s["image_path"] for s in image_segments}))
    return image_segments
