"""Instagram Reels exports and opt-in publishing through Instagram Login.

https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/
"""
import argparse
import ipaddress
import math
import os
import re
import time
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from config import settings
from pipeline.run_store import file_hash, read_json, write_json
from pipeline.select_background import probe_media, run_media_command
from pipeline.upload_youtube import _topic_hashtags, _upload_lock
from pipeline.youtube_package import attribution_text


def prepare_video(directory):
    source = directory/'video.mp4'
    output = directory/'instagram_reel.mp4'
    stamp = directory/'instagram_export.json'
    fingerprint = file_hash(source)
    if output.exists() and stamp.exists():
        saved = read_json(stamp)
        if saved.get('source_sha256') == fingerprint and saved.get('sha256') == file_hash(output):
            return output
    validate_video(source)
    with tempfile.TemporaryDirectory(prefix='.instagram-', dir=directory) as tmp:
        target = Path(tmp)/'reel.mp4'
        run_media_command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                           '-i', str(source), '-map', '0:v:0', '-map', '0:a:0',
                           '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k', '-ar', '48000',
                           '-ac', '2', '-movflags', '+faststart', str(target)])
        validate_video(target)
        target.replace(output)
    write_json(stamp, {'source_sha256': fingerprint, 'sha256': file_hash(output)})
    return output


def prepare_package(directory, fact, segments):
    directory = Path(directory)
    prepare_video(directory)
    tags = _topic_hashtags(fact.get('tags', []), fact)
    footer = '\n\n'.join(part for part in (
        ' '.join('#' + tag for tag in tags), attribution_text(segments)) if part)
    if len(footer) > 2100:
        raise ValueError('Instagram credits are too long for a caption; shorten them without removing required attribution.')
    prose = fact.get('description') or fact.get('title', '')
    caption = '\n\n'.join(part for part in (prose[:2200-len(footer)-2].rstrip(), footer) if part)
    package = {'video': 'instagram_reel.mp4', 'media_type': 'REELS', 'caption': caption,
               'share_to_feed': True, 'sources': fact.get('sources', []),
               'notes': 'Review cross-platform media rights. Automatic publishing requires a professional account, an Instagram Login token, and a public HTTPS URL for this exact video.'}
    write_json(directory / 'instagram_metadata.json', package)
    (directory / 'instagram_upload.txt').write_text(
        'Instagram > + > Reel > select instagram_reel.mp4.\n\nCaption:\n' + caption +
        '\n\n' + package['notes'] + '\n', encoding='utf-8')
    return package


def validate_video(video):
    media = probe_media(video)
    visual = next((s for s in media.get('streams', []) if s.get('codec_type') == 'video'), {})
    duration = float(media.get('format', {}).get('duration', 0))
    if (video.suffix.lower() != '.mp4' or not math.isfinite(duration) or not 3 <= duration <= 900
            or not 0 < visual.get('width', 0) <= 1920 or visual.get('height', 0) < visual.get('width', 0)
            or visual.get('codec_name') not in {'h264', 'hevc'} or video.stat().st_size > 1024**3):
        raise ValueError('Reels export must be a vertical/square H.264 or HEVC MP4, 3–900 seconds, width <=1920 and size <=1 GB.')


def config():
    mode = os.environ.get('INSTAGRAM_LOGIN_MODE', 'instagram').strip()
    if mode not in {'instagram', 'facebook'}:
        raise ValueError('INSTAGRAM_LOGIN_MODE must be instagram or facebook.')
    account = os.environ.get('INSTAGRAM_USER_ID', '').strip()
    token = os.environ.get('INSTAGRAM_ACCESS_TOKEN', '').strip()
    version = os.environ.get('INSTAGRAM_API_VERSION', '').strip()
    if mode == 'facebook':
        token = os.environ.get('FACEBOOK_PAGE_ACCESS_TOKEN', '').strip()
        version = os.environ.get('META_API_VERSION', '').strip()
    if not account.isdigit() or not token or not re.fullmatch(r'v\d+\.\d+', version):
        raise ValueError('Configure INSTAGRAM_USER_ID, INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_API_VERSION in .env after switching to Creator/Business and setting up Instagram Login.')
    return account, token, ('https://graph.facebook.com/' if mode == 'facebook' else 'https://graph.instagram.com/') + version


def setup_issue():
    try:
        config()
    except ValueError as exc:
        return str(exc)
    return None


def api(method, root, path, token, **kwargs):
    try:
        with requests.request(method, root + '/' + path, headers={'Authorization': 'Bearer ' + token},
                              timeout=(10, 60), allow_redirects=False, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f'Instagram HTTP {response.status_code}; check token, permissions, account and media eligibility.')
            if len(response.content) > 2_000_000:
                raise RuntimeError('Instagram response exceeded metadata limit.')
            data = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError('Instagram request failed; credentials and response text are not logged.') from None
    if not isinstance(data, dict) or data.get('error'):
        raise RuntimeError('Instagram did not report success.')
    return data


