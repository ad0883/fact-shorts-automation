"""Spotlight exports and opt-in Public Profile API publishing.

Contract: https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement
"""
import argparse
import base64
import getpass
import json
import math
import os
import re
import secrets
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from config import settings
from pipeline.run_store import file_hash, read_json, write_json
from pipeline.select_background import probe_media
from pipeline.upload_youtube import _atomic_write, _topic_hashtags, _upload_lock
from pipeline.youtube_package import attribution_text

API = 'https://businessapi.snapchat.com'
TOKEN_URL = 'https://accounts.snapchat.com/login/oauth2/access_token'


def token_path():
    return settings.env_path('SNAPCHAT_TOKEN_FILE', settings.PROJECT_ROOT / 'snapchat_token.json')


def request(method, url, **kwargs):
    try:
        with requests.request(method, url, timeout=(10, 120), allow_redirects=False, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f'Snapchat HTTP {response.status_code}; check API approval, permissions and credentials.')
            if len(response.content) > 2_000_000:
                raise RuntimeError('Snapchat response exceeded the metadata limit.')
            data = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError('Snapchat request failed; no credentials or provider response are logged.') from None
    if not isinstance(data, dict) or data.get('request_status', 'SUCCESS') != 'SUCCESS':
        raise RuntimeError('Snapchat did not report success.')
    return data


def save_token(data):
    if not data.get('access_token') or not data.get('refresh_token'):
        raise RuntimeError('Snapchat returned incomplete OAuth credentials.')
    _atomic_write(token_path(), json.dumps(data))
    token_path().chmod(0o600)


def credentials():
    values = [os.environ.get(name, '').strip() for name in ('SNAPCHAT_CLIENT_ID', 'SNAPCHAT_CLIENT_SECRET')]
    if not all(values):
        raise ValueError('Set SNAPCHAT_CLIENT_ID and SNAPCHAT_CLIENT_SECRET in .env after API approval.')
    return dict(zip(('client_id', 'client_secret'), values))


def connect():
    config = credentials()
    redirect = os.environ.get('SNAPCHAT_REDIRECT_URI', '').strip()
    if not redirect:
        raise ValueError('Set SNAPCHAT_REDIRECT_URI to the exact URI registered in your Snap OAuth app.')
    state = secrets.token_urlsafe(32)
    url = 'https://accounts.snapchat.com/login/oauth2/authorize?' + urlencode({
        'response_type': 'code', 'client_id': config['client_id'], 'redirect_uri': redirect,
        'scope': 'snapchat-profile-api', 'state': state})
    print('Authorize your approved app in the browser. Copy the final redirect URL even if its page does not load.')
    webbrowser.open(url)
    reply = urlsplit(getpass.getpass('Paste the redirect URL (hidden): ').strip())
    expected = urlsplit(redirect)
    query = parse_qs(reply.query)
    if ((reply.scheme, reply.netloc, reply.path) != (expected.scheme, expected.netloc, expected.path)
            or query.get('state') != [state] or not query.get('code')):
        raise ValueError('OAuth redirect or state did not match; no token was exchanged.')
    data = request('POST', TOKEN_URL, data={**config, 'grant_type': 'authorization_code',
                   'redirect_uri': redirect, 'code': query['code'][0]})
    save_token(data)
    print('Snapchat OAuth credentials saved. No video was posted.')


def access_token():
    saved = read_json(token_path())
    data = request('POST', TOKEN_URL, data={**credentials(), 'grant_type': 'refresh_token',
                                          'refresh_token': saved['refresh_token']})
    save_token({**saved, **data})
    return data['access_token']


