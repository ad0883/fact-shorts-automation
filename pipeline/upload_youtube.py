"""Explicit YouTube uploads with resumable retries and local duplicate protection.

Shorts classification is determined by YouTube from the video itself. A hashtag
or a Shorts-formatted URL does not guarantee classification.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import (
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_CLIENT_SECRET_FILE,
    YOUTUBE_PRIVACY_STATUS,
    YOUTUBE_TOKEN_FILE,
    YOUTUBE_LANGUAGE,
)

# Upload videos and verify the selected channel. No caption-edit permission needed.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def _google_libraries():
    """Keep local generation and CLI help independent of OAuth dependencies."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        from httplib2 import HttpLib2Error
    except ImportError as exc:
        raise RuntimeError(
            "YouTube upload dependencies are missing. Install google-api-python-client, "
            "google-auth-oauthlib, and google-auth-httplib2."
        ) from exc
    return SimpleNamespace(
        RefreshError=RefreshError, Request=Request, Credentials=Credentials,
        InstalledAppFlow=InstalledAppFlow, build=build, HttpError=HttpError,
        MediaFileUpload=MediaFileUpload, HttpLib2Error=HttpLib2Error,
    )


def _atomic_write(path: Path, text: str) -> None:
    """Replace a complete file atomically, with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def get_authenticated_service(
    client_secret_file: Path | str = YOUTUBE_CLIENT_SECRET_FILE,
    token_file: Path | str = YOUTUBE_TOKEN_FILE,
    *, allow_interactive: bool = True,
):
    """Load or refresh credentials; start browser OAuth only when necessary."""
    libraries = _google_libraries()
    client_secret_path, token_path = Path(client_secret_file), Path(token_file)
    creds = None
    if token_path.exists():
        try:
            saved = json.loads(token_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("scopes", []), list):
                raise ValueError("Invalid OAuth token format")
            # Passing requested scopes to Credentials would disguise an old,
            # upload-only grant. Inspect the saved grant before loading it.
            saved_scopes = set(saved.get("scopes", []))
            compatible = set(SCOPES).issubset(saved_scopes) or bool(saved_scopes & {
                "https://www.googleapis.com/auth/youtube", "https://www.googleapis.com/auth/youtube.force-ssl"})
            if compatible:
                creds = libraries.Credentials.from_authorized_user_file(str(token_path), saved["scopes"])
            else:
                print("YouTube needs upload and channel-read permission; reconnecting your channel.")
        except (ValueError, KeyError):
            print(f"Warning: Invalid OAuth credentials in {token_path}; authentication is required.", file=sys.stderr)
        if os.name != "nt":
            token_path.chmod(0o600)

    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            try:
                print("Refreshing YouTube OAuth token...")
                creds.refresh(libraries.Request())
            except libraries.RefreshError as exc:
                # Revoked/invalid grants require consent; network failures should
                # propagate instead of needlessly launching a browser login.
                if getattr(exc, "retryable", False):
                    raise
                creds = None
        if not creds or not creds.valid:
            if not allow_interactive:
                raise RuntimeError("YouTube needs channel authorization. Run: venv/bin/python main.py --connect-youtube")
            if not client_secret_path.is_file():
                raise FileNotFoundError(
                    f"YouTube OAuth client file not found: {client_secret_path}. "
                    "Create a Desktop app OAuth client with YouTube Data API v3 enabled, "
                    "then save its JSON credentials here."
                )
            print("Opening browser authentication for your YouTube channel...")
            flow = libraries.InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        if not creds or not creds.valid:
            raise RuntimeError("YouTube authentication did not return valid credentials.")
        _atomic_write(token_path, creds.to_json())
    return libraries.build("youtube", "v3", credentials=creds, cache_discovery=False)


def _metadata_text(metadata: dict, key: str, default: str = "") -> str:
    value = metadata.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"Metadata {key!r} must be a string.")
    # YouTube does not allow angle brackets in titles or descriptions.
    return value.replace("<", "").replace(">", "").strip()


_PROMOTIONAL_TAGS = {"viral", "fyp", "foryou", "foryoupage", "trending",
                     "goviral", "viralvideo", "viralshorts", "subscribe", "likeandshare"}
_GENERIC_TAGS = {"shorts", "facts", "funfacts", "amazingfacts", "didyouknow",
                 "education", "educational", "knowledge", "youtube", "youtubeshorts"}


def _tag_key(tag: str) -> str:
    return "".join(char for char in tag.casefold() if char.isalnum())


def _topic_hashtags(tags: list[str], metadata: dict) -> list[str]:
    """Prefer tags grounded in the title/narration, keeping supplied niches as fallback.

    This is a deterministic relevance heuristic, not a popularity estimate.
    """
    def words(text):
        return " ".join(re.findall(r"[^\W_]+", text.casefold()))

    title = f" {words(_metadata_text(metadata, 'title'))} "
    narration = f" {words(_metadata_text(metadata, 'fact_text'))} "
    candidates, seen = [], set()
    for tag in tags:
        clean = "".join(char for char in tag if char.isalnum() or char == "_")
        key = _tag_key(clean)
        if (not 2 <= len(clean) <= 35 or key in seen
                or key in _PROMOTIONAL_TAGS | _GENERIC_TAGS):
            continue
        seen.add(key)
        phrase = words(tag)
        # Exact phrases avoid accidental matches such as "art" in "Earth".
        score = (4 if f" {phrase} " in title else 0) + (2 if f" {phrase} " in narration else 0)
        candidates.append((score, clean))
    # Stable sorting retains the author's order for equally relevant tags.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [tag for _, tag in candidates[:3]]


def prepare_metadata(fact_metadata: dict, privacy_status: str | None = None, *, image_segments=None) -> dict:
    """Produce metadata within YouTube's title, UTF-8 description and tag limits.

    Limits: https://developers.google.com/youtube/v3/docs/videos#snippet
    """
    if not isinstance(fact_metadata, dict):
        raise ValueError("Fact metadata must be a JSON object.")
    privacy = YOUTUBE_PRIVACY_STATUS if privacy_status is None else privacy_status
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("YouTube privacy must be private, unlisted, or public.")
    raw_title = _metadata_text(fact_metadata, "title", "Fascinating Fact") or "Fascinating Fact"
    title = raw_title[:100].rstrip()
    raw_tags = fact_metadata.get("tags", [])
    if not isinstance(raw_tags, list) or any(not isinstance(tag, str) for tag in raw_tags):
        raise ValueError("Metadata 'tags' must be a list of strings.")
    clean_tags, seen, tag_length = [], set(), 0
    for tag in raw_tags + ["Shorts", "Facts"]:
        clean = " ".join(tag.strip().lstrip("#").replace("<", "").replace(">", "").split())
        if not clean or clean.casefold() in seen or _tag_key(clean) in _PROMOTIONAL_TAGS:
            continue
        cost = len(clean) + (2 if " " in clean else 0) + (1 if clean_tags else 0)
        if tag_length + cost > 500:
            continue
        clean_tags.append(clean)
        seen.add(clean.casefold())
        tag_length += cost
    hashtags = _topic_hashtags(clean_tags, fact_metadata)
    hashtag_text = " ".join("#" + tag for tag in [*hashtags, "Shorts"])
    from pipeline.youtube_package import attribution_text
    credits = attribution_text(image_segments or [])
    sources = [f"{item.get('title', 'Reference')}: {item['url']}"
               for item in fact_metadata.get("sources", [])
               if isinstance(item, dict) and isinstance(item.get("url"), str)
               and item["url"].startswith(("https://", "http://"))]
    # Reserve complete credits and references before trimming descriptive prose.
    footer = "\n\n".join(part for part in [hashtag_text,
        "Source references:\n" + "\n".join(sources) if sources else "", credits] if part)
    footer = footer.replace("<", "").replace(">", "")
    remaining = 5000 - len(footer.encode("utf-8")) - 2
    if remaining < 0:
        raise ValueError("YouTube description references and image credits exceed 5000 bytes; shorten them before upload.")
    prose = _metadata_text(fact_metadata, "description") or _metadata_text(fact_metadata, "fact_text")
    prose = prose.encode("utf-8")[:remaining].decode("utf-8", errors="ignore").rstrip()
    description = "\n\n".join(part for part in [prose, footer] if part)
    return {
        "snippet": {
            "title": title, "description": description, "tags": clean_tags,
            "categoryId": str(YOUTUBE_CATEGORY_ID),
            "defaultLanguage": YOUTUBE_LANGUAGE, "defaultAudioLanguage": YOUTUBE_LANGUAGE,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _upload_lock(path: Path):
    """Reject simultaneous upload attempts for a given receipt."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(
            f"An upload lock already exists: {lock_path}. "
            "If a previous process was interrupted, check YouTube and ensure it is no longer "
            "running before removing the lock."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _load_receipt(path: Path, digest: str, force_upload: bool):
    if not path.exists() or force_upload:
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Cannot read upload receipt {path}; inspect it before using --force-upload.") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Invalid upload receipt {path}; inspect it before using --force-upload.")
    if result.get("sha256") == digest and result.get("video_id") and result.get("status") == "uploaded":
        print(f"  Already uploaded: {result['url']}")
        return result
    raise RuntimeError(
        f"An existing upload receipt needs review: {path}. "
        "Check YouTube for an earlier upload before using --force-upload to create another video."
    )


