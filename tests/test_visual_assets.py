"""Storyboard selection, downloads, and reproducible local scene inputs."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import visual_assets as visuals
from pipeline import fetch_images
from test_content import Response


class SelectionTests(unittest.TestCase):
    @patch("pipeline.visual_assets.plan_scenes", return_value=[
        {"query": "ocean submarine", "headline": "", "subject_query": ""}])
    @patch("pipeline.commons_images.search_commons_images", return_value=[])
    @patch("pipeline.stock_video.download_pexels_video", return_value=False)
    @patch("pipeline.fetch_images.download_pexels_image", side_effect=[False, True])
    def test_missing_image_gets_one_alternative_search(self, photo, video, archive, plans):
        beat = {"narration": "A submarine explores the ocean.", "start": 0, "end": 3,
                "beat_index": 0, "alignment": "estimated",
                "visual": {"kind": "photo", "query": "titanium submarine hull", "headline": "Ocean"}}
        report = {}
        result = fetch_images.fetch_images_for_fact(
            beat["narration"], "ocean", [], Path("unused"), storyboard=[beat], report=report)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["query"], "ocean submarine")
        self.assertEqual(result[0]["requested_query"], "titanium submarine hull")
        self.assertEqual(result[0]["beat_index"], 0)
        self.assertEqual(photo.call_count, 2)
        self.assertEqual(photo.call_args.kwargs["orientation"], "")
        plans.assert_called_once()
        self.assertEqual(report["alternative_searches"], 1)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=True)
    @patch("pipeline.visual_assets.generate_json", side_effect=visuals.GeminiError("temporary provider error"))
    def test_storyboard_failure_preserves_named_subject(self, generate):
        plans = visuals.plan_scenes(["Ancient bronze gears.", "Could it predict eclipses?"], "archaeology",
                                   subject_hint="The Antikythera Mechanism: An ancient computer")
        self.assertEqual([p["query"] for p in plans], ["Antikythera Mechanism"] * 2)
        self.assertEqual(plans[0]["subject_query"], "Antikythera Mechanism")

    def test_explicit_subject_can_be_recovered_from_narration(self):
        self.assertEqual(visuals.subject_from_title("Ancient gears", ["Known as the Antikythera Mechanism, it calculated cycles."]), "Antikythera Mechanism")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=True)
    @patch("pipeline.visual_assets.generate_json", return_value=[
        {"query": "deep sea octopus", "headline": "Years of brooding", "subject_query": "deep sea octopus"}])
    def test_explicit_scientific_name_overrides_broad_archive_subject(self, generate):
        narration = "Researchers studying the species Graneledone boreopacifica watched her brood eggs."
        plans = visuals.plan_scenes([narration], "marine biology", subject_hint="Why do octopuses brood for years?")
        self.assertEqual(plans[0]["subject_query"], "Graneledone boreopacifica")
        self.assertEqual(visuals.subject_from_title("Octopuses", ["Many species brood their eggs."]), "")
        self.assertEqual(visuals.subject_from_title("Octopuses", ["The species has unusual habits."]), "")

    def test_rejects_weak_or_unlabeled_stock_matches(self):
        for query, photo in [
            ("heliopause boundary solar", {"alt": "Solar garden light"}),
            ("solar wind", {"alt": "Solar garden light"}),
            ("Vast empty space", {"alt": "A vast desert with an empty road"}),
            ("Voyager spacecraft", {"alt": "Apollo mission control"}),
            ("ocean", {"src": {"large": "image.jpg"}}),
        ]:
            with self.subTest(query=query):
                self.assertIsNone(visuals.choose_relevant_photo(query, [photo]))

    def test_prefers_subject_match_over_provider_order(self):
        wrong = {"alt": "Sunlight over a garden"}
        right = {"alt": "Voyager spacecraft at the edge of the heliosphere"}
        self.assertIs(visuals.choose_relevant_photo("Voyager spacecraft", [wrong, right]), right)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=True)
    @patch("pipeline.visual_assets.generate_json", return_value=[{"query": "Voyager spacecraft", "headline": "An invisible border"}])
    def test_storyboard_preserves_passage_count_and_headline(self, generate):
        self.assertEqual(visuals.plan_scenes(["Voyager crossed a border."], "space")[0]["headline"], "An invisible border")
        self.assertEqual(len(visuals.plan_scenes(["One passage.", "Second passage."], "space")), 2)

    @patch.dict(os.environ, {}, clear=True)
    @patch("pipeline.visual_assets.plan_scenes", return_value=[{"query": "Voyager spacecraft", "headline": "The probe"}, {"query": "Voyager spacecraft", "headline": "The journey"}])
    @patch("pipeline.visual_assets.download_nasa_image", return_value=True)
    @patch("pipeline.fetch_images.download_pexels_image")
    def test_space_images_need_no_stock_key_and_bridge_short_pauses(self, pexels, nasa, plans):
        words = [{"word": "Voyager", "start": .2, "end": .5}, {"word": "flies.", "start": .5, "end": 1},
                 {"word": "Voyager", "start": 1.5, "end": 1.8}, {"word": "travels.", "start": 1.8, "end": 2}]
        result = fetch_images.fetch_images_for_fact("Voyager flies. Voyager travels.", "astronomy", words, Path("unused"))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["start"], 0)
        self.assertEqual(result[0]["end"], result[1]["start"])
        nasa.assert_called_once()
        pexels.assert_not_called()

    @patch("pipeline.visual_assets.plan_scenes", return_value=[{"query": "Invisible border", "headline": "An invisible border"}])
    @patch("pipeline.visual_assets.download_nasa_image", side_effect=[False, True])
    @patch("pipeline.fetch_images.download_pexels_image")
    def test_space_search_failure_reuses_named_subject_without_unrelated_stock(self, pexels, nasa, plans):
        words = [{"word": "Voyager.", "start": 0, "end": 1}]
        result = fetch_images.fetch_images_for_fact("Voyager.", "astronomy", words, Path("unused"))
        self.assertEqual(len(result), 1)
        self.assertEqual(nasa.call_args_list[1].args[0], "Voyager")
        self.assertEqual(result[0]["query"], "Voyager")
        self.assertEqual(result[0]["requested_query"], "Invisible border")
        pexels.assert_not_called()


class NasaTests(unittest.TestCase):
    def payload(self, asset="https://images-assets.nasa.gov/image/PIA1/PIA1~large.jpg"):
        return {"collection": {"items": [{"data": [{"nasa_id": "PIA1", "title": "Voyager spacecraft", "description": "An artist concept of Voyager."}],
                                           "links": [{"href": asset}]}]}}

    @patch("pipeline.visual_assets.requests.get")
    def test_incidental_description_reference_is_not_a_subject_match(self, get):
        payload = self.payload()
        payload["collection"]["items"][0]["data"][0].update(
            title="First Official Pluto Feature Names", description="Voyager crossing detection is mentioned in the mission history.")
        get.return_value = Response(payload)
        self.assertFalse(visuals.download_nasa_image("Voyager crossing detection", Path("unused")))
        get.assert_called_once()

    @patch("pipeline.visual_assets.requests.get")
    def test_download_keeps_attribution_and_illustration_label(self, get):
        blob = b"\xff\xd8\xffimage"
        get.side_effect = [Response(self.payload()), Response(body=blob, headers={"Content-Type": "image/jpeg", "Content-Length": str(len(blob))})]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "image.jpg"
            metadata = {}
            self.assertTrue(visuals.download_nasa_image("Voyager spacecraft", out, metadata=metadata))
            self.assertEqual(out.read_bytes(), blob)
            self.assertEqual(metadata["usage"], "artist_concept")
            self.assertEqual(metadata["photo_url"], "https://images.nasa.gov/details/PIA1")
        self.assertNotIn("headers", get.call_args_list[1].kwargs)
        self.assertFalse(get.call_args_list[1].kwargs["allow_redirects"])

    @patch("pipeline.visual_assets.requests.get")
    def test_untrusted_image_host_is_never_requested(self, get):
        get.return_value = Response(self.payload("https://images-assets.nasa.gov.evil.test/image/1.jpg"))
        self.assertFalse(visuals.download_nasa_image("Voyager spacecraft", Path("unused")))
        get.assert_called_once()

    @patch("pipeline.visual_assets.requests.get")
    def test_partial_image_preserves_existing_file(self, get):
        get.side_effect = [Response(self.payload()), Response(body=b"\xff\xd8\xffpartial", headers={"Content-Type": "image/jpeg", "Content-Length": "500"})]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "image.jpg"
            out.write_bytes(b"old image")
            self.assertFalse(visuals.download_nasa_image("Voyager spacecraft", out))
            self.assertEqual(out.read_bytes(), b"old image")
            self.assertEqual(list(Path(directory).glob("*.part")), [])


class LocalPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "image.jpg"
        self.image.write_bytes(b"\xff\xd8\xfffixture")
        self.path = self.root / "plan.json"
        self.scene = {"start": 0, "end": 5, "image_path": "image.jpg", "headline": "A border", "fit": "contain"}
        self.save([self.scene])

    def save(self, scenes):
        self.path.write_text(json.dumps({"scenes": scenes}))

    def test_copies_relative_asset_and_clips_end_to_narration(self):
        self.save([{**self.scene, "start": "0", "end": "5"}])
        option = visuals.visual_plan_option(self.path)
        result = visuals.import_visual_plan(option, self.root / "run", 3)
        self.assertEqual((result[0]["start"], result[0]["end"]), (0, 3))
        self.assertEqual(Path(result[0]["image_path"]).read_bytes(), self.image.read_bytes())
        self.assertEqual(result[0]["headline"], "A border")

    def test_changed_asset_cannot_silently_replace_original_storyboard(self):
        option = visuals.visual_plan_option(self.path)
        self.image.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            visuals.import_visual_plan(option, self.root / "run", 3)

    def test_bad_times_missing_files_and_overlap_fail_early(self):
        for scenes in [[{**self.scene, "start": -1}], [{**self.scene, "end": float("nan")}],
                       [{**self.scene, "image_path": "missing.jpg"}], [self.scene, {**self.scene, "start": 4}]]:
            with self.subTest(scenes=scenes):
                self.save(scenes)
                with self.assertRaisesRegex(ValueError, "Invalid visual scene"):
                    visuals.visual_plan_option(self.path)


if __name__ == "__main__":
    unittest.main()
