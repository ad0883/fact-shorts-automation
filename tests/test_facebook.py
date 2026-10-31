import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline import publish_facebook as fb
from pipeline.run_store import write_json, read_json


class FacebookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        (self.directory/'instagram_reel.mp4').write_bytes(b'video')
        write_json(self.directory/'fact.json', {'title': 'Test'})
        for p in [patch.dict(os.environ, {'FACEBOOK_PAGE_ID':'123', 'FACEBOOK_PAGE_ACCESS_TOKEN':'secret',
                                          'META_API_VERSION':'v26.0'}, clear=True),
                  patch.object(fb, 'prepare_package', return_value={'caption':'Credits'}),
                  patch.object(fb, 'validate_video')]:
            p.start(); self.addCleanup(p.stop)

    def test_publish_and_repeat_are_idempotent(self):
        with patch.object(fb, 'upload_file') as upload, patch.object(fb, 'request', side_effect=[
            {'video_id':'456','upload_url':'https://rupload.facebook.com/video-upload/v26.0/456'},
            {'success':True}, {'status':{'publishing_phase':{'status':'complete'}}}]) as request:
            result = fb.publish(self.directory)
            self.assertEqual(result['status'], 'published')
            self.assertEqual(fb.publish(self.directory), result)
            self.assertEqual(request.call_count, 3)
            upload.assert_called_once()

    def test_uncertain_finish_only_checks_existing_video(self):
        with patch.object(fb, 'upload_file'), patch.object(fb, 'request', side_effect=[
            {'video_id':'456','upload_url':'https://rupload.facebook.com/video-upload/v26.0/456'},
            RuntimeError('timeout')]):
            with self.assertRaisesRegex(RuntimeError,'timeout'):fb.publish(self.directory)
        self.assertEqual(read_json(self.directory/'facebook.json')['status'], 'publishing')
        with patch.object(fb, 'request', return_value={'status':{'publishing_phase':{'status':'complete'}}}) as request:
            self.assertEqual(fb.publish(self.directory)['video_id'],'456')
            self.assertEqual(request.call_args.args[0], 'GET')
            request.assert_called_once()

    def test_unknown_upload_host_never_receives_token(self):
        with patch.object(fb, 'request') as request:
            with self.assertRaises(ValueError):
                fb.upload_file('https://evil.example/upload', 'secret', self.directory/'instagram_reel.mp4')
            request.assert_not_called()

    def test_browser_history_blocks_previously_published_run(self):
        from pipeline.publishing_history import check_browser_history
        with patch('pipeline.publishing_history.settings.PROJECT_ROOT', self.directory):
            write_json(self.directory/'output'/'instagram_upload_queue.json', {'items': [{
                'run': 'prior', 'sha256': 'same', 'status': 'published',
                'destinations': ['instagram', 'facebook']}]})
            for platform in ['instagram', 'facebook']:
                with self.assertRaisesRegex(ValueError, 'already published'):
                    check_browser_history(Path('prior'), 'same', platform)
            check_browser_history(Path('prior'), 'different', 'facebook')

    def test_demo_cannot_publish(self):
        import main
        with self.assertRaisesRegex(ValueError, 'cannot be posted to Facebook'):
            main.run(demo=True, facebook=True)