def media_url(directory, explicit=None):
    url = explicit
    if not url and (directory/'instagram_media_url.json').exists():
        mapping = read_json(directory/'instagram_media_url.json')
        if mapping.get('sha256') != file_hash(directory/'instagram_reel.mp4'):
            raise ValueError('Instagram media URL points to an older render; update its URL and sha256.')
        url = mapping.get('video_url')
    if not url:
        base = os.environ.get('INSTAGRAM_MEDIA_BASE_URL', '').rstrip('/')
        if base:
            url = base + '/' + quote(directory.name, safe='') + '/instagram_reel.mp4'
    if not isinstance(url, str):
        raise ValueError('Host this exact video on HTTPS and provide --video-url or INSTAGRAM_MEDIA_BASE_URL. Local files cannot be fetched by Meta.')
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Instagram media must use a public HTTPS URL without embedded credentials.')
    try:
        private = not ipaddress.ip_address(host).is_global
    except ValueError:
        private = '.' not in host or host.endswith(('.localhost', '.local', '.internal'))
    if private:
        raise ValueError('Instagram cannot fetch a local/private media URL.')
    return url


def publish(directory, video_url=None):
    directory = Path(directory).resolve()
    account, token, root = config()
    video = directory/'instagram_reel.mp4'
    package = prepare_package(directory, read_json(directory/'fact.json'),
                              read_json(directory/'image_segments.json') if (directory/'image_segments.json').exists() else [])
    validate_video(video)
    fingerprint = file_hash(video)
    from pipeline.publishing_history import check_browser_history
    check_browser_history(directory, fingerprint, 'instagram')
    receipt = directory/'instagram.json'
    with _upload_lock(directory/'instagram-publish'):
        record = read_json(receipt) if receipt.exists() else None
        if record:
            if record.get('sha256') != fingerprint or record.get('account_id') != account:
                raise ValueError('Instagram receipt belongs to different media/account. Use a new run.')
            if record.get('status') == 'published':
                return record
            if record.get('status') == 'publishing':
                raise RuntimeError('Previous Instagram publish result is uncertain. Check your profile before retrying; the receipt prevents duplicates.')
            if record.get('status') != 'processing':
                raise RuntimeError('Instagram container failed or expired. Review instagram.json before starting a new submission.')
        if not record:
            url = media_url(directory, video_url)
            result = api('POST', root, account+'/media', token, data={
                'media_type': 'REELS', 'video_url': url, 'caption': package['caption'], 'share_to_feed': 'true'})
            container = str(result.get('id', ''))
            if not container.isdigit():
                raise RuntimeError('Instagram returned no valid container ID.')
            record = {'sha256': fingerprint, 'account_id': account, 'container_id': container, 'status': 'processing'}
            write_json(receipt, record)
        # Resume processing the same container instead of creating another.
        for attempt in range(12):
            status = api('GET', root, record['container_id'], token, params={'fields': 'status_code'}).get('status_code')
            if status == 'FINISHED':
                break
            if status in {'ERROR', 'EXPIRED', 'PUBLISHED'}:
                record['status'] = status.lower()
                if status == 'PUBLISHED':
                    record['status'] = 'publishing'  # Published remotely, ID needs reconciliation.
                write_json(receipt, record)
                raise RuntimeError(f'Instagram container is {status}; check your profile and saved receipt.')
            if status != 'IN_PROGRESS':
                raise RuntimeError('Unexpected Instagram processing status; progress is saved.')
            if attempt < 11:
                print('Instagram is processing the Reel…', flush=True)
                time.sleep(5)
        else:
            raise RuntimeError('Instagram is still processing. Resume later; the existing container will be reused.')
        record['status'] = 'publishing'
        write_json(receipt, record)
        result = api('POST', root, account+'/media_publish', token, data={'creation_id': record['container_id']})
        if not str(result.get('id', '')).isdigit():
            raise RuntimeError('Instagram returned no media ID; check the profile before retrying.')
        record.update(status='published', media_id=str(result['id']))
        write_json(receipt, record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--export', action='store_true', help='Prepare manual upload files only')
    parser.add_argument('--video-url', help='Public HTTPS URL of this exact exported video')
    args = parser.parse_args()
    if args.export:
        prepare_package(args.run, read_json(args.run/'fact.json'),
                        read_json(args.run/'image_segments.json') if (args.run/'image_segments.json').exists() else [])
        print('Instagram instructions: ' + str(args.run/'instagram_upload.txt'))
    else:
        result = publish(args.run, args.video_url)
        print('Instagram Reel published: ' + result['media_id'])


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from None
