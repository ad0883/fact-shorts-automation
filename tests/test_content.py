"""Content and optional image regressions; all network calls are mocked."""

import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

from pipeline import fetch_images as images
from pipeline import gemini_client as client
from pipeline.generate_fact import generate_fact, slugify, validate_fact


class Response:
    def __init__(self, payload=None, status=200, headers=None, body=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(payload).encode()
        self.closed = False

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def gemini_response(value):
    return Response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}]})


def script():
    return {
        "title": "A curious ocean fact",
        "description": "A short educational story about the ocean.",
        "fact_text": "Water covers much of our planet and scientists study its remarkable inhabitants every day.",
        "tags": ["science", "ocean", "nature", "research", "education"],
        "sources": [{"title": "Ocean research", "url": "https://ocean.si.edu/ocean-life", "claim": "The ocean supports diverse life."}],
    }


def with_storyboard(raw):
    tokens = raw["fact_text"].split()
    raw["storyboard"] = [{"role": role, "narration": " ".join(tokens[i*len(tokens)//4:(i+1)*len(tokens)//4]),
                         "visual": {"kind": "question", "headline": "A fact to explore", "query": "", "labels": []}}
                        for i, role in enumerate(("hook", "setup", "explain", "reveal"))]
    return raw


class ScriptTests(unittest.TestCase):
    @patch("pipeline.gemini_client.generate_json")
    def test_generation_assembles_narration_from_storyboard_without_duplication(self, generate):
        raw = with_storyboard(script())
        expected = raw.pop("fact_text")
        raw["storyboard"][0]["narration"] += "\n "
        before = copy.deepcopy(raw)
        generate.return_value = raw
        result = generate_fact("ocean")
        self.assertEqual(result["fact_text"], expected)
        self.assertEqual(" ".join(b["narration"] for b in result["storyboard"]), expected)
        self.assertEqual(raw, before)
        generate.assert_called_once()
        schema = generate.call_args.kwargs["schema"]
        self.assertNotIn("fact_text", schema["properties"])
        self.assertNotIn("fact_text", schema["required"])

    @patch("pipeline.gemini_client.generate_json")
    def test_malformed_generated_beats_receive_bounded_correction(self, generate):
        for beats in (None, [], [None], [{"narration": 42}]):
            with self.subTest(beats=beats):
                raw = script()
                del raw["fact_text"]
                raw["storyboard"] = beats
                generate.reset_mock()
                generate.return_value = raw
                with self.assertRaisesRegex(ValueError, "after one correction attempt"):
                    generate_fact("ocean")
                self.assertEqual(generate.call_count, 2)

    def test_imported_narration_mismatch_is_still_rejected(self):
        raw = with_storyboard(script())
        raw["storyboard"][1]["narration"] += " altered"
        with self.assertRaisesRegex(ValueError, "reproduce fact_text exactly"):
            validate_fact(raw)

    def test_normalizes_without_mutating_or_self_verifying(self):
        raw = script()
        raw.update(slug="../../bad", review_status="verified")
        before = copy.deepcopy(raw)
        result = validate_fact(raw, "ocean")
        self.assertEqual(raw, before)
        self.assertEqual(result["slug"], "a-curious-ocean-fact")
        self.assertEqual(result["review_status"], "needs_review")
        self.assertEqual(result["sources_status"], "provided_unverified")
        self.assertEqual(result["category"], "ocean")

    def test_missing_sources_stay_visible(self):
        raw = script()
        del raw["sources"]
        self.assertEqual(validate_fact(raw)["sources_status"], "missing")

    def test_invalid_fields_are_rejected(self):
        for key, value in [("title", 42), ("tags", "science"), ("tags", [None]),
                           ("fact_text", "tiny"), ("description", ""), ("sources", ["fake"]),
                           ("sources", [{"title": "Source", "url": "https://example.org"}])]:
            with self.subTest(key=key, value=value):
                raw = script()
                raw[key] = value
                with self.assertRaises(ValueError):
                    validate_fact(raw)

    def test_invalid_source_links_are_rejected(self):
        for url in ["file:///etc/passwd", "https://127.0.0.1/secret", "https://user:pass@example.org", "https://foo.local/x", "javascript:alert(1)"]:
            with self.subTest(url=url):
                raw = script()
                raw["sources"][0]["url"] = url
                with self.assertRaises(ValueError):
                    validate_fact(raw)

    def test_slug_is_bounded_and_nonempty_for_non_ascii_titles(self):
        for title in ["../../", "海洋", "", "A " * 200]:
            slug = slugify(title)
            self.assertRegex(slug, r"^[a-z0-9][a-z0-9-]*$")
            self.assertLessEqual(len(slug), 50)
        self.assertEqual(slugify("Café étoile"), "cafe-etoile")

    @patch("pipeline.gemini_client.generate_json")
    def test_generated_sources_are_explicitly_unverified(self, generate):
        raw = script()
        raw["fact_text"] = " ".join(["Narration"] * 130)
        generate.return_value = with_storyboard(raw)
        result = generate_fact("ocean")
        self.assertEqual(result["sources_status"], "model_supplied_unverified")
        self.assertEqual(result["review_status"], "needs_review")
        self.assertEqual(result["generation"]["provider"], "gemini")

    @patch("pipeline.gemini_client.generate_json")
    def test_missing_or_blank_source_claim_gets_one_validated_correction(self, generate):
        for missing in (True, False):
            with self.subTest(missing=missing):
                raw = script()
                if missing:
                    del raw["sources"][0]["claim"]
                else:
                    raw["sources"][0]["claim"] = "  "
                before = copy.deepcopy(raw)
                generate.reset_mock()
                generate.side_effect = [raw, with_storyboard(script())]
                result = generate_fact("ocean")
                self.assertEqual(generate.call_count, 2)
                self.assertIn("source.claim", generate.call_args.args[0])
                self.assertEqual(raw, before)
                self.assertEqual(result["sources"][0]["claim"], script()["sources"][0]["claim"])
                self.assertEqual(result["sources_status"], "model_supplied_unverified")
                self.assertEqual(result["review_status"], "needs_review")

    @patch("pipeline.gemini_client.generate_json")
    def test_invalid_correction_is_rejected_without_an_unbounded_retry(self, generate):
        raw = script()
        del raw["sources"][0]["claim"]
        generate.return_value = raw
        with self.assertRaisesRegex(ValueError, "after one correction attempt: source.claim"):
            generate_fact("ocean")
        self.assertEqual(generate.call_count, 2)

    @patch("pipeline.gemini_client.generate_json", side_effect=client.GeminiError("HTTP 400"))
    def test_provider_errors_do_not_trigger_script_correction(self, generate):
        with self.assertRaises(client.GeminiError):
            generate_fact("ocean")
        generate.assert_called_once()

    @patch("pipeline.gemini_client.generate_json")
    def test_generation_requires_sources_but_treats_word_target_as_review_note(self, generate):
        raw = with_storyboard(script())
        generate.return_value = raw
        result = generate_fact("ocean")
        self.assertIn("80-105", result["quality_warnings"][0])
        self.assertEqual(validate_fact(result)["quality_warnings"], result["quality_warnings"])
        raw["sources"] = []
        with self.assertRaisesRegex(ValueError, "no supporting sources"):
            generate_fact("ocean")

    @patch("pipeline.gemini_client.generate_json")
    def test_editorial_notes_recompute_after_script_edit_but_hard_limits_remain(self, generate):
        raw = script()
        raw["fact_text"] = " ".join(["Narration"] * 191)
        raw["title"] = "A" * 71
        generate.return_value = with_storyboard(raw)
        result = generate_fact("ocean")
        self.assertTrue(any("191 words" in note for note in result["quality_warnings"]))
        self.assertTrue(any("71 characters" in note for note in result["quality_warnings"]))
        result["title"] = "A short title"
        result["fact_text"] = " ".join(["A short sentence."] * 30)
        with_storyboard(result)
        notes = validate_fact(result)["quality_warnings"]
        self.assertFalse(any("191 words" in note or "71 characters" in note for note in notes))
        self.assertTrue(any("visual holds" in note for note in notes))
        raw["fact_text"] = " ".join(["Narration"] * 401)
        with self.assertRaisesRegex(ValueError, "10-400"):
            generate_fact("ocean")

    def test_validation_preserves_generation_provenance(self):
        raw = script()
        raw["generation"] = {"provider": "gemini", "model": "test-model", "created_at": "2026-10-15T00:00:00+00:00", "unknown": "dropped"}
        result = validate_fact(raw)
        self.assertEqual(result["generation"]["model"], "test-model")
        self.assertNotIn("unknown", result["generation"])
        self.assertEqual(result["sources_status"], "model_supplied_unverified")
        self.assertEqual(result["review_status"], "needs_review")
        raw["generation"]["created_at"] = "not a timestamp"
        with self.assertRaisesRegex(ValueError, "ISO timestamp"):
            validate_fact(raw)

    def test_local_script_validation_works_without_site_packages(self):
        result = subprocess.run(
            [sys.executable, "-S", "-c", "from pipeline.generate_fact import validate_fact; import json,sys; validate_fact(json.loads(sys.argv[1]))", json.dumps(script())],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True)
class GeminiTests(unittest.TestCase):
    @patch("pipeline.gemini_client.GEMINI_MODEL", "gemini-3.8-flash")
    @patch("pipeline.gemini_client.requests.post")
    def test_api_auth_and_schema_and_response(self, post):
        response = gemini_response({"answer": 42})
        post.return_value = response
        self.assertEqual(client.generate_json("hello", schema={"type": "object"}), {"answer": 42})
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertFalse(kwargs["allow_redirects"])
        # The REST TextResponseFormat contract requires this enum; a MIME string
        # produced HTTP 400 in production even with a valid key and model.
        self.assertEqual(kwargs["json"]["generationConfig"]["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")
        self.assertEqual(kwargs["json"]["generationConfig"]["responseFormat"]["text"]["schema"], {"type": "object"})
        self.assertNotIn("test-key", post.call_args.args[0])
        self.assertTrue(response.closed)

    @patch("pipeline.gemini_client.requests.post")
    def test_gemini_2_uses_schema_fields_that_the_model_enforces(self, post):
        schema = {"type": "object", "properties": {"claim": {"type": "string"}}, "required": ["claim"]}
        for model in ("gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite", "models/gemini-2.5-pro"):
            with self.subTest(model=model), patch.object(client, "GEMINI_MODEL", model):
                post.return_value = gemini_response({"claim": "A reference to review"})
                self.assertEqual(client.generate_json("hello", schema=schema), {"claim": "A reference to review"})
                config = post.call_args.kwargs["json"]["generationConfig"]
                self.assertEqual(config["responseMimeType"], "application/json")
                self.assertEqual(config["responseJsonSchema"], schema)
                self.assertNotIn("responseFormat", config)
                self.assertNotIn("responseSchema", config)
                self.assertNotIn("models/models/", post.call_args.args[0])

    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post")
    def test_transient_error_retries_same_model(self, post, sleep):
        first = Response(status=429, headers={"Retry-After": "2"})
        post.side_effect = [first, gemini_response(["ocean"])]
        self.assertEqual(client.generate_json("hello", schema={"type": "array"}), ["ocean"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], post.call_args_list[1].args[0])
        sleep.assert_called_once_with(2.0)
        self.assertTrue(first.closed)

    @patch("pipeline.gemini_client.random.uniform", return_value=0)
    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post")
    def test_overload_retries_report_progress_and_release_connections(self, post, sleep, jitter):
        responses = [Response(status=503), Response(status=503), gemini_response({"ok": True})]
        post.side_effect = responses
        sleep.side_effect = lambda delay: self.assertTrue(responses[post.call_count - 1].closed)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(client.generate_json("hello", schema={}), {"ok": True})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 10.0])
        self.assertIn("HTTP 503; retry 2/3", output.getvalue())
        self.assertIn("retry 3/3", output.getvalue())
        self.assertNotIn("test-key", output.getvalue())

    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post")
    def test_long_provider_wait_is_not_shortened_or_slept_through(self, post, sleep):
        response = Response(status=503, headers={"Retry-After": "3600"})
        post.return_value = response
        with self.assertRaisesRegex(client.GeminiError, "wait of 3600 seconds"):
            client.generate_json("hello", schema={})
        post.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(response.closed)

    @patch("pipeline.gemini_client.time.time", return_value=0)
    @patch("pipeline.gemini_client.random.uniform", return_value=0)
    def test_retry_after_date_and_invalid_values(self, jitter, now):
        self.assertEqual(client._retry_delay(Response(headers={"Retry-After": "Thu, 01 Jan 1970 00:00:45 GMT"}), 0), 45)
        for value in ("", "invalid", "NaN", "Infinity"):
            with self.subTest(value=value):
                self.assertEqual(client._retry_delay(Response(headers={"Retry-After": value}), 1), 10)

    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post")
    def test_terminal_overload_and_quota_errors_are_distinct(self, post, sleep):
        for status, message in ((503, "service unavailable"), (429, "quota or rate limit reached")):
            with self.subTest(status=status):
                post.side_effect = [Response(status=status) for _ in range(client.MAX_ATTEMPTS)]
                with self.assertRaisesRegex(client.GeminiError, message) as raised:
                    client.generate_json("hello", schema={})
                self.assertIn(client.GEMINI_MODEL.removeprefix("models/"), str(raised.exception))

    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post", side_effect=requests.Timeout("test-key"))
    def test_network_retries_are_bounded_and_redacted(self, post, sleep):
        with self.assertRaises(client.GeminiError) as raised:
            client.generate_json("hello", schema={})
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("test-key", str(raised.exception))

    @patch("pipeline.gemini_client.requests.post", return_value=Response(status=404))
    def test_unavailable_model_does_not_silently_switch(self, post):
        with self.assertRaisesRegex(client.GeminiError, "GEMINI_MODEL"):
            client.generate_json("hello", schema={})
        post.assert_called_once()

    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post")
    def test_bad_request_reports_provider_diagnostic_without_retrying_or_leaking_key(self, post, sleep):
        response = Response({"error": {"code": 400, "message":
            'Invalid value at generation_config.response_format.text.mime_type: application/json. '
            'Credential test-key; URL https://example.test/?key=another-secret&x=1'}}, status=400)
        post.return_value = response
        with self.assertRaises(client.GeminiError) as raised:
            client.generate_json("hello", schema={})
        diagnostic = str(raised.exception)
        self.assertIn("mime_type", diagnostic)
        self.assertNotIn("test-key", diagnostic)
        self.assertNotIn("another-secret", diagnostic)
        self.assertIn("[REDACTED]", diagnostic)
        post.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(response.closed)

    @patch("pipeline.gemini_client.requests.post")
    def test_bad_request_handles_non_json_and_oversized_error_bodies(self, post):
        for body in (b"<html>gateway failure</html>", b"x" * (client.MAX_ERROR_BYTES + 1),
                     b'{"error":{"message":123}}'):
            with self.subTest(body_type=body[:20]):
                response = Response(status=400, body=body)
                post.return_value = response
                with self.assertRaisesRegex(client.GeminiError, "HTTP 400") as raised:
                    client.generate_json("hello", schema={})
                self.assertLess(len(str(raised.exception)), 200)
                self.assertTrue(response.closed)

    @patch("pipeline.gemini_client.time.sleep")
    @patch("pipeline.gemini_client.requests.post")
    def test_exhausted_service_retries_preserve_provider_reason(self, post, sleep):
        responses = [Response({"error": {"message": "This model is currently experiencing high demand."}}, status=503)
                     for _ in range(client.MAX_ATTEMPTS)]
        post.side_effect = responses
        with self.assertRaisesRegex(client.GeminiError, "high demand"):
            client.generate_json("hello", schema={})
        self.assertEqual(post.call_count, client.MAX_ATTEMPTS)
        self.assertTrue(all(response.closed for response in responses))

    @patch("pipeline.gemini_client.requests.post")
    def test_empty_api_key_fails_before_network(self, post):
        with patch.dict(os.environ, {"GEMINI_API_KEY": " "}):
            with self.assertRaisesRegex(client.GeminiError, "GEMINI_API_KEY"):
                client.generate_json("hello", schema={})
        post.assert_not_called()

    @patch("pipeline.gemini_client.requests.post")
    def test_rejects_incomplete_malformed_and_oversized_responses(self, post):
        cases = [Response({"candidates": []}), Response({"candidates": [{"finishReason": "MAX_TOKENS"}]}),
                 Response(body=b"not json"), Response(body=b"x" * (client.MAX_RESPONSE_BYTES + 1))]
        for response in cases:
            with self.subTest(response=response):
                post.return_value = response
                with self.assertRaises(client.GeminiError):
                    client.generate_json("hello", schema={})
                self.assertTrue(response.closed)

    def test_ignores_thought_text(self):
        payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"thought": True, "text": "reasoning"}, {"text": '{"ok":true}'}]}}]}
        self.assertEqual(client._extract_json(payload), {"ok": True})


