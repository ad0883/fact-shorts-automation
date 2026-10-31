"""Archive fallback failures, provenance, and exact subject selection."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import commons_images as commons
from pipeline.fetch_images import fetch_images_for_fact
from test_content import Response


def archive_payload(title="File:Antikythera mechanism.jpg", license_code="cc-by-4.0"):
    return {"query": {"pages": {"1": {"pageid": 1, "title": title, "imageinfo": [{
        "thumburl": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Antikythera.jpg/1280px-Antikythera.jpg",
        "extmetadata": {"License": {"value": license_code}, "LicenseShortName": {"value": "CC BY 4.0"},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                        "Artist": {"value": '<a href="/author">A &amp; B</a>'},
                        "ImageDescription": {"value": "A photo of the Antikythera mechanism."}}}]}}}}


class CommonsTests(unittest.TestCase):
    @patch("pipeline.visual_assets.plan_scenes", return_value=[
        {"query": "cold sea water", "headline": "A long wait", "subject_query": "deep sea octopus"}])
    @patch("pipeline.commons_images.search_commons_images", return_value=[
        {"alt": "Graneledone boreopacifica", "provider": "commons"}])
    @patch("pipeline.commons_images.download_commons_image", return_value=True)
    @patch("pipeline.fetch_images.download_pexels_image")
    def test_named_species_does_not_use_generic_stock_even_if_archive_fails(self, stock, download, search, plan):
        narration = "The species Graneledone boreopacifica broods eggs."
        words = [{"word": word, "start": i, "end": i + 1} for i, word in enumerate(narration.split())]
        for candidates in ([{"alt": "Graneledone boreopacifica", "provider": "commons"}], []):
            with self.subTest(archive_available=bool(candidates)):
                search.reset_mock()
                search.return_value = candidates
                result = fetch_images_for_fact(narration, "marine biology", words, Path("unused"))
                self.assertEqual(bool(result), bool(candidates))
                search.assert_called_once_with("Graneledone boreopacifica")
        stock.assert_not_called()

    @patch("pipeline.commons_images.requests.get")
    def test_archive_search_keeps_plain_attribution_and_license(self, get):
        get.return_value = Response(archive_payload())
        candidates = commons.search_commons_images("Antikythera mechanism")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["photographer"], "A & B")
        self.assertEqual(candidates[0]["license"], "CC BY 4.0")
        self.assertTrue(candidates[0]["photo_url"].startswith("https://commons.wikimedia.org/wiki/"))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    @patch("pipeline.commons_images.requests.get")
    def test_fragment_caption_can_identify_exact_object(self, get):
        get.return_value = Response(archive_payload("File:Antikythera Fragment A.webp"))
        self.assertEqual(len(commons.search_commons_images("Antikythera mechanism")), 1)

    @patch("pipeline.commons_images.requests.get")
    def test_unrelated_and_unsupported_assets_are_not_accepted(self, get):
        for title, license_code in [("File:Wikidata Antikythera mechanism.png", "cc-by-4.0"),
                                    ("File:Antikythera mechanism.svg", "cc-by-4.0"),
                                    ("File:Antikythera mechanism.jpg", "unknown"),
                                    ("File:Generic gears.jpg", "cc-by-4.0")]:
            with self.subTest(title=title, license=license_code):
                get.return_value = Response(archive_payload(title, license_code))
                self.assertEqual(commons.search_commons_images("Antikythera mechanism"), [])

    @patch("pipeline.commons_images.requests.get")
    def test_search_response_is_bounded(self, get):
        get.return_value = Response(body=b"x" * 100)
        with patch.object(commons, "MAX_METADATA_BYTES", 50):
            self.assertEqual(commons.search_commons_images("Antikythera mechanism"), [])

    @patch("pipeline.commons_images.requests.get")
    def test_download_is_atomic_and_rejects_untrusted_hosts(self, get):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "image.jpg"
            out.write_bytes(b"original")
            self.assertFalse(commons.download_commons_image({"asset_url": "https://localhost/image.jpg"}, out))
            get.assert_not_called()
            candidate = {"asset_url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/photo.jpg"}
            get.return_value = Response(body=b"\xff\xd8\xffpartial", headers={"Content-Type": "image/jpeg", "Content-Length": "50"})
            self.assertFalse(commons.download_commons_image(candidate, out))
            self.assertEqual(out.read_bytes(), b"original")
            get.return_value = Response(body=b"\xff\xd8\xffvalid", headers={"Content-Type": "image/jpeg"})
            self.assertTrue(commons.download_commons_image(candidate, out))
            self.assertEqual(out.read_bytes(), b"\xff\xd8\xffvalid")
            self.assertEqual(list(out.parent.glob("*.part")), [])
            self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    @patch.dict(os.environ, {}, clear=True)
    @patch("pipeline.visual_assets.plan_scenes", return_value=[
        {"query": "Greek gears", "headline": "Ancient computer", "subject_query": "Antikythera mechanism"},
        {"query": "Bronze gear", "headline": "A discovery", "subject_query": "Antikythera mechanism"}])
    @patch("pipeline.commons_images.search_commons_images", return_value=[{"alt": "Antikythera mechanism", "provider": "commons"},
                                                                        {"alt": "Antikythera fragment", "provider": "commons"}])
    @patch("pipeline.commons_images.download_commons_image", return_value=True)
    @patch("pipeline.fetch_images.download_pexels_image")
    def test_archaeology_gets_multiple_archive_images_without_stock_key(self, stock, download, search, plan):
        report = {}
        result = fetch_images_for_fact("Ancient gears. Bronze mechanism.", "archaeology", [
            {"word": "Ancient", "start": 0, "end": .5}, {"word": "gears.", "start": .5, "end": 1},
            {"word": "Bronze", "start": 1.4, "end": 1.8}, {"word": "mechanism.", "start": 1.8, "end": 2}],
            Path("unused"), report=report)
        self.assertEqual(report["selected_scenes"], 2)
        self.assertEqual(report["unique_images"], 2)
        self.assertEqual(result[0]["end"], result[1]["start"])
        search.assert_called_once_with("Antikythera mechanism")
        stock.assert_not_called()

    @patch("pipeline.visual_assets.plan_scenes", return_value=[
        {"query": "Graneledone boreopacifica", "headline": "Brooding", "subject_query": "deep sea octopus"},
        {"query": "Graneledone boreopacifica", "headline": "Growing", "subject_query": "deep sea octopus"}])
    @patch("pipeline.commons_images.search_commons_images", side_effect=[[], [
        {"alt": "Graneledone boreopacifica", "provider": "commons", "license": "Public domain"}]])
    @patch("pipeline.commons_images.download_commons_image", return_value=True)
    @patch("pipeline.fetch_images.download_pexels_image", return_value=False)
    def test_empty_shared_subject_does_not_hide_specific_scene_images(self, stock, download, search, plan):
        report = {}
        result = fetch_images_for_fact("Octopus broods. Eggs grow.", "marine biology", [
            {"word": "Octopus", "start": 0, "end": .5}, {"word": "broods.", "start": .5, "end": 1},
            {"word": "Eggs", "start": 1, "end": 1.5}, {"word": "grow.", "start": 1.5, "end": 2}],
            Path("unused"), report=report)
        self.assertEqual(len(result), 2)
        self.assertEqual([call.args[0] for call in search.call_args_list], ["deep sea octopus", "Graneledone boreopacifica"])
        download.assert_called_once()
        self.assertEqual(result[0]["query"], "Graneledone boreopacifica")
        self.assertEqual(result[0]["provenance"]["license"], "Public domain")
        self.assertEqual(report["archive_searches"], [
            {"query": "deep sea octopus", "candidates": 0, "downloaded": 0},
            {"query": "Graneledone boreopacifica", "candidates": 1, "downloaded": 1}])

    @patch("pipeline.visual_assets.plan_scenes", return_value=[
        {"query": "Graneledone boreopacifica", "headline": "Brooding", "subject_query": "Graneledone boreopacifica"}])
    @patch("pipeline.commons_images.search_commons_images")
    @patch("pipeline.commons_images.download_commons_image", side_effect=[False, True, True])
    @patch("pipeline.fetch_images.download_pexels_image", return_value=False)
    def test_smaller_previews_recover_photos_without_repeating_failed_urls(self, stock, download, search, plan):
        original = {"photo_id": 1, "asset_url": "original", "alt": "First octopus"}
        available = {"photo_id": 2, "asset_url": "available", "alt": "Second octopus"}
        preview = {**original, "asset_url": "smaller-preview"}
        search.side_effect = [[original, available], [original, available, preview]]
        report = {}
        result = fetch_images_for_fact("Octopus broods.", "marine biology", [
            {"word": "Octopus", "start": 0, "end": .5}, {"word": "broods.", "start": .5, "end": 1}],
            Path("unused"), report=report)
        self.assertEqual(len(result), 1)
        self.assertEqual([call.args[0]["asset_url"] for call in download.call_args_list],
                         ["original", "available", "smaller-preview"])
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args.kwargs, {"thumbnail_width": 640})
        self.assertEqual(report["archive_searches"][0]["downloaded"], 2)
        self.assertTrue(report["archive_searches"][0]["smaller_thumbnails_requested"])


if __name__ == "__main__":
    unittest.main()