def upload_short(
    video_path: Path | str,
    fact_metadata: dict,
    privacy_status: str | None = None,
    client_secret_file: Path | str = YOUTUBE_CLIENT_SECRET_FILE,
    token_file: Path | str = YOUTUBE_TOKEN_FILE,
    *,
    force_upload: bool = False,
    receipt_path: Path | str | None = None,
    max_retries: int = 5,
    youtube_service=None,
    image_segments=None,
) -> dict:
    """Upload a video explicitly and atomically persist its result.

    A matching successful receipt is reused without another API request. An
    interrupted upload requires review and an explicit force flag, because an
    unacknowledged final chunk can still have created a remote video.
    """
    video_file = Path(video_path)
    if not video_file.is_file():
        raise FileNotFoundError(f"Video file not found: {video_file}")
    if video_file.stat().st_size == 0:
        raise ValueError(f"Video file is empty: {video_file}")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer.")
    body = prepare_metadata(fact_metadata, privacy_status, image_segments=image_segments)
    default_receipt = video_file.parent / "youtube.json" if video_file.name == "video.mp4" else video_file.with_suffix(".youtube.json")
    receipt = Path(receipt_path) if receipt_path is not None else default_receipt
    if receipt.resolve() in {video_file.resolve(), Path(token_file).resolve(), Path(client_secret_file).resolve()}:
        raise ValueError("Upload receipt must not overwrite video or credential files.")
    digest = _fingerprint(video_file)
    with _upload_lock(receipt):
        previous = _load_receipt(receipt, digest, force_upload)
        if previous is not None:
            return previous
        libraries = _google_libraries()
        youtube = youtube_service if youtube_service is not None else get_authenticated_service(client_secret_file, token_file)
        print(f"\nUploading to YouTube: {body['snippet']['title']}")
        print(f"  Privacy: {body['status']['privacyStatus']}")
        media = libraries.MediaFileUpload(
            str(video_file), mimetype="video/mp4", chunksize=2 * 1024 * 1024, resumable=True,
        )
        try:
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            pending = {
                "status": "uploading", "sha256": digest, "video_path": str(video_file.resolve()),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_write(receipt, json.dumps(pending, indent=2) + "\n")
            response, retries, stalled = None, 0, 0
            last_progress = -1.0
            while response is None:
                try:
                    status, response = request.next_chunk(num_retries=0)
                    progress = float(status.progress()) if status else last_progress
                    if response is None:
                        stalled = stalled + 1 if progress <= last_progress else 0
                        if stalled >= 10:
                            raise RuntimeError("YouTube upload stopped making progress; inspect its receipt before retrying.")
                    if status:
                        print(f"  Upload progress: {int(progress * 100)}%", end="\r", flush=True)
                    last_progress = progress
                except (libraries.HttpError, libraries.HttpLib2Error, http.client.HTTPException, OSError) as exc:
                    if isinstance(exc, libraries.HttpError) and exc.resp.status not in (429, 500, 502, 503, 504):
                        raise
                    if retries >= max_retries:
                        raise
                    delay = min(2 ** retries, 32) + random.random()
                    retries += 1
                    print(f"\nTemporary upload failure; retry {retries}/{max_retries} in {delay:.1f}s...")
                    time.sleep(delay)
            if not isinstance(response, dict) or not response.get("id"):
                raise RuntimeError("YouTube returned no video ID. Check your channel before retrying this upload.")
            video_id = response["id"]
            result = {
                **pending, "status": "uploaded", "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "privacy": response.get("status", {}).get("privacyStatus", body["status"]["privacyStatus"]),
                "title": body["snippet"]["title"], "response": response,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                _atomic_write(receipt, json.dumps(result, indent=2) + "\n")
            except OSError as exc:
                raise RuntimeError(
                    f"Video uploaded to {result['url']}, but the receipt could not be saved. "
                    "Record this URL before attempting another upload."
                ) from exc
            print(f"\n  Upload complete: {result['url']}")
            return result
        finally:
            # MediaFileUpload holds an open file descriptor for resumable uploads.
            stream = media.stream()
            if stream is not None:
                stream.close()


def main():
    parser = argparse.ArgumentParser(description="Explicitly upload a video to YouTube.")
    parser.add_argument("--video", required=True, type=Path, help="Path to video.mp4")
    parser.add_argument("--fact", type=Path, help="Path to fact.json metadata file")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default=None)
    parser.add_argument("--receipt", type=Path, help="Receipt path (video.mp4 uses youtube.json; others use <video-stem>.youtube.json)")
    parser.add_argument("--force-upload", action="store_true", help="Create another upload despite an existing receipt; review your channel first")
    args = parser.parse_args()
    fact_path = args.fact if args.fact is not None else args.video.parent / "fact.json"
    if args.fact is not None or fact_path.exists():
        try:
            fact_data = json.loads(fact_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"Unable to load metadata: {exc}")
    else:
        fact_data = {"title": args.video.stem.replace("-", " ").title(), "tags": ["Facts", "Shorts"]}
    try:
        upload_short(args.video, fact_data, privacy_status=args.privacy,
                     force_upload=args.force_upload, receipt_path=args.receipt)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Upload failed: {exc}\n")


if __name__ == "__main__":
    main()