class TimingAndQueryTests(unittest.TestCase):
    def test_preserves_short_sentences_and_titles(self):
        self.assertEqual(images.split_into_sentences("Stop! Dr. Smith studied 3.5 samples. Why?"),
                         ["Stop!", "Dr. Smith studied 3.5 samples.", "Why?"])

    def test_repeated_words_and_omissions_do_not_rewind_timing(self):
        sentences = ["The ocean is blue.", "The ocean is very deep.", "Ocean life adapts."]
        spoken = "The ocean blue The ocean is deep Ocean life adapts".split()
        words = [{"word": word, "start": index * .4, "end": (index + 1) * .4} for index, word in enumerate(spoken)]
        segments = images.map_sentences_to_timestamps(sentences, words)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0]["start"], 0)
        self.assertAlmostEqual(segments[-1]["end"], 4)
        self.assertEqual(segments[0]["alignment"], "estimated")
        for left, right in zip(segments, segments[1:]):
            self.assertLessEqual(left["end"], right["start"])

    def test_invalid_timestamps_do_not_propagate_nan(self):
        words = [{"word": "ocean", "start": float("nan"), "end": 1},
                 {"word": "ocean", "start": 0, "end": 1}]
        self.assertEqual(images.map_sentences_to_timestamps(["Ocean."], words)[0]["end"], 1)

    @patch.dict(os.environ, {}, clear=True)
    @patch("pipeline.fetch_images.generate_json")
    def test_local_queries_need_no_gemini_key(self, generate):
        self.assertEqual(images.generate_search_queries(["The ocean is blue."], "science"), ["ocean blue"])
        generate.assert_not_called()

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True)
    @patch("pipeline.fetch_images.generate_json", return_value=["one only"])
    def test_bad_query_count_gets_complete_fallback(self, generate):
        queries = images.generate_search_queries(["The ocean is blue.", "Birds fly."], "nature")
        self.assertEqual(len(queries), 2)
        self.assertTrue(all(isinstance(query, str) and query for query in queries))

    @patch.dict(os.environ, {}, clear=True)
    @patch("pipeline.fetch_images.generate_search_queries")
    @patch("pipeline.fetch_images.requests.get")
    def test_missing_pexels_key_skips_all_optional_network(self, get, queries):
        self.assertEqual(images.fetch_images_for_fact("Ocean life.", "nature", [], Path("unused")), [])
        queries.assert_not_called()
        get.assert_not_called()


