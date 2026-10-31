"""Offline Instagram publishing contracts and local export checks."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from pipeline import publish_instagram as ig
from pipeline.run_store import write_json, read_json, file_hash


class InstagramTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'video.mp4').write_bytes(b'original')
        (self.root/'instagram_reel.mp4').write_bytes(b'export')
        write_json(self.root/'fact.json', {'title': 'Ocean pressure', 'tags': ['ocean']})
        env = patch.dict(os.environ, {'INSTAGRAM_USER_ID': '123', 'INSTAGRAM_ACCESS_TOKEN': 'secret',
                                     'INSTAGRAM_API_VERSION': 'v25.0'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_caption_reserves_credits_and_export_does_not_post(self):
        with patch.object(ig, 'prepare_video'), patch.object(ig, 'api') as api:
            result = ig.prepare_package(self.root, {'description': 'A'*3000, 'tags': ['ocean']}, [{'attribution': 'Credit'}])
            self.assertLessEqual(len(result['caption']), 2200)
            self.assertIn('Credit', result['caption'])
            self.assertIn('#ocean', result['caption'])
            api.assert_not_called()

    def test_publish_polls_then_saves_receipt_before_final_post(self):
        def api(method, root, path, token, **kwargs):
            if path == '123/media':
                self.assertEqual(kwargs['data']['media_type'], 'REELS')
                return {'id': '456'}
            if path == '456':
                return {'status_code': 'FINISHED'}
            self.assertEqual(read_json(self.root/'instagram.json')['status'], 'publishing')
            return {'id': '789'}
        with patch.object(ig, 'prepare_video'), patch.object(ig, 'validate_video'), \
             patch.object(ig, 'api', side_effect=api) as call:
            result = ig.publish(self.root, 'https://example.org/reel.mp4')
            self.assertEqual(result['media_id'], '789')
            self.assertEqual(ig.publish(self.root), result)
            self.assertEqual(call.call_count, 3)

    def test_timeout_resumes_container_without_recreating(self):
        with patch.object(ig, 'prepare_video'), patch.object(ig, 'validate_video'), \
             patch.object(ig.time, 'sleep'), patch.object(ig, 'api') as api:
            api.side_effect = [{'id': '456'}] + [{'status_code': 'IN_PROGRESS'}]*12
            with self.assertRaisesRegex(RuntimeError, 'still processing'):
                ig.publish(self.root, 'https://example.org/reel.mp4')
            self.assertEqual(read_json(self.root/'instagram.json')['status'], 'processing')
            api.reset_mock()
            api.side_effect = [{'status_code': 'FINISHED'}, {'id': '789'}]
            ig.publish(self.root)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args_list[0].args[2], '456')

    def test_uncertain_publish_and_changed_media_do_not_repost(self):
        record = {'sha256': file_hash(self.root/'instagram_reel.mp4'), 'account_id': '123',
                  'container_id': '456', 'status': 'publishing'}
        write_json(self.root/'instagram.json', record)
        with patch.object(ig, 'prepare_video'), patch.object(ig, 'validate_video'), patch.object(ig, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                ig.publish(self.root)
            (self.root/'instagram_reel.mp4').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'different media'):
                ig.publish(self.root)
            api.assert_not_called()

    def test_media_url_rejects_private_hosts_and_stale_mapping(self):
        for url in ['file:///video.mp4', 'https://localhost/a', 'https://127.0.0.1/a',
                    'https://name:secret@example.org/a', 'https://example.local/a']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                ig.media_url(self.root, url)
        write_json(self.root/'instagram_media_url.json', {'sha256': 'old', 'video_url': 'https://example.org/a'})
        with self.assertRaisesRegex(ValueError, 'older render'):
            ig.media_url(self.root)

    def test_network_error_is_redacted(self):
        with patch.object(ig.requests, 'request', side_effect=requests.Timeout('secret')):
            with self.assertRaises(RuntimeError) as error:
                ig.api('POST', 'https://graph.instagram.com/v25.0', '123/media', 'secret')
            self.assertNotIn('secret', str(error.exception))

    def test_video_constraints(self):
        media = {'format': {'duration': '30'}, 'streams': [
            {'codec_type': 'video', 'codec_name': 'h264', 'width': 1080, 'height': 1920}]}
        with patch.object(ig, 'probe_media', return_value=media):
            ig.validate_video(self.root/'video.mp4')
            media['format']['duration'] = 'nan'
            with self.assertRaises(ValueError):
                ig.validate_video(self.root/'video.mp4')

    def test_failed_export_preserves_previous_video(self):
        with patch.object(ig, 'validate_video'), patch.object(ig, 'run_media_command', side_effect=RuntimeError('encoder failure')):
            with self.assertRaisesRegex(RuntimeError, 'encoder failure'):
                ig.prepare_video(self.root)
        self.assertEqual((self.root/'instagram_reel.mp4').read_bytes(), b'export')

    def test_facebook_login_uses_page_token_and_facebook_host(self):
        with patch.dict(os.environ, {'INSTAGRAM_LOGIN_MODE': 'facebook',
                                    'FACEBOOK_PAGE_ACCESS_TOKEN': 'page-secret',
                                    'META_API_VERSION': 'v26.0'}):
            self.assertEqual(ig.config(), ('123', 'page-secret', 'https://graph.facebook.com/v26.0'))

    def test_demo_cannot_publish(self):
        import main
        with self.assertRaisesRegex(ValueError, 'cannot be posted to Instagram'):
            main.run(demo=True, instagram=True)
