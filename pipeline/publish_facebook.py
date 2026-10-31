"""Publish local Reels to a Facebook Page, with durable duplicate protection."""
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from pipeline.publish_instagram import prepare_package, validate_video
from pipeline.run_store import read_json, write_json, file_hash
from pipeline.upload_youtube import _upload_lock


def config():
    page = os.environ.get('FACEBOOK_PAGE_ID', '').strip()
    token = os.environ.get('FACEBOOK_PAGE_ACCESS_TOKEN', '').strip()
    version = os.environ.get('META_API_VERSION', '').strip()
    if not page.isdigit() or not token or not re.fullmatch(r'v\d+\.\d+', version):
        raise ValueError('Set FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN and META_API_VERSION in .env.')
    return page, token, 'https://graph.facebook.com/' + version


def setup_issue():
    try:
        config()
    except ValueError as exc:
        return str(exc)
    return None


def request(method, url, token, *, upload=False, **kwargs):
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = ('OAuth ' if upload else 'Bearer ') + token
    try:
        with requests.request(method, url, headers=headers, timeout=(10, 180),
                              allow_redirects=False, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f'Facebook HTTP {response.status_code}; check Page token and permissions.')
            data = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError('Facebook request failed; credentials and response text are not logged.') from None
    if not isinstance(data, dict) or data.get('error'):
        raise RuntimeError('Facebook did not report success.')
    return data


def upload_file(url, token, video):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'rupload.facebook.com'
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Refusing an unexpected Meta upload host.')
    with video.open('rb') as stream:
        result = request('POST', url, token, upload=True, data=stream,
                         headers={'offset': '0', 'file_size': str(video.stat().st_size),
                                  'Content-Type': 'application/octet-stream'})
    if result.get('success') is not True:
        raise RuntimeError('Facebook did not confirm the video upload.')


def publish(directory):
    directory = Path(directory).resolve()
    page, token, root = config()
    fact = read_json(directory/'fact.json')
    package = prepare_package(directory, fact, read_json(directory/'image_segments.json')
                              if (directory/'image_segments.json').exists() else [])
    video = directory/'instagram_reel.mp4'
    validate_video(video)
    fingerprint = file_hash(video)
    from pipeline.publishing_history import check_browser_history
    check_browser_history(directory, fingerprint, 'facebook')
    receipt = directory/'facebook.json'
    with _upload_lock(directory/'facebook-publish'):
        record = read_json(receipt) if receipt.exists() else None
        if record:
            if record.get('sha256') != fingerprint or record.get('page_id') != page:
                raise ValueError('Facebook receipt belongs to different media/Page. Use a new run.')
            if record['status'] == 'published':
                return record
            if record['status'] in {'uploading', 'failed'}:
                raise RuntimeError('Previous Facebook upload needs review; receipt prevents a duplicate.')
        else:
            result = request('POST', root+'/'+page+'/video_reels', token, data={'upload_phase': 'start'})
            video_id = str(result.get('video_id', ''))
            if not video_id.isdigit():
                raise RuntimeError('Facebook returned no video ID.')
            record = {'page_id': page, 'video_id': video_id, 'sha256': fingerprint, 'status': 'uploading'}
            write_json(receipt, record)
            upload_file(result.get('upload_url', ''), token, video)
            record['status'] = 'uploaded'
            write_json(receipt, record)
        if record['status'] == 'uploaded':
            # Persist before final mutation: a timeout must never create another post.
            record['status'] = 'publishing'
            write_json(receipt, record)
            result = request('POST', root+'/'+page+'/video_reels', token, data={
                'upload_phase': 'finish', 'video_id': record['video_id'], 'video_state': 'PUBLISHED',
                'description': package['caption'], 'title': fact.get('title', '')})
            if result.get('success') is not True:
                raise RuntimeError('Facebook publication is uncertain; resume to check the saved video ID.')
        for attempt in range(12):
            status = request('GET', root+'/'+record['video_id'], token, params={'fields': 'status'}).get('status', {})
            if status.get('publishing_phase', {}).get('status') == 'complete':
                record.update(status='published', url='https://www.facebook.com/reel/'+record['video_id'])
                write_json(receipt, record)
                return record
            if status.get('video_status') == 'error' or any(
                    isinstance(v, dict) and v.get('status') == 'error' for v in status.values()):
                record['status'] = 'failed'
                write_json(receipt, record)
                raise RuntimeError('Facebook processing failed; review the saved video before retrying.')
            if attempt < 11:
                time.sleep(5)
        raise RuntimeError('Facebook publication is not confirmed yet. Resume to check the same video; do not delete the receipt.')
