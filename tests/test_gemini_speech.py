"""Speech request, audio integrity, and recovery regressions; no live API calls."""

import base64
import io
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

import requests

from pipeline import gemini_speech as speech
from pipeline.gemini_client import GeminiError
from tests.test_content import Response


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"".join(struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * i / 24000)))
                                  for i in range(24000)))
    return output.getvalue()


def payload(data=None, mime="audio/wav", finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(wav_bytes() if data is None else data).decode()}}
    ]}}]}


@patch.dict("os.environ", {"GEMINI_API_KEY": "speech-test-key", "PATH": os.environ.get("PATH", os.defpath)}, clear=True)
class GeminiSpeechTests(unittest.TestCase):
    def generate(self, path):
        speech.generate_speech("Don't rush. Let it flow.", path, model="gemini-3.8-flash-tts",
                               voice="Achird", style="Warm and conversational", max_attempts=2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for audio conversion")
    @patch("pipeline.gemini_speech.requests.post")
    def test_verbatim_transcript_and_separate_style_produce_a_real_mp3(self, post):
        response = Response(payload())
        post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "narration's sample.mp3"
            self.generate(target)
            self.assertGreater(target.stat().st_size, 0)
            self.assertNotEqual(target.read_bytes()[:4], b"RIFF")
            self.assertEqual(list(Path(directory).iterdir()), [target])
        part = post.call_args.kwargs["json"]["contents"][0]["parts"][0]
        self.assertEqual(part["text"], "Don't rush. Let it flow.")
        self.assertEqual(part["speech_metadata"], {"style": "Warm and conversational"})
        self.assertNotIn("speech-test-key", post.call_args.args[0])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertTrue(response.closed)

    def test_partial_corrupt_and_non_audio_responses_are_rejected(self):
        cases = [None, {}, payload(finish="MAX_TOKENS"), payload(mime="text/plain"),
                 payload(b"not audio"), payload(wav_bytes()[:-100]), payload(b"")]
        invalid_base64 = payload()
        invalid_base64["candidates"][0]["content"]["parts"][0]["inlineData"]["data"] = "???"
        cases.append(invalid_base64)
        for case in cases:
            with self.subTest(case=str(case)[:70]), self.assertRaises(GeminiError):
                speech._extract_wav(case)

    @patch("pipeline.gemini_speech.time.sleep")
    @patch("pipeline.gemini_speech.requests.post")
    def test_permanent_error_is_redacted_and_not_retried(self, post, sleep):
        response = Response({"error": {"message": "Bad credential speech-test-key"}}, status=400)
        post.return_value = response
        with self.assertRaises(GeminiError) as raised:
            self.generate(Path("unused.mp3"))
        self.assertNotIn("speech-test-key", str(raised.exception))
        self.assertIn("HTTP 400", str(raised.exception))
        post.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(response.closed)

    @patch("pipeline.gemini_speech.time.sleep")
    @patch("pipeline.gemini_speech.requests.post")
    def test_transient_http_and_network_retries_are_bounded(self, post, sleep):
        post.side_effect = [Response(status=503), requests.Timeout("speech-test-key")]
        with self.assertRaises(GeminiError) as raised:
            self.generate(Path("unused.mp3"))
        self.assertNotIn("speech-test-key", str(raised.exception))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()

    @patch("pipeline.gemini_speech.MAX_RESPONSE_BYTES", 10)
    @patch("pipeline.gemini_speech.requests.post")
    def test_oversized_response_is_closed_before_conversion(self, post):
        response = Response(body=b"x" * 11)
        post.return_value = response
        with self.assertRaisesRegex(GeminiError, "size limit"):
            self.generate(Path("unused.mp3"))
        self.assertTrue(response.closed)

    @patch("pipeline.gemini_speech.requests.post")
    def test_missing_key_fails_without_network(self, post):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}), self.assertRaisesRegex(GeminiError, "GEMINI_API_KEY"):
            self.generate(Path("unused.mp3"))
        post.assert_not_called()
