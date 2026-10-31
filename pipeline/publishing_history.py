"""Protect videos already published through Business Suite from API re-posts."""
from config import settings
from pipeline.run_store import read_json


def check_browser_history(directory, sha256, platform):
    history = settings.PROJECT_ROOT/'output'/'instagram_upload_queue.json'
    if not history.exists():
        return
    for item in read_json(history).get('items', []):
        if item.get('run') != directory.name or item.get('sha256') != sha256:
            continue
        published = item.get('status') == 'published' and platform in item.get('destinations', ['instagram'])
        if platform == 'facebook' and item.get('facebook_status'):
            published = item['facebook_status'] == 'published'
        if published:
            raise ValueError(f'This run was already published to {platform} through Business Suite. '
                             f'Disable that destination when resuming; the saved history prevents duplicates.')