@patch.dict(os.environ, {"PEXELS_API_KEY": "pexels-secret"}, clear=True)
class DownloadTests(unittest.TestCase):
    def photo(self, url="https://images.pexels.com/photos/42/photo.jpeg"):
        return {"photos": [{"id": 42, "alt": "Blue ocean waves", "src": {"large": url}, "url": "https://www.pexels.com/photo/42/",
                            "photographer": "A Photographer", "photographer_url": "https://www.pexels.com/@photographer"}]}

    @patch("pipeline.fetch_images.requests.get")
    def test_download_is_atomic_bounded_and_keeps_attribution(self, get):
        blob = b"\xff\xd8\xff" + b"image bytes"
        get.side_effect = [Response(self.photo()), Response(body=blob, headers={"Content-Type": "image/jpeg", "Content-Length": str(len(blob))})]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.jpg"
            metadata = {}
            self.assertTrue(images.download_pexels_image("ocean", path, metadata=metadata))
            self.assertEqual(path.read_bytes(), blob)
            self.assertEqual(metadata["photographer"], "A Photographer")
            self.assertEqual(metadata["usage"], "illustrative_stock_image")
            self.assertEqual(list(Path(folder).glob("*.part")), [])
        self.assertNotIn("headers", get.call_args_list[1].kwargs)
        self.assertTrue(get.call_args_list[1].kwargs["stream"])
        self.assertFalse(get.call_args_list[1].kwargs["allow_redirects"])

    @patch("pipeline.fetch_images.requests.get")
    def test_untrusted_assets_never_get_requested(self, get):
        for url in ["http://images.pexels.com/photos/1/x.jpg", "https://localhost/x.jpg", "https://images.pexels.com.evil.org/photos/x", "https://user:pass@images.pexels.com/photos/x"]:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as folder:
                get.reset_mock()
                get.return_value = Response(self.photo(url))
                self.assertFalse(images.download_pexels_image("ocean", Path(folder) / "photo.jpg"))
                get.assert_called_once()

    @patch("pipeline.fetch_images.requests.get")
    def test_oversized_download_does_not_replace_existing_image(self, get):
        get.side_effect = [Response(self.photo()), Response(body=b"\xff\xd8\xff" + b"x" * 100, headers={"Content-Type": "image/jpeg"})]
        with tempfile.TemporaryDirectory() as folder, patch.object(images, "MAX_IMAGE_BYTES", 50):
            path = Path(folder) / "photo.jpg"
            path.write_bytes(b"existing")
            self.assertFalse(images.download_pexels_image("ocean", path))
            self.assertEqual(path.read_bytes(), b"existing")
            self.assertEqual([entry.name for entry in Path(folder).iterdir()], ["photo.jpg"])

    @patch("pipeline.fetch_images.requests.get")
    def test_html_and_redirect_responses_are_not_saved_as_images(self, get):
        for response in [Response(status=302), Response(body=b"<html>error</html>", headers={"Content-Type": "image/jpeg"})]:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as folder:
                get.side_effect = [Response(self.photo()), response]
                path = Path(folder) / "photo.jpg"
                self.assertFalse(images.download_pexels_image("ocean", path))
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