def prepare_package(directory, fact, segments):
    directory = Path(directory)
    video = directory / 'video.mp4'
    media = probe_media(video)
    visual = next((s for s in media.get('streams', []) if s.get('codec_type') == 'video'), {})
    duration = float(media.get('format', {}).get('duration', 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Video duration must be finite and positive.')
    width, height = visual.get('width', 0), visual.get('height', 0)
    eligible = video.suffix.lower() == '.mp4' and 6 <= duration <= 60 and width >= 540 and height >= 960
    tags = _topic_hashtags(fact.get('tags', []), fact)[:2]
    suffix = ' '.join('#' + tag for tag in tags)
    title = ' '.join(fact.get('title', '').split())
    description = (title[:max(0, 159-len(suffix))].rstrip() + ' ' + suffix).strip()[:160]
    locale = os.environ.get('SNAPCHAT_LOCALE', 'en_US')
    if not re.fullmatch(r'[a-z]{2}_[A-Z]{2}', locale):
        raise ValueError('SNAPCHAT_LOCALE must look like en_US.')
    credits = attribution_text(segments)
    package = {'video': 'video.mp4', 'description': description, 'locale': locale,
               'api_eligible': eligible, 'duration_seconds': duration,
               'credits': credits, 'sources': fact.get('sources', []),
               'notes': 'API requires MP4, 6–60 seconds, at least 540x960. Review media rights and any required attribution before posting.'}
    write_json(directory / 'snapchat_metadata.json', package)
    (directory / 'snapchat_upload.txt').write_text(
        'Upload the original video.mp4 using Snapchat > Spotlight > +.\n'
        'Review the Share to Story option before submitting.\n\nDescription:\n' + description +
        '\n\n' + package['notes'] + '\n\n' + credits + '\n', encoding='utf-8')
    return package


def setup_issue():
    if not os.environ.get('SNAPCHAT_PROFILE_ID'):
        return 'Set SNAPCHAT_PROFILE_ID after Public Profile API approval.'
    if not token_path().is_file():
        return 'Connect an approved app: python -m pipeline.publish_snapchat --connect'
    try:
        credentials()
        import cryptography  # noqa: F401
    except (ValueError, ImportError):
        return 'Configure Snapchat credentials and install requirements-snapchat.txt.'
    return None


def publish(directory):
    directory = Path(directory).resolve()
    issue = setup_issue()
    if issue:
        raise ValueError(issue)
    profile = os.environ['SNAPCHAT_PROFILE_ID'].strip()
    if not re.fullmatch(r'[a-zA-Z0-9-]+', profile):
        raise ValueError('Invalid SNAPCHAT_PROFILE_ID.')
    video = directory / 'video.mp4'
    package = prepare_package(directory, read_json(directory / 'fact.json'),
                              read_json(directory / 'image_segments.json') if (directory / 'image_segments.json').exists() else [])
    if not package['api_eligible']:
        raise ValueError('Spotlight API requires MP4, 6–60 seconds and at least 540x960.')
    # The API description cannot safely accommodate arbitrary required credits.
    if package['credits'] and os.environ.get('SNAPCHAT_RIGHTS_REVIEWED', '').lower() != 'true':
        raise ValueError('Review snapchat_metadata.json credits and on-screen attribution; set SNAPCHAT_RIGHTS_REVIEWED=true after confirming Snapchat usage rights.')
    receipt = directory / 'snapchat.json'
    fingerprint = file_hash(video)
    with _upload_lock(directory / 'snapchat-publish'):
        if receipt.exists():
            previous = read_json(receipt)
            if previous.get('sha256') != fingerprint or previous.get('profile_id') != profile:
                raise ValueError('Snapchat receipt belongs to different media/profile. Use a new run.')
            if previous.get('status') == 'submitted':
                return previous
            raise RuntimeError('Previous Spotlight submission is uncertain. Check Snapchat before retrying; receipt prevents duplicate posts.')
        headers = {'Authorization': 'Bearer ' + access_token()}
        root = f'/v1/public_profiles/{profile}'
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        key, iv = os.urandom(32), os.urandom(16)
        with tempfile.TemporaryDirectory(prefix='snapchat-') as tmp:
            encrypted = Path(tmp) / 'video.enc'
            encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
            padder = PKCS7(128).padder()
            with video.open('rb') as source, encrypted.open('wb') as target:
                while chunk := source.read(1024*1024):
                    target.write(encryptor.update(padder.update(chunk)))
                target.write(encryptor.update(padder.finalize()) + encryptor.finalize())
            if encrypted.stat().st_size > 1024**3:
                raise ValueError('Spotlight API upload exceeds 1 GB.')
            media = request('POST', API + root + '/media', headers=headers, json={
                'type': 'VIDEO', 'name': video.name,
                'key': base64.b64encode(key).decode(), 'iv': base64.b64encode(iv).decode()})
            paths = [media.get('add_path', ''), media.get('finalize_path', '')]
            for path in paths:
                if not re.fullmatch(r'/[A-Za-z0-9_/-]+', path) or not path.endswith('/multipart-upload'):
                    raise RuntimeError('Unexpected Snapchat upload path.')
            with encrypted.open('rb') as source:
                part = 0
                while chunk := source.read(32*1024*1024):
                    part += 1
                    request('POST', API + paths[0], headers=headers, data={'action': 'ADD', 'part_number': part},
                            files={'file': ('video.enc', chunk, 'application/octet-stream')})
            request('POST', API + paths[1], headers=headers, files={'action': (None, 'FINALIZE')})
        record = {'sha256': fingerprint, 'profile_id': profile, 'media_id': media['media_id'], 'status': 'submitting'}
        write_json(receipt, record)  # Persist BEFORE the irreversible post request.
        result = request('POST', API + root + '/spotlights', headers=headers, json={
            'media_id': media['media_id'], 'description': package['description'], 'locale': package['locale']})
        if not result.get('spotlight_id'):
            raise RuntimeError('Snapchat returned no Spotlight ID; check the profile before retrying.')
        record.update(status='submitted', spotlight_id=result['spotlight_id'])
        write_json(receipt, record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--export', action='store_true', help='Prepare manual-upload metadata only; never post')
    parser.add_argument('--run', type=Path, help='Existing completed run to publish without rerendering')
    args = parser.parse_args()
    if args.connect:
        connect()
    elif args.run and args.export:
        prepare_package(args.run, read_json(args.run / 'fact.json'),
                        read_json(args.run / 'image_segments.json') if (args.run / 'image_segments.json').exists() else [])
        print('Manual upload instructions: ' + str(args.run / 'snapchat_upload.txt'))
    elif args.run:
        result = publish(args.run)
        print('Spotlight submitted: ' + result['spotlight_id'])
    else:
        parser.error('Choose --connect or --run DIRECTORY')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from None
