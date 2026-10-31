"""Story integrity, missing visual recovery, caption transitions and metric comparisons."""

import copy
from contextlib import ExitStack, redirect_stdout
import io
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main
from pipeline import build_captions as captions
from pipeline import engagement
from pipeline.generate_fact import validate_fact
from pipeline.run_store import read_json, write_json
from pipeline.storyboard import coverage_report, compose_visuals, time_storyboard


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "golf-story.json"


def timed(text, duration=34):
    words = text.split()
    return [{"word": w, "start": i*duration/len(words), "end": (i+1)*duration/len(words)} for i, w in enumerate(words)]


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.fact = validate_fact(read_json(EXAMPLE))

    def test_storyboard_round_trip_and_stale_script_rejection(self):
        self.assertEqual(validate_fact(self.fact)["storyboard"], self.fact["storyboard"])
        edited = copy.deepcopy(self.fact)
        edited["fact_text"] = edited["fact_text"].replace("smoother", "smaller")
        with self.assertRaisesRegex(ValueError, "reproduce fact_text"):
            validate_fact(edited)

    def test_motion_template_roundtrips_and_cannot_be_assigned_to_unrelated_fact(self):
        motion = validate_fact(read_json(EXAMPLE.with_name("golf-motion-story.json")))
        self.assertEqual(motion["storyboard"][0]["visual"]["template"], "golf_airflow")
        self.assertEqual(validate_fact(motion)["storyboard"], motion["storyboard"])
        motion["fact_text"] = "An unrelated ocean fact has no relationship to this visual subject."
        with self.assertRaisesRegex(ValueError, "requires narration about golf"):
            validate_fact(motion)

    def test_animation_produces_actual_moving_frames(self):
        from pipeline.motion_graphics import render_animation
        beat = {"start":0,"end":.4,"beat_index":0,"role":"explain",
                "visual":{"template":"golf_airflow","view":"smooth","headline":"Watch the wake"}}
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(main.settings, VIDEO_WIDTH=360, VIDEO_HEIGHT=640, VIDEO_FPS=15):
            scenes=render_animation(beat,Path(tmp))
            self.assertEqual(scenes[0]["media_type"],"video")
            result=subprocess.run(["ffmpeg","-v","error","-i",scenes[0]["image_path"],"-vf","crop=300:200:20:200", "-f","framemd5","-"],
                                  capture_output=True,text=True,check=True)
            hashes=[line.rsplit(",",1)[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")]
            self.assertGreater(len(set(hashes)),3)

    def test_rejects_malformed_graphics_and_missing_reveal(self):
        for modify in (lambda f: f["storyboard"][-1]["visual"].update(labels=["one"]),
                       lambda f: f["storyboard"][0].update(role="setup"),
                       lambda f: f["storyboard"][2]["visual"].update(labels=["x"*65,"y"]),
                       lambda f: f["storyboard"][1]["visual"].update(query="")):
            raw = copy.deepcopy(self.fact)
            modify(raw)
            with self.assertRaises(ValueError):
                validate_fact(raw)

    def test_alignment_covers_silence_and_keeps_all_beats_when_asr_omits_words(self):
        for words in (timed(self.fact["fact_text"]), [{"word": "golf", "start": 1, "end": 2}]):
            beats = time_storyboard(self.fact, words, 34)
            self.assertEqual(len(beats), 8)
            self.assertEqual(beats[0]["start"], 0)
            self.assertEqual(beats[-1]["end"], 34)
            self.assertEqual(coverage_report(beats, 34)["gaps"], [])
            for a, b in zip(beats, beats[1:]):
                self.assertEqual(a["end"], b["start"])

    def test_coverage_detects_opening_middle_trailing_and_long_hold(self):
        report = coverage_report([{"start": 2, "end": 4}, {"start": 8, "end": 22}], 25)
        self.assertEqual(report["gaps"], [{"start": 0, "end": 2}, {"start": 4, "end": 8}, {"start": 22, "end": 25}])
        self.assertEqual(report["coverage_percent"], 64)
        self.assertEqual(len(report["long_scenes"]), 1)

    def test_missing_photos_leave_gameplay_visible_and_preserve_sequences(self):
        from pipeline.select_background import probe_media
        beats = time_storyboard(self.fact, timed(self.fact["fact_text"]), 34)
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(main.settings, VIDEO_WIDTH=360, VIDEO_HEIGHT=640, VISUAL_LAYOUT="story"):
            scenes, report = compose_visuals(beats, [], Path(tmp), 34)
            self.assertLess(report["coverage_percent"], 100)
            self.assertGreater(report["gameplay_fallback_percent"], 0)
            self.assertTrue(report["fallback_scenes"])
            self.assertTrue(report["warnings"])
            self.assertTrue(all(s["layout"] == "full" for s in scenes))
            sequence = [s for s in scenes if s["beat_index"] == 2]
            self.assertEqual(len(sequence), 1)
            for s in scenes:
                self.assertEqual(s["media_type"], "video")
            stream=probe_media(Path(sequence[0]["image_path"]))["streams"][0]
            self.assertEqual((stream["width"], stream["height"]),(360,640))
            self.assertEqual(report["moving_media_percent"], report["coverage_percent"])
            self.assertFalse(any(s.get("fallback") for s in scenes))

    def test_pure_graphic_story_never_downloads_stock_images(self):
        from pipeline.fetch_images import fetch_images_for_fact
        beat = {"visual": {"kind": "question"}, "narration": "Which one?"}
        with patch("pipeline.visual_assets.plan_scenes") as plan, patch("pipeline.fetch_images.requests.get") as get:
            self.assertEqual(fetch_images_for_fact("Which one?", "physics", [], Path("unused"), storyboard=[beat]), [])
            plan.assert_not_called()
            get.assert_not_called()

    def test_missing_subject_media_uses_gameplay_without_text_cards(self):
        beat = {"role": "hook", "narration": "Watch this.", "start": 0, "end": 30, "beat_index": 0,
                "alignment": "estimated", "visual": {"kind": "photo", "query": "golf ball"}}
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(main.settings, VIDEO_WIDTH=360, VIDEO_HEIGHT=640, VISUAL_LAYOUT="story"):
            scenes, report = compose_visuals([beat], [], Path(tmp), 30)
            self.assertEqual(report["coverage_percent"], 0)
            self.assertEqual(scenes, [])
            self.assertEqual(report["gameplay_fallback_percent"], 100)

    def test_captions_move_even_when_a_word_straddles_layout_change(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(captions, "VISUAL_LAYOUT", "story"):
            path = Path(tmp) / "captions.ass"
            captions.build_ass_captions([{"word": "airflow", "start": 0, "end": 2}], path, image_segments=[
                {"start": 0, "end": 1, "layout": "full"}, {"start": 1, "end": 2, "layout": "split"}])
            text = path.read_text()
            self.assertIn(f"\\pos({captions.VIDEO_WIDTH//2},{round(captions.VIDEO_HEIGHT*.78)})", text)
            self.assertIn(f"\\pos({captions.VIDEO_WIDTH//2},{round(captions.VIDEO_HEIGHT*captions.CINEMATIC_SPLIT)})", text)
            self.assertIn("0:00:00.00,0:00:01.00", text)
            self.assertIn("0:00:01.00,0:00:02.00", text)

    def test_story_mode_recovers_empty_image_search_and_resume_reuses_render(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            script, voice, words = root/"fact.json", root/"voice.wav", root/"words.json"
            write_json(script, self.fact)
            voice.write_bytes(b"audio")
            write_json(words, timed(self.fact["fact_text"]))
            stack.enter_context(patch.multiple(main.settings, VISUAL_LAYOUT="story", VIDEO_WIDTH=360, VIDEO_HEIGHT=640))
            stack.enter_context(patch("pipeline.doctor.check_environment", return_value=[]))
            stack.enter_context(patch("pipeline.select_background.get_duration_seconds", return_value=34))
            stack.enter_context(patch("pipeline.select_background.select_background_clip", side_effect=lambda d,p,**kw:p.write_bytes(b"game")))
            search = stack.enter_context(patch("pipeline.fetch_images.fetch_images_for_fact", return_value=[]))
            renderer = stack.enter_context(patch("pipeline.render_video.render_final_video", side_effect=lambda b,a,c,p,**kw:p.write_bytes(b"video")))
            stack.enter_context(patch("pipeline.youtube_package.validate_short", return_value=34))
            stack.enter_context(patch("pipeline.publish_snapchat.prepare_package"))
            publish = stack.enter_context(patch("pipeline.publish_youtube.publish_short"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = main.run(script=script, audio=voice, words=words, output_dir=root/"runs", upload=False,
                              experiment="length", variant="shorter")
            main.run(resume=result.parent, upload=False)
            renderer.assert_called_once()
            search.assert_called_once()
            publish.assert_not_called()
            report = read_json(result.parent/"image_report.json")
            self.assertLess(report["quality"]["coverage_percent"], 100)
            self.assertGreater(report["quality"]["gameplay_fallback_percent"], 0)
            self.assertTrue((result.parent/"storyboard.json").exists())
            self.assertEqual(read_json(result.parent/"experiment.json")["variant"], "shorter")


class MetricTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_run(self, name, variant):
        directory = self.root/name
        directory.mkdir()
        write_json(directory/"run.json", {"duration_seconds": 34})
        write_json(directory/"fact.json", main.DEMO_FACT)
        (directory/"video.mp4").write_bytes(b"test video")
        engagement.prepare_experiment(directory, main.DEMO_FACT, 34, {"experiment": "length", "variant": variant}, "story")
        return directory

    def record(self, directory, **overrides):
        values = dict(window_hours=48, stayed_to_watch=60, average_percentage_viewed=110,
                      average_view_duration=37.4, engaged_views=100)
        values.update(overrides)
        return engagement.record_metrics(directory, **values)

    def test_metrics_allow_replays_replace_same_window_and_reject_invalid_data(self):
        directory = self.make_run("one", "baseline")
        self.record(directory)
        self.record(directory, stayed_to_watch=65)
        self.record(directory, window_hours=168)
        data = read_json(directory/"engagement.json")
        self.assertEqual(len(data["snapshots"]), 2)
        self.assertEqual(data["snapshots"][0]["average_percentage_viewed"], 110)
        for values in ({"stayed_to_watch":101},{"engaged_views":1.5},{"window_hours":0},
                       {"average_view_duration":float("nan")},{"retention_drops":[35]}):
            with self.assertRaises(ValueError):
                self.record(directory, **values)

    def test_compare_uses_equal_windows_and_reports_sample_counts(self):
        a = self.make_run("old", "baseline")
        b = self.make_run("new", "shorter")
        self.record(a)
        self.record(b, stayed_to_watch=70)
        self.record(b, window_hours=168, stayed_to_watch=90)
        rows = engagement.compare_experiments(self.root, "length")
        short = next(r for r in rows if r["variant"] == "shorter" and r["window_hours"] == 48)
        self.assertEqual(short["delta_vs_baseline"]["stayed_to_watch"], 10)
        self.assertEqual(short["videos"], 1)
        week = next(r for r in rows if r["window_hours"] == 168)
        self.assertEqual(week["delta_vs_baseline"], {})

    def test_rerender_archives_old_metrics_and_does_not_claim_improvement(self):
        directory = self.make_run("one", "baseline")
        self.record(directory)
        (directory/"video.mp4").write_bytes(b"different edit")
        engagement.prepare_experiment(directory, main.DEMO_FACT, 34, {}, "story")
        self.assertEqual(read_json(directory/"engagement.json")["snapshots"], [])
        self.assertEqual(len(list(directory.glob("engagement-*.json"))), 1)
        self.assertEqual(engagement.compare_experiments(self.root, "length"), [])

    def test_label_existing_video_does_not_rewrite_it(self):
        directory = self.make_run("one", "baseline")
        before = (directory/"video.mp4").stat().st_mtime_ns
        with redirect_stdout(io.StringIO()):
            self.assertEqual(engagement.main(["label", str(directory), "--experiment", "hooks", "--variant", "question"]), 0)
        self.assertEqual((directory/"video.mp4").stat().st_mtime_ns, before)
        self.assertEqual(read_json(directory/"experiment.json")["experiment"], "hooks")


if __name__ == "__main__":
    unittest.main()
