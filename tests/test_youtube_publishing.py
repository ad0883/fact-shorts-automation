"""Publishing failure recovery and metadata tests; never contact YouTube."""

from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import main
from pipeline import publish_youtube as publish, upload_youtube as upload, youtube_package as package


class HttpError(Exception):
    def __init__(self, status):
        self.resp = SimpleNamespace(status=status)
        self.content = b'{"error":{"errors":[{"reason":"forbidden"}]}}'


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"mock video")
        self.receipt = self.root / "youtube.json"
        self.service = MagicMock()
        self.service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {
            "id": "video-1", "status": {"privacyStatus": "unlisted"}})
        self.libraries = SimpleNamespace(HttpError=HttpError, HttpLib2Error=type("NetworkError", (Exception,), {}),
                                         MediaFileUpload=MagicMock(side_effect=lambda *args, **kwargs: MagicMock()))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(publish, "validate_short", return_value=10))
        self.connect = self.stack.enter_context(patch.object(publish, "connected_service", return_value=(self.service, {"id": "channel-1", "title": "My channel"})))
        self.stack.enter_context(patch.object(upload, "_google_libraries", return_value=self.libraries))

    def run_publish(self):
        return publish.publish_short(self.video, {"title": "A real fact", "tags": ["astronomy"]}, privacy="unlisted",
                                     image_segments=[], receipt=self.receipt)

    def test_only_video_is_uploaded_once_without_any_attachment_files(self):
        result = self.run_publish()
        self.assertEqual(result["privacy"], "unlisted")
        self.assertEqual(result["publish_status"], "complete")
        self.assertEqual(result["upload_mode"], "video_only")
        self.run_publish()
        self.service.videos.return_value.insert.assert_called_once()
        self.service.thumbnails.assert_not_called()
        self.service.captions.assert_not_called()
        self.assertFalse((self.root / "thumbnail.jpg").exists())
        self.assertFalse((self.root / "captions.srt").exists())

    def test_old_failed_attachments_do_not_block_video_only_resume(self):
        result = self.run_publish()
        result.update(publish_status="incomplete", attachments={"thumbnail": {"status": "failed"}})
        self.receipt.write_text(json.dumps(result))
        resumed = self.run_publish()
        self.assertEqual(resumed["publish_status"], "complete")
        self.assertEqual(resumed["upload_mode"], "video_only")
        self.service.videos.return_value.insert.assert_called_once()
        self.service.thumbnails.assert_not_called()
        self.service.captions.assert_not_called()

    def test_changed_video_stops_before_network(self):
        self.run_publish()
        self.connect.reset_mock()
        self.video.write_bytes(b"edited video")
        with self.assertRaisesRegex(RuntimeError, "needs review"):
            self.run_publish()
        self.connect.assert_not_called()

    def test_uncertain_original_upload_never_attempts_attachments(self):
        self.service.videos.return_value.insert.return_value.next_chunk.side_effect = HttpError(403)
        with self.assertRaises(HttpError):
            self.run_publish()
        self.service.thumbnails.return_value.set.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "needs review"):
            self.run_publish()
        self.service.videos.return_value.insert.assert_called_once()


class ConfigurationTests(unittest.TestCase):
    def test_upload_readiness_is_optional_for_automatic_runs(self):
        from pipeline import doctor
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(doctor.settings, "YOUTUBE_CLIENT_SECRET_FILE", root / "client.json"), \
                 patch.object(doctor.settings, "YOUTUBE_TOKEN_FILE", root / "token.json"), \
                 patch.object(doctor.shutil, "which", return_value=None):
                checks = doctor.check_environment(upload=True, upload_required=False)
                upload_checks = [check for check in checks if check.get("upload")]
                self.assertTrue(upload_checks)
                self.assertFalse(any(check["required"] for check in upload_checks))
                credentials = next(check for check in upload_checks if check["name"] == "YouTube credentials")
                self.assertFalse(credentials["ok"])
                self.assertIn("--connect-youtube", credentials["detail"])
                strict = doctor.check_environment(upload=True)
                self.assertTrue(all(check["required"] for check in strict if check.get("upload")))

    def test_auto_upload_choice_is_saved_and_can_be_overridden(self):
        options = {}
        with patch.object(main.settings, "AUTO_UPLOAD_YOUTUBE", True), patch.object(main.settings, "YOUTUBE_PRIVACY_STATUS", "unlisted"):
            self.assertEqual(main.upload_options(None, None, options), (True, "unlisted"))
        with patch.object(main.settings, "AUTO_UPLOAD_YOUTUBE", False), patch.object(main.settings, "YOUTUBE_PRIVACY_STATUS", "public"):
            self.assertEqual(main.upload_options(None, None, options), (True, "unlisted"))
            self.assertEqual(main.upload_options(False, None, options), (False, "unlisted"))

    def test_auto_upload_never_uploads_demos_or_scripts(self):
        with patch.object(main.settings, "AUTO_UPLOAD_YOUTUBE", True):
            self.assertFalse(main.upload_options(None, None, {"demo": True})[0])
            self.assertFalse(main.upload_options(None, None, {}, script_only=True)[0])

    def test_no_upload_cli_and_connect_only(self):
        with patch("main.run") as run:
            self.assertEqual(main.main(["--no-upload"]), 0)
            self.assertFalse(run.call_args.kwargs["upload"])
        with patch("main.run") as run, patch.object(publish, "connect_channel") as connect:
            self.assertEqual(main.main(["--connect-youtube"]), 0)
            run.assert_not_called()
            connect.assert_called_once()

    def test_channel_pin_rejects_wrong_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "channel.json"
            path.write_text('{"id":"expected"}')
            with patch.object(upload, "get_authenticated_service"), patch.object(publish, "_channel", return_value={"id": "different", "title": "Another channel"}):
                with self.assertRaisesRegex(RuntimeError, "differs from the saved channel"):
                    publish.connected_service(path)

    def test_old_upload_only_oauth_grant_requires_new_consent(self):
        with tempfile.TemporaryDirectory() as temporary:
            token, client = Path(temporary) / "token.json", Path(temporary) / "client.json"
            token.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.upload"]}))
            client.write_text("{}")
            libraries = MagicMock()
            libraries.RefreshError = type("RefreshError", (Exception,), {})
            flow = libraries.InstalledAppFlow.from_client_secrets_file.return_value
            flow.run_local_server.return_value = SimpleNamespace(valid=True, to_json=lambda: json.dumps({"scopes": upload.SCOPES}))
            with patch.object(upload, "_google_libraries", return_value=libraries), redirect_stdout(io.StringIO()):
                upload.get_authenticated_service(client, token)
            libraries.Credentials.from_authorized_user_file.assert_not_called()
            flow.run_local_server.assert_called_once()

    def test_unattended_publish_never_waits_for_browser_consent(self):
        with tempfile.TemporaryDirectory() as temporary:
            libraries = MagicMock()
            libraries.RefreshError = type("RefreshError", (Exception,), {})
            with patch.object(upload, "_google_libraries", return_value=libraries):
                with self.assertRaisesRegex(RuntimeError, "--connect-youtube"):
                    upload.get_authenticated_service(Path(temporary) / "client.json", Path(temporary) / "token.json", allow_interactive=False)
            libraries.InstalledAppFlow.from_client_secrets_file.assert_not_called()


