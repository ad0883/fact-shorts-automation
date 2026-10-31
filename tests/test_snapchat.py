"""Offline Spotlight contract, OAuth and duplicate-protection regressions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import requests
from pipeline import publish_snapchat as snap
from pipeline.run_store import write_json, read_json, file_hash


class SpotlightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'video.mp4').write_bytes(b'video')
        write_json(self.root / 'fact.json', {'title': 'Ocean pressure', 'tags': ['ocean', 'pressure']})
        self.media = {'format': {'duration': '30'}, 'streams': [
            {'codec_type': 'video', 'width': 1080, 'height': 1920}]}
        self.env = patch.dict(os.environ, {'SNAPCHAT_PROFILE_ID': 'profile-123', 'SNAPCHAT_LOCALE': 'en_US'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_export_bounds_caption_preserves_credits_and_checks_api_limits(self):
        with patch.object(snap, 'probe_media', return_value=self.media):
            package = snap.prepare_package(self.root, {'title': 'A' * 300, 'tags': ['ocean']}, [{'attribution': 'Credit required'}])
            self.assertLessEqual(len(package['description']), 160)
            self.assertIn('#ocean', package['description'])
            self.assertIn('Credit required', package['credits'])
            self.assertTrue(package['api_eligible'])
            self.assertTrue((self.root / 'snapchat_upload.txt').is_file())
            for duration in ('5', '61'):
                self.media['format']['duration'] = duration
                self.assertFalse(snap.prepare_package(self.root, {}, [])['api_eligible'])
            self.media['format']['duration'] = 'nan'
            with self.assertRaisesRegex(ValueError, 'finite'):
                snap.prepare_package(self.root, {}, [])

    def test_upload_encrypts_chunks_and_persists_receipt_before_post(self):
        calls = []
        def request(method, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith('/media'):
                self.assertIn('key', kwargs['json'])
                return {'media_id': 'media', 'add_path': '/us/v1/media/multipart-upload',
                        'finalize_path': '/us/v1/media/multipart-upload'}
            if url.endswith('/spotlights'):
                self.assertEqual(read_json(self.root/'snapchat.json')['status'], 'submitting')
                self.assertEqual(kwargs['json']['locale'], 'en_US')
                return {'spotlight_id': 'spotlight-1'}
            return {}
        with patch.object(snap, 'setup_issue', return_value=None), \
             patch.object(snap, 'probe_media', return_value=self.media), \
             patch.object(snap, 'access_token', return_value='private-token') as token, \
             patch.object(snap, 'request', side_effect=request):
            result = snap.publish(self.root)
            self.assertEqual(result['status'], 'submitted')
            self.assertEqual(len(calls), 4)
            self.assertNotEqual(calls[1][1]['files']['file'][1], b'video')
            self.assertEqual(snap.publish(self.root), result)
            token.assert_called_once()
            (self.root/'video.mp4').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'different media'):
                snap.publish(self.root)

    def test_uncertain_submission_is_never_reposted(self):
        write_json(self.root/'snapchat.json', {'status': 'submitting', 'sha256': file_hash(self.root/'video.mp4'),
                                             'profile_id': 'profile-123'})
        with patch.object(snap, 'setup_issue', return_value=None), \
             patch.object(snap, 'probe_media', return_value=self.media), \
             patch.object(snap, 'request') as request:
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                snap.publish(self.root)
            request.assert_not_called()

    def test_oauth_wrong_state_cannot_exchange_code(self):
        with patch.dict(os.environ, {'SNAPCHAT_REDIRECT_URI': 'http://localhost/callback'}), \
             patch.object(snap, 'credentials', return_value={'client_id': 'id', 'client_secret': 'secret'}), \
             patch.object(snap.webbrowser, 'open'), \
             patch.object(snap.getpass, 'getpass', return_value='http://localhost/callback?code=secret&state=wrong'), \
             patch.object(snap, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'state'):
                snap.connect()
            request.assert_not_called()

    def test_http_failures_never_echo_secrets_or_retry(self):
        with patch.object(snap.requests, 'request', side_effect=requests.Timeout('secret-token')) as request:
            with self.assertRaises(RuntimeError) as error:
                snap.request('POST', snap.API, headers={'Authorization': 'Bearer secret-token'})
            self.assertNotIn('secret-token', str(error.exception))
            request.assert_called_once()

    def test_setup_missing_is_actionable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('PROFILE_ID', snap.setup_issue())

    def test_refreshed_credentials_are_private_and_rotated(self):
        path = self.root/'token.json'
        write_json(path, {'access_token': 'old', 'refresh_token': 'old-refresh'})
        with patch.object(snap, 'token_path', return_value=path), \
             patch.object(snap, 'credentials', return_value={}), \
             patch.object(snap, 'request', return_value={'access_token': 'new', 'refresh_token': 'new-refresh'}):
            self.assertEqual(snap.access_token(), 'new')
            self.assertEqual(read_json(path)['refresh_token'], 'new-refresh')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

class DualPublishingTests(unittest.TestCase):
    def test_youtube_failure_still_attempts_spotlight_and_saves_both_results(self):
        from contextlib import ExitStack, redirect_stdout, redirect_stderr
        import io
        import main
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            write_json(root/'fact.json', main.DEMO_FACT)
            (root/'voice.wav').write_bytes(b'audio')
            write_json(root/'words.json', [{'word': 'Hello', 'start': 0, 'end': 1}])
            stack.enter_context(patch('pipeline.doctor.check_environment', return_value=[]))
            stack.enter_context(patch('pipeline.select_background.get_duration_seconds', return_value=10))
            stack.enter_context(patch('pipeline.select_background.select_background_clip', side_effect=lambda d,p,**kw:p.write_bytes(b'background')))
            stack.enter_context(patch('pipeline.render_video.render_final_video', side_effect=lambda b,a,c,p,**kw:p.write_bytes(b'video')))
            stack.enter_context(patch('pipeline.youtube_package.validate_short', return_value=10))
            stack.enter_context(patch.object(snap, 'prepare_package'))
            stack.enter_context(patch.object(snap, 'setup_issue', return_value=None))
            spotlight = stack.enter_context(patch.object(snap, 'publish', return_value={'spotlight_id': 'snap-1'}))
            stack.enter_context(patch('pipeline.publish_youtube.publish_short', side_effect=RuntimeError('upload unavailable')))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            with self.assertRaisesRegex(RuntimeError, 'YouTube: upload unavailable'):
                main.run(script=root/'fact.json', audio=root/'voice.wav', words=root/'words.json',
                         output_dir=root/'runs', no_images=True, upload=True, snapchat=True)
            spotlight.assert_called_once()
            state = read_json(next((root/'runs').glob('*/run.json')))
            self.assertEqual(state['snapchat_id'], 'snap-1')
            self.assertEqual(state['upload_error'], 'upload unavailable')

    def test_demo_cannot_post_to_spotlight(self):
        import main
        with self.assertRaisesRegex(ValueError, 'cannot be posted'):
            main.run(demo=True, snapchat=True)
