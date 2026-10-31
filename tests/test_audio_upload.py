"""Offline regression tests for narration, timestamps, and upload safeguards."""

import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from pipeline import generate_audio as audio
from pipeline import transcribe
from pipeline import upload_youtube as upload


class AudioTests(unittest.TestCase):
    def test_retries_write_atomically_and_clean_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration.mp3"
            target.write_bytes(b"existing")
            attempts = []

            async def generate(text, path, **kwargs):
                attempts.append(path)
                self.assertEqual(target.read_bytes(), b"existing")
                path.write_bytes(b"partial")
                if len(attempts) == 1:
                    raise ConnectionError("transient")
                path.write_bytes(b"complete narration")

            with patch.object(audio, "_generate_audio_edge", generate), patch.object(audio.time, "sleep"):
                audio.generate_audio("Hello", target, provider="edge", fallback="none")
            self.assertEqual(len(attempts), 2)
            self.assertEqual(target.read_bytes(), b"complete narration")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_failed_providers_preserve_previous_audio(self):
        async def generate(*args, **kwargs):
            raise ConnectionError("offline")

        gtts = SimpleNamespace(gTTS=MagicMock(side_effect=ConnectionError("offline")))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration.mp3"
            target.write_bytes(b"existing")
            with patch.object(audio, "_generate_audio_edge", generate), patch.dict(sys.modules, {"gtts": gtts}), patch.object(audio.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "edge:.*gtts:"):
                    audio.generate_audio("Hello", target, provider="edge", fallback="gtts", max_attempts=2)
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_empty_edge_result_uses_fallback(self):
        async def generate(*args, **kwargs):
            return None

        def save(path):
            Path(path).write_bytes(b"fallback")

        gtts = SimpleNamespace(gTTS=MagicMock(return_value=SimpleNamespace(save=save)))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration.mp3"
            with patch.object(audio, "_generate_audio_edge", generate), patch.dict(sys.modules, {"gtts": gtts}):
                metadata = audio.generate_audio("Hello", target, provider="edge", fallback="gtts", max_attempts=1)
            self.assertEqual(target.read_bytes(), b"fallback")
            self.assertIn("timeout", gtts.gTTS.call_args.kwargs)
            self.assertEqual(metadata["provider"], "gtts")
            self.assertTrue(metadata["fallback_used"])

    def test_gemini_failure_preserves_audio_without_basic_fallback(self):
        from pipeline.gemini_client import GeminiError
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration.mp3"
            target.write_bytes(b"existing")
            with patch("pipeline.gemini_speech.generate_speech", side_effect=GeminiError("HTTP 503")) as speech, \
                    patch.object(audio, "_generate_legacy") as legacy:
                with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                    audio.generate_audio("Hello", target, provider="gemini", fallback="none")
            legacy.assert_not_called()
            speech.assert_called_once()
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_gemini_records_actual_voice_and_style(self):
        def generate(text, path, **kwargs):
            path.write_bytes(b"expressive narration")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration.mp3"
            with patch("pipeline.gemini_speech.generate_speech", side_effect=generate) as speech:
                metadata = audio.generate_audio("Don't rush. Let it flow.", target, provider="gemini", fallback="none",
                                                gemini_voice="Achird", style="Warm and conversational")
            self.assertEqual(speech.call_args.args[0], "Don't rush. Let it flow.")
            self.assertEqual(speech.call_args.kwargs["style"], "Warm and conversational")
            self.assertEqual(metadata["provider"], "gemini")
            self.assertEqual(metadata["voice"], "Achird")
            self.assertFalse(metadata["fallback_used"])

    def test_invalid_provider_fails_before_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration.mp3"
            with self.assertRaisesRegex(ValueError, "TTS_PROVIDER"):
                audio.generate_audio("Hello", target, provider="unknown")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_local_script_still_checks_credentials_for_gemini_narration(self):
        from pipeline import doctor
        with patch.dict(os.environ, {}, clear=True), patch.object(doctor.settings, "TTS_PROVIDER", "gemini"), \
                patch.object(doctor.shutil, "which", return_value=None):
            checks = doctor.check_environment(need_generation=False, need_tts=True, need_whisper=False, need_footage=False)
        key_check = next(check for check in checks if check["name"] == "Gemini API key")
        self.assertFalse(key_check["ok"])
        self.assertTrue(key_check["required"])

    def test_empty_text_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "audio.mp3"
            with self.assertRaises(ValueError):
                audio.generate_audio(" ", target)
            self.assertFalse(target.exists())

    def test_edge_timeout_cancels_provider(self):
        cancelled = []

        async def hang(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        with patch.object(audio, "_generate_audio_edge", hang):
            with self.assertRaises(TimeoutError):
                asyncio.run(audio._generate_with_timeout("x", Path("unused"), "voice", "+0%", "+0Hz", 0.001))
        self.assertEqual(cancelled, [True])


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audio.mp3"
        self.path.write_bytes(b"test audio")

    def run_words(self, words, **kwargs):
        model = MagicMock()
        model.transcribe.return_value = ([SimpleNamespace(words=words)], None)
        with patch.object(transcribe, "get_model", return_value=model):
            result = transcribe.transcribe_words(self.path, **kwargs)
        return result, model

    def test_words_and_small_overlap_are_normalized(self):
        result, model = self.run_words([
            SimpleNamespace(word=" Hello ", start=0, end=0.5),
            SimpleNamespace(word=" world ", start=0.49, end=1.2),
        ], initial_prompt="Hello world")
        self.assertEqual(result, [
            {"word": "Hello", "start": 0.0, "end": 0.49},
            {"word": "world", "start": 0.49, "end": 1.2},
        ])
        self.assertEqual(model.transcribe.call_args.kwargs["initial_prompt"], "Hello world")

    def test_empty_transcription_is_error(self):
        for words in (None, [], [SimpleNamespace(word=" ", start=0, end=1)]):
            with self.subTest(words=words), self.assertRaisesRegex(RuntimeError, "No spoken words"):
                self.run_words(words)

    def test_missing_opening_retries_without_prompt_once_and_keeps_better_alignment(self):
        model = MagicMock()
        late = [SimpleNamespace(word="answer", start=4, end=5)]
        recovered = [SimpleNamespace(word="Question", start=0, end=1),
                     SimpleNamespace(word="answer", start=4, end=5)]
        model.transcribe.side_effect = [([SimpleNamespace(words=late)], None),
                                        ([SimpleNamespace(words=recovered)], None)]
        with patch.object(transcribe, "get_model", return_value=model):
            result = transcribe.transcribe_words(self.path, initial_prompt="Question answer")
        self.assertEqual(result[0]["word"], "Question")
        self.assertEqual(model.transcribe.call_count, 2)
        self.assertNotIn("initial_prompt", model.transcribe.call_args_list[1].kwargs)

    def test_retry_does_not_replace_better_first_transcription(self):
        model = MagicMock()
        first = [SimpleNamespace(word="Question", start=2, end=3), SimpleNamespace(word="answer", start=4, end=5)]
        worse = [SimpleNamespace(word="unrelated", start=0, end=1)]
        model.transcribe.side_effect = [([SimpleNamespace(words=first)], None), ([SimpleNamespace(words=worse)], None)]
        with patch.object(transcribe, "get_model", return_value=model):
            result = transcribe.transcribe_words(self.path, initial_prompt="Question answer")
        self.assertEqual([w["word"] for w in result], ["Question", "answer"])
        self.assertEqual(model.transcribe.call_count, 2)

    def test_invalid_timestamps_are_rejected(self):
        for start, end in [(-1, 1), (2, 1), (0, float("nan")), (0, float("inf")), (None, 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.run_words([SimpleNamespace(word="hello", start=start, end=end)])

    def test_zero_duration_asr_tokens_are_repaired(self):
        words, _ = self.run_words([SimpleNamespace(word="hello", start=1, end=1)])
        self.assertEqual(words, [{"word": "hello", "start": 1.0, "end": 1.01}])

    def test_backwards_word_order_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "out of order"):
            self.run_words([
                SimpleNamespace(word="one", start=1, end=2),
                SimpleNamespace(word="two", start=0.5, end=1.5),
            ])

    def test_missing_audio_never_loads_model(self):
        with patch.object(transcribe, "get_model") as model, self.assertRaises(FileNotFoundError):
            transcribe.transcribe_words(self.path.with_name("missing.mp3"))
        model.assert_not_called()


class FakeHttpError(Exception):
    def __init__(self, status):
        self.resp = SimpleNamespace(status=status)


class FakeHttpLibError(Exception):
    pass


class FakeRefreshError(Exception):
    pass


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "video.mp4"
        self.path.write_bytes(b"not a real video; all APIs are mocked")
        self.receipt = self.path.parent / "youtube.json"
        self.request = MagicMock()
        self.request.next_chunk.return_value = (None, {"id": "test-video", "status": {"privacyStatus": "private"}})
        self.service = MagicMock()
        self.service.videos.return_value.insert.return_value = self.request
        self.media = MagicMock()
        self.libraries = SimpleNamespace(
            HttpError=FakeHttpError, HttpLib2Error=FakeHttpLibError,
            MediaFileUpload=MagicMock(return_value=self.media),
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(upload, "_google_libraries", return_value=self.libraries))
        self.authenticate = self.stack.enter_context(patch.object(upload, "get_authenticated_service", return_value=self.service))
        self.stack.enter_context(patch.object(upload.time, "sleep"))

    def test_upload_receipt_prevents_duplicate_upload(self):
        first = upload.upload_short(self.path, {"title": "Fact"})
        second = upload.upload_short(self.path, {"title": "Fact"})
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "uploaded")
        self.assertEqual(first["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(self.receipt.read_text()), first)
        self.authenticate.assert_called_once()
        self.request.next_chunk.assert_called_once()
        self.media.stream.return_value.close.assert_called_once()
        self.assertFalse(self.receipt.with_name(self.receipt.name + ".lock").exists())

    def test_transient_http_and_network_errors_retry(self):
        self.request.next_chunk.side_effect = [
            FakeHttpError(429), FakeHttpError(503), ConnectionError("reset"),
            (None, {"id": "uploaded"}),
        ]
        result = upload.upload_short(self.path, {})
        self.assertEqual(result["video_id"], "uploaded")
        self.assertEqual(self.request.next_chunk.call_count, 4)

    def test_permanent_error_leaves_uncertain_receipt_and_blocks_duplicate(self):
        self.request.next_chunk.side_effect = FakeHttpError(403)
        with self.assertRaises(FakeHttpError):
            upload.upload_short(self.path, {})
        self.assertEqual(self.request.next_chunk.call_count, 1)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "uploading")
        with self.assertRaisesRegex(RuntimeError, "needs review"):
            upload.upload_short(self.path, {})
        self.assertEqual(self.request.next_chunk.call_count, 1)

    def test_retry_budget_is_bounded(self):
        self.request.next_chunk.side_effect = FakeHttpError(503)
        with self.assertRaises(FakeHttpError):
            upload.upload_short(self.path, {}, max_retries=2)
        self.assertEqual(self.request.next_chunk.call_count, 3)

    def test_force_upload_explicitly_allows_second_upload(self):
        upload.upload_short(self.path, {})
        upload.upload_short(self.path, {}, force_upload=True)
        self.assertEqual(self.request.next_chunk.call_count, 2)

    def test_changed_video_cannot_reuse_or_replace_existing_receipt(self):
        original = upload.upload_short(self.path, {})
        self.path.write_bytes(b"different video content")
        with self.assertRaisesRegex(RuntimeError, "needs review"):
            upload.upload_short(self.path, {})
        self.assertEqual(json.loads(self.receipt.read_text()), original)
        self.assertEqual(self.request.next_chunk.call_count, 1)

    def test_other_video_names_use_separate_receipts(self):
        another = self.path.with_name("another.mp4")
        another.write_bytes(b"another video")
        upload.upload_short(self.path, {})
        upload.upload_short(another, {})
        self.assertTrue(self.receipt.is_file())
        self.assertTrue(another.with_suffix(".youtube.json").is_file())
        self.assertEqual(self.request.next_chunk.call_count, 2)

    def test_invalid_privacy_is_rejected_before_authentication(self):
        with self.assertRaises(ValueError):
            upload.upload_short(self.path, {}, privacy_status="publci")
        self.authenticate.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_receipt_cannot_overwrite_video(self):
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            upload.upload_short(self.path, {}, receipt_path=self.path)
        self.authenticate.assert_not_called()

    def test_existing_lock_blocks_concurrent_upload(self):
        lock = self.receipt.with_name(self.receipt.name + ".lock")
        lock.write_text("123")
        with self.assertRaisesRegex(RuntimeError, "lock already exists"):
            upload.upload_short(self.path, {})
        self.assertTrue(lock.exists())
        self.authenticate.assert_not_called()

    def test_failed_receipt_write_reports_remote_video_url(self):
        real_write = upload._atomic_write

        def write(path, text):
            if json.loads(text)["status"] == "uploaded":
                raise OSError("disk full")
            real_write(path, text)

        with patch.object(upload, "_atomic_write", side_effect=write), self.assertRaisesRegex(RuntimeError, "watch\\?v=test-video"):
            upload.upload_short(self.path, {})
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "uploading")

    def test_metadata_respects_unicode_bytes_and_tag_accounting(self):
        result = upload.prepare_metadata({
            "title": "<" + "é" * 150 + ">",
            "description": "🪐" * 2000,
            "tags": ["#Space", "space", "a b" * 100, "c" * 300, "Facts"],
        }, "private")
        self.assertEqual(len(result["snippet"]["title"]), 100)
        self.assertLessEqual(len(result["snippet"]["description"].encode("utf-8")), 5000)
        tags = result["snippet"]["tags"]
        self.assertEqual(tags.count("Space"), 1)
        cost = sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(0, len(tags) - 1)
        self.assertLessEqual(cost, 500)
        self.assertNotIn("#Shorts", result["snippet"]["title"])

    def test_invalid_metadata_types_are_rejected(self):
        for metadata in ([], {"title": None}, {"tags": "space"}, {"tags": [1]}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                upload.prepare_metadata(metadata)


class OAuthTests(unittest.TestCase):
    def test_invalid_nonexpired_credentials_launch_flow_and_secure_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token.json"
            token.write_text(json.dumps({"scopes": upload.SCOPES}))
            secret = Path(directory) / "client.json"
            secret.write_text("{}")
            old = SimpleNamespace(valid=False, expired=False, refresh_token=None)
            new = SimpleNamespace(valid=True, to_json=lambda: '{"token":"test-only"}')
            flow = MagicMock()
            flow.run_local_server.return_value = new
            libraries = SimpleNamespace(
                Credentials=MagicMock(), RefreshError=FakeRefreshError,
                InstalledAppFlow=MagicMock(), build=MagicMock(), Request=MagicMock(),
            )
            libraries.Credentials.from_authorized_user_file.return_value = old
            libraries.InstalledAppFlow.from_client_secrets_file.return_value = flow
            with patch.object(upload, "_google_libraries", return_value=libraries):
                upload.get_authenticated_service(secret, token)
            flow.run_local_server.assert_called_once_with(port=0)
            self.assertEqual(json.loads(token.read_text()), {"token": "test-only"})
            if os.name != "nt":
                self.assertEqual(token.stat().st_mode & 0o777, 0o600)

    def test_oauth_network_refresh_failure_does_not_start_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token.json"
            token.write_text(json.dumps({"scopes": upload.SCOPES}))
            credentials = MagicMock(valid=False, refresh_token="test")
            credentials.refresh.side_effect = ConnectionError("offline")
            libraries = SimpleNamespace(
                Credentials=MagicMock(), RefreshError=FakeRefreshError,
                InstalledAppFlow=MagicMock(), Request=MagicMock(),
            )
            libraries.Credentials.from_authorized_user_file.return_value = credentials
            with patch.object(upload, "_google_libraries", return_value=libraries), self.assertRaises(ConnectionError):
                upload.get_authenticated_service("unused.json", token)
            libraries.InstalledAppFlow.from_client_secrets_file.assert_not_called()

    def test_upload_module_and_help_load_without_site_packages(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-S", str(root / "pipeline" / "upload_youtube.py"), "--help"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--force-upload", result.stdout)


if __name__ == "__main__":
    unittest.main()