class PackageTests(unittest.TestCase):
    def test_hashtags_prioritize_actual_subject_over_generic_tags(self):
        body = upload.prepare_metadata({
            "title": "Why golf ball dimples matter",
            "fact_text": "Golf ball dimples change air resistance.",
            "tags": ["education", "did you know", "physics", "air resistance",
                     "golf ball dimples", "golfballdimples", "viral shorts", "#FYP"],
        })
        snippet = body["snippet"]
        self.assertEqual(snippet["description"],
                         "Golf ball dimples change air resistance.\n\n"
                         "#golfballdimples #airresistance #physics #Shorts")
        self.assertNotIn("viral shorts", snippet["tags"])
        self.assertNotIn("FYP", snippet["tags"])

    def test_hashtag_ranking_matches_words_and_supports_unicode(self):
        tags = ["art", "ocean", "Earth", "海洋"]
        self.assertEqual(upload._topic_hashtags(tags, {"title": "Earth and 海洋"}),
                         ["Earth", "海洋", "art"])
        self.assertEqual(upload._topic_hashtags(
            ["science", "Science", "sci_ence", "fyp", "facts"], {}),
            ["science"])

    def test_metadata_keeps_credits_and_hashtags_when_description_is_long(self):
        segments = [{"provenance": {"photographer": "A photographer", "provider": "commons",
                    "photo_url": "https://commons.wikimedia.org/wiki/File:Example.jpg", "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/", "modifications": "Cropped/resized"}}] * 4
        body = upload.prepare_metadata({"title": "A fact", "description": "🪐" * 2000,
                "tags": ["archaeology", "ancient technology", "Archaeology", "viral", "fyp", "Shorts"]}, "unlisted", image_segments=segments)
        description = body["snippet"]["description"]
        self.assertLessEqual(len(description.encode()), 5000)
        self.assertEqual(description.count("A photographer"), 1)
        self.assertIn("CC BY 4.0", description)
        self.assertIn("#ancienttechnology", description)
        self.assertNotIn("#viral", description)
        self.assertNotIn("#fyp", description)
        self.assertEqual(description.count("#Shorts"), 1)

    def test_required_credits_are_not_silently_truncated(self):
        with self.assertRaisesRegex(ValueError, "credits exceed"):
            upload.prepare_metadata({}, image_segments=[{"attribution": "x" * 6000}])

    def test_shorts_geometry_and_duration_are_checked(self):
        for width, height, duration in [(1920, 1080, 60), (1080, 1920, 181), (1080, 1920, float("nan"))]:
            with self.subTest(width=width, height=height, duration=duration), patch.object(package, "probe_media", return_value={
                "format": {"duration": duration}, "streams": [{"codec_type": "video", "width": width, "height": height}, {"codec_type": "audio"}]}):
                with self.assertRaises(ValueError):
                    package.validate_short(Path("mock.mp4"))

    def test_local_package_has_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(package, "validate_short", return_value=10):
                paths = package.prepare_package(root, {"title": "A real fact"}, [], "unlisted")
            self.assertEqual(paths, [root / "youtube_metadata.json"])
            metadata = json.loads(paths[0].read_text())
            self.assertEqual(metadata["upload_mode"], "video_only")
            self.assertNotIn("thumbnail", metadata)
            self.assertNotIn("captions", metadata)
            self.assertEqual(list(root.iterdir()), paths)


if __name__ == "__main__":
    unittest.main()
