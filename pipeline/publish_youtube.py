"""Upload only the final video, with channel checks and duplicate protection."""

import json
from pathlib import Path

from config import settings
from pipeline import upload_youtube as uploader
from pipeline.youtube_package import validate_short


def _save(path: Path, data: dict):
    try:
        uploader._atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        raise RuntimeError(f"Could not save publishing progress. Video URL: {data.get('url', 'not uploaded yet')}") from exc


def _channel(youtube) -> dict:
    response = youtube.channels().list(part="snippet", mine=True).execute(num_retries=3)
    items = response.get("items", [])
    if len(items) != 1 or not items[0].get("id"):
        raise RuntimeError("Choose one YouTube channel during Google sign-in; no unique channel was returned.")
    return {"id": items[0]["id"], "title": items[0].get("snippet", {}).get("title", "YouTube channel")}


def connected_service(channel_file: Path | None = None):
    channel_file = Path(channel_file or settings.YOUTUBE_CHANNEL_FILE)
    youtube = uploader.get_authenticated_service(allow_interactive=False)
    channel = _channel(youtube)
    if channel_file.exists():
        saved = json.loads(channel_file.read_text(encoding="utf-8"))
        if saved.get("id") != channel["id"]:
            raise RuntimeError(f"Connected channel {channel['title']} ({channel['id']}) differs from the saved channel. "
                               "Run --connect-youtube to deliberately select and save this account.")
    else:
        _save(channel_file, channel)
    print(f"YouTube channel: {channel['title']} ({channel['id']})")
    return youtube, channel


def connect_channel() -> dict:
    """Authorize once and record the selected channel, without uploading anything."""
    youtube = uploader.get_authenticated_service()
    channel = _channel(youtube)
    _save(settings.YOUTUBE_CHANNEL_FILE, channel)
    print(f"Connected: {channel['title']}\nChannel: https://www.youtube.com/channel/{channel['id']}")
    print(f"Automatic uploads: {'on' if settings.AUTO_UPLOAD_YOUTUBE else 'off'}; visibility: {settings.YOUTUBE_PRIVACY_STATUS}")
    return channel


def publish_short(video: Path, fact: dict, *, privacy: str, image_segments: list[dict],
                  receipt: Path) -> dict:
    """No thumbnail or separate caption track is generated or uploaded."""
    validate_short(video)
    uploader.prepare_metadata(fact, privacy, image_segments=image_segments)
    protected = {path.resolve() for path in (video, settings.YOUTUBE_TOKEN_FILE,
                                             settings.YOUTUBE_CLIENT_SECRET_FILE, settings.YOUTUBE_CHANNEL_FILE)}
    if receipt.resolve() in protected:
        raise ValueError("The publishing receipt cannot overwrite media or account files.")
    with uploader._upload_lock(receipt.with_name("youtube-publish")):
        # Inspect an uncertain receipt before browser sign-in or any API calls.
        previous = uploader._load_receipt(receipt, uploader._fingerprint(video), False)
        youtube, channel = connected_service()
        if previous and previous.get("channel", {}).get("id", channel["id"]) != channel["id"]:
            raise RuntimeError("This video was uploaded to a different channel; reconnect that channel to resume.")
        result = uploader.upload_short(video, fact, privacy_status=privacy, receipt_path=receipt,
                                       youtube_service=youtube, image_segments=image_segments)
        result.update(channel=channel, publish_status="complete", upload_mode="video_only")
        _save(receipt, result)
        if result["privacy"] != privacy:
            print(f"YouTube visibility is {result['privacy']}; requested {privacy}. Check YouTube Studio.")
        print(f"YouTube ready ({result['privacy']}): {result['url']}")
        return result
