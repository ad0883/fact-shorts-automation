"""Media contracts and small, offline FFmpeg integration fixtures."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pipeline import build_captions as captions
from pipeline import render_video as renderer
from pipeline import select_background as background


class CaptionTests(unittest.TestCase):
    def test_cinematic_headlines_and_illustration_labels_are_safe(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(captions, "VISUAL_LAYOUT", "cinematic"):
            path = Path(temporary) / "captions.ass"
            captions.build_ass_captions([{"word": "Voyager", "start": 0, "end": 1}], path,
                image_segments=[{"start": 0, "end": 1, "headline": r"A {\pos(0,0)}border",
                                 "provenance": {"provider": "nasa", "usage": "artist_concept"}}])
            result = path.read_text()
            self.assertIn("NASA / ARTIST CONCEPT", result)
            self.assertNotIn(r"\pos(0,0)", result)
            self.assertNotIn("BORDER", result)
            self.assertIn(f"\\pos({captions.VIDEO_WIDTH//2},{round(captions.VIDEO_HEIGHT*captions.CINEMATIC_SPLIT)})", result)

    def test_default_captions_show_at_most_two_words(self):
        words = [{"word": word, "start": i*.3, "end": (i+1)*.3}
                 for i, word in enumerate(["one", "two", "three", "four"])]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "captions.ass"
            captions.build_ass_captions(words, path)
            events = [line for line in path.read_text().splitlines() if line.startswith("Dialogue:")]
            self.assertIn("TWO", events[0])
            self.assertNotIn("THREE", events[0])
            self.assertIn("THREE", events[2])
            self.assertNotIn("TWO", events[2])

    def test_timestamp_rounding_carries_at_minute_and_hour(self):
        self.assertEqual(captions.format_ass_time(59.999), "0:01:00.00")
        self.assertEqual(captions.format_ass_time(3599.999), "1:00:00.00")
        self.assertEqual(captions.format_srt_time(59.9999), "00:01:00,000")
        for invalid in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                captions.format_ass_time(invalid)

    def test_malformed_timings_are_rejected_before_writing(self):
        for words in ([], [{"word": "hello", "start": -1, "end": 1}],
                      [{"word": "hello", "start": 2, "end": 1}],
                      [{"word": "hello", "start": float("nan"), "end": 1}],
                      [{"word": "hello", "start": 0}],
                      [{"word": "one", "start": 1, "end": 2}, {"word": "two", "start": 0, "end": 3}]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                captions.validate_words(words)

    def test_overlap_is_repaired_without_mutating_input(self):
        words = [{"word": "one", "start": 0, "end": 0.6},
                 {"word": "two", "start": 0.5, "end": 0.5}]
        fixed = captions.validate_words(words)
        self.assertEqual(words[0]["end"], 0.6)
        self.assertEqual(fixed[0]["end"], 0.5)
        self.assertGreater(fixed[1]["end"], fixed[1]["start"])

    def test_duplicate_and_near_identical_times_remain_visible(self):
        words = [{"word": text, "start": start, "end": 0.2}
                 for text, start in [("one", 0), ("two", 0), ("three", 0.001)]]
        fixed = captions.validate_words(words)
        for current, following in zip(fixed, fixed[1:]):
            self.assertGreaterEqual(current["end"] - current["start"], 0.009)
            self.assertLessEqual(current["end"], following["start"])

    def test_phrase_highlight_and_portable_subtitles(self):
        words = [{"word": "hello", "start": 0, "end": 0.4},
                 {"word": "world.", "start": 0.4, "end": 0.8},
                 {"word": r"{\pos(0,0)}safe\Ntext", "start": 1, "end": 1.5}]
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "captions.ass"
            srt = Path(temporary) / "captions.srt"
            captions.build_ass_captions(words, ass)
            captions.build_srt_captions(words, srt)
            text = ass.read_text()
            events = [line for line in text.splitlines() if line.startswith("Dialogue:")]
            self.assertEqual(len(events), 3)
            self.assertIn("HELLO", events[0])
            self.assertIn("WORLD.", events[0])
            self.assertNotEqual(events[0].split(",,", 2)[-1], events[1].split(",,", 2)[-1])
            self.assertNotIn(r"\pos(0,0)", text)
            self.assertNotIn(r"safe\Ntext", text)
            self.assertIn("00:00:00,000 --> 00:00:00,800\nhello world.", srt.read_text())


class MediaValidationTests(unittest.TestCase):
    def test_missing_assets_raise_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(background, "ASSETS_DIR", Path(directory)):
            with self.assertRaisesRegex(FileNotFoundError, "No gameplay video"):
                background.pick_source_clip()

    def test_case_insensitive_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MY_CLIP.MP4"
            path.write_bytes(b"fixture")
            with patch.object(background, "ASSETS_DIR", Path(directory)):
                self.assertEqual(background.pick_source_clip(), path)

    def test_invalid_duration_fails_before_media_work(self):
        for duration in (0, -1, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                background.select_background_clip(duration, Path("unused.mp4"))

    def test_probe_rejects_unparseable_duration(self):
        with patch.object(background, "probe_media", return_value={"format": {"duration": "NaN"}, "streams": []}):
            with self.assertRaisesRegex(ValueError, "positive duration"):
                background.get_duration_seconds(Path("bad.mp4"))

    def test_probe_can_use_stream_duration(self):
        with patch.object(background, "probe_media", return_value={"format": {"duration": "N/A"}, "streams": [{"duration": "1.25"}]}):
            self.assertEqual(background.get_duration_seconds(Path("clip.mp4")), 1.25)

    def test_tool_failure_contains_useful_diagnostics(self):
        result = subprocess.CompletedProcess(["ffmpeg"], 1, "", "Unknown encoder 'libx264'")
        with patch.object(background.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "Unknown encoder"):
                background.run_media_command(["ffmpeg"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are required")
class FFmpegIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=True)
        if "subtitles" not in filters.stdout:
            raise unittest.SkipTest("FFmpeg lacks the libass subtitles filter")
        cls.temporary = tempfile.TemporaryDirectory(prefix="shorts path's [test], ")
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "source's clip.mp4"
        cls.audio = cls.directory / "narration's track.wav"
        cls.image = cls.directory / "image's card.png"
        cls.captions = cls.directory / "caption's [text],file.ass"
        cls.duration = 1.7
        cls._ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15", "-t", "0.6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.source)])
        cls._ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", str(cls.duration), str(cls.audio)])
        cls._ffmpeg(["-f", "lavfi", "-i", "color=c=teal:s=120x200", "-frames:v", "1", str(cls.image)])
        captions.build_ass_captions([
            {"word": "Offline", "start": 0, "end": 0.8},
            {"word": "fixture.", "start": 0.8, "end": cls.duration},
        ], cls.captions)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @staticmethod
    def _ffmpeg(arguments):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments], check=True, capture_output=True)

    def setUp(self):
        self.patches = ExitStack()
        for module in (background, renderer):
            self.patches.enter_context(patch.object(module, "VIDEO_WIDTH", 160))
            self.patches.enter_context(patch.object(module, "VIDEO_HEIGHT", 284))
        self.patches.enter_context(patch.object(background.settings, "VIDEO_FPS", 15, create=True))
        self.addCleanup(self.patches.close)

    def test_short_background_loops_and_remains_animated(self):
        output = self.directory / "prepared's clip.mp4"
        background.select_background_clip(self.duration, output, source_path=self.source)
        self.assertAlmostEqual(background.get_duration_seconds(output), self.duration, delta=0.08)
        stream = background.probe_media(output)["streams"][0]
        self.assertEqual((stream["width"], stream["height"], stream["pix_fmt"]), (160, 284, "yuv420p"))
        frame_hashes = []
        for timestamp in (1.0, 1.3):
            frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(output), "-frames:v", "1", "-f", "md5", "-"], capture_output=True, check=True)
            frame_hashes.append(frame.stdout)
        self.assertNotEqual(*frame_hashes, "Looped footage must keep moving after the first source playback")

    def test_render_paths_and_overlay_preserve_full_narration(self):
        for with_image in (False, True):
            with self.subTest(with_image=with_image):
                output = self.directory / f"final's [{with_image}], output.mp4"
                segments = [{"image_path": str(self.image), "start": 0.2, "end": 1.5}] if with_image else None
                renderer.render_final_video(self.source, self.audio, self.captions, output, segments)
                self.assertAlmostEqual(background.get_duration_seconds(output), self.duration, delta=0.08)
                streams = {stream["codec_type"]: stream for stream in background.probe_media(output)["streams"]}
                self.assertEqual(streams["video"]["codec_name"], "h264")
                self.assertEqual(streams["video"]["pix_fmt"], "yuv420p")
                self.assertEqual(streams["audio"]["codec_name"], "aac")
                self.assertEqual(streams["audio"]["sample_rate"], "48000")
                data = output.read_bytes()
                self.assertLess(data.index(b"moov"), data.index(b"mdat"))

    def test_failed_render_preserves_previous_output(self):
        output = self.directory / "keep prior.mp4"
        output.write_bytes(b"previous completed render")
        with patch.object(renderer, "run_media_command", side_effect=RuntimeError("synthetic encoder failure")):
            with self.assertRaisesRegex(RuntimeError, "encoder failure"):
                renderer.render_final_video(self.source, self.audio, self.captions, output)
        self.assertEqual(output.read_bytes(), b"previous completed render")
        self.assertEqual(list(self.directory.glob(".render-*")), [])

    def test_cinematic_scenes_switch_while_gameplay_keeps_moving(self):
        red = self.directory / "red.png"
        self._ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=120x200", "-frames:v", "1", str(red)])
        output = self.directory / "scene transitions.mp4"
        scenes = [{"image_path": str(path), "start": start, "end": end, "fit": "cover"}
                  for path, start, end in [(self.image, 0, .8), (red, .8, self.duration)]]
        with patch.object(renderer, "VISUAL_LAYOUT", "cinematic"):
            renderer.render_final_video(self.source, self.audio, self.captions, output, scenes)
        colors, game_frames = [], []
        for timestamp in (.3, 1.2):
            command = ["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(output), "-frames:v", "1"]
            pixel = subprocess.run(command + ["-vf", "crop=2:2:80:80,scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True)
            colors.append(tuple(pixel.stdout))
            frame = subprocess.run(command + ["-vf", "crop=160:60:0:220", "-f", "md5", "-"], capture_output=True, check=True)
            game_frames.append(frame.stdout)
        self.assertLess(colors[0][0], 30)  # teal first scene
        self.assertGreater(colors[0][1], 80)
        self.assertGreater(colors[1][0], 200)  # red second scene
        self.assertLess(colors[1][1], 30)
        self.assertNotEqual(*game_frames)

    def test_story_fullscreen_covers_gameplay_then_restores_split(self):
        output = self.directory / "full to split.mp4"
        scenes = [{"image_path": str(self.image), "start": a, "end": b, "fit": "contain", "layout": layout}
                  for a,b,layout in [(0,.8,"full"),(.8,self.duration,"split")]]
        with patch.object(renderer, "VISUAL_LAYOUT", "story"):
            renderer.render_final_video(self.source, self.audio, self.captions, output, scenes)
        pixels = []
        for timestamp in (.3, 1.2):
            pixel = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(output),
                "-frames:v", "1", "-vf", "crop=2:2:80:250,scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                capture_output=True, check=True)
            pixels.append(tuple(pixel.stdout))
        self.assertLess(pixels[0][0], 30)
        self.assertGreater(pixels[0][1], 80)
        self.assertNotEqual(pixels[0], pixels[1])

    def test_every_frame_at_fractional_cuts_has_the_expected_visual(self):
        """Metadata can be gapless while the decoder exposes gameplay at EOF."""
        red = self.directory / "gap-test-red.png"
        self._ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=120x200", "-frames:v", "1", str(red)])
        red_video = self.directory / "gap-test-red.mp4"
        self._ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=120x200:rate=15", "-t", "0.2", "-c:v", "libx264", str(red_video)])
        bounds = [0, .217, .563, .951, 1.183, self.duration]
        scenes = [{"image_path": str(self.image if i%2 == 0 else red_video if i==1 else red), "start": a, "end": b,
                   "media_type": "video" if i==1 else "image", "fit": "cover", "layout": "full" if i%2 == 0 else "split"}
                  for i,(a,b) in enumerate(zip(bounds,bounds[1:]))]
        output = self.directory / "fractional-cut-audit.mp4"
        with patch.object(renderer, "VISUAL_LAYOUT", "story"):
            renderer.render_final_video(self.source, self.audio, self.captions, output, scenes)
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vf", "crop=2:2:80:40,scale=1:1",
                                  "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        for frame in range(len(decoded)//3):
            t = frame/15
            scene = next((i for i,(a,b) in enumerate(zip(bounds,bounds[1:])) if a <= t < b), len(scenes)-1)
            r,g,b = decoded[frame*3:frame*3+3]
            with self.subTest(frame=frame, time=t):
                if scene%2:
                    self.assertGreater(r, 200)
                    self.assertLess(g, 35)
                else:
                    self.assertLess(r, 35)
                    self.assertGreater(g, 80)

    def test_footage_shot_seeks_forward_instead_of_restarting(self):
        clip = self.directory / "two-colors.mp4"
        self._ffmpeg(["-f", "lavfi", "-i", "color=red:s=160x320:r=15:d=1",
                      "-f", "lavfi", "-i", "color=blue:s=160x320:r=15:d=1",
                      "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                      "-c:v", "libx264", str(clip)])
        output = self.directory / "advance-footage.mp4"
        scene={"image_path":str(clip),"start":0,"end":self.duration,"fit":"cover","layout":"full",
               "media_type":"video","source_offset":1.1,"camera":"pull"}
        with patch.object(renderer,"VISUAL_LAYOUT","story"):
            renderer.render_final_video(self.source,self.audio,self.captions,output,[scene])
        pixel=subprocess.run(["ffmpeg","-v","error","-ss","0.1","-i",str(output),"-frames:v","1",
                              "-vf","crop=2:2:80:40,scale=1:1","-pix_fmt","rgb24","-f","rawvideo","-"],
                             capture_output=True,check=True).stdout
        self.assertGreater(pixel[2],200)
        self.assertLess(pixel[0],35)


if __name__ == "__main__":
    unittest.main()
