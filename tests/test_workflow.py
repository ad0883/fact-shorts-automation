"""Checkpoint recovery and command-line workflow regression coverage."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
from pipeline.run_store import RunStore, create_run_dir, file_hash, read_json, run_lock, write_json
from pipeline.review import write_review


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root)

    def action(self, name="result.txt", text="saved"):
        path = self.root / name
        path.write_text(text)
        return [path]

    def test_resume_reuses_valid_artifacts_and_invalidates_changed_inputs(self):
        action = Mock(side_effect=self.action)
        with redirect_stdout(io.StringIO()):
            self.store.stage("render", {"input": "one"}, action)
            resumed = RunStore(self.root)
            self.assertTrue(resumed.stage("render", {"input": "one"}, action))
            self.assertFalse(resumed.stage("render", {"input": "two"}, action))
        self.assertEqual(action.call_count, 2)

    def test_deleted_or_tampered_output_is_rebuilt(self):
        action = Mock(side_effect=self.action)
        with redirect_stdout(io.StringIO()):
            self.store.stage("render", {}, action)
            (self.root / "result.txt").write_text("corrupted")
            self.store.stage("render", {}, action)
            (self.root / "result.txt").unlink()
            self.store.stage("render", {}, action)
        self.assertEqual(action.call_count, 3)

    def test_failure_keeps_completed_stages_and_can_resume(self):
        with redirect_stdout(io.StringIO()):
            self.store.stage("audio", {}, self.action)
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self.store.stage("render", {}, Mock(side_effect=RuntimeError("simulated")))
            saved = read_json(self.root / "run.json")
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["stages"]["audio"]["status"], "complete")
            resumed = RunStore(self.root)
            action = Mock(side_effect=self.action)
            resumed.stage("audio", {}, action)
            action.assert_not_called()
            resumed.stage("render", {}, lambda: self.action("video.mp4"))
            self.assertEqual(resumed.data["stages"]["render"]["status"], "complete")

    def test_keyboard_interrupt_saves_recoverable_state(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            self.store.stage("render", {}, Mock(side_effect=KeyboardInterrupt()))
        self.assertEqual(read_json(self.root / "run.json")["status"], "interrupted")

    def test_runs_do_not_collide(self):
        first = create_run_dir(self.root, "same-title")
        second = create_run_dir(self.root, "same-title")
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())

    def test_lock_blocks_concurrent_process_and_releases_on_exception(self):
        with self.assertRaisesRegex(ValueError, "exit"):
            with run_lock(self.root):
                with self.assertRaises(RuntimeError):
                    with run_lock(self.root):
                        self.fail("Lock was acquired twice")
                raise ValueError("exit")
        with run_lock(self.root):
            pass

    def test_stage_cannot_register_outputs_outside_run(self):
        with tempfile.TemporaryDirectory() as external:
            path = Path(external) / "secret"
            path.write_text("data")
            with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                self.store.stage("bad", {}, lambda: [path])

    def test_atomic_json_rejects_nan_without_clobbering_previous_file(self):
        path = self.root / "state.json"
        write_json(path, {"valid": 1})
        with self.assertRaises(ValueError):
            write_json(path, {"invalid": float("nan")})
        self.assertEqual(read_json(path), {"valid": 1})
        self.assertEqual(list(self.root.glob(".state.json.*")), [])


class WorkflowTests(unittest.TestCase):
    def test_missing_youtube_setup_saves_video_then_resumes_upload_without_rendering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, audio, words = root / "fact.json", root / "voice.wav", root / "words.json"
            write_json(script, main.DEMO_FACT)
            audio.write_bytes(b"provided audio")
            write_json(words, [{"word": "Hello", "start": 0, "end": 1}])
            def background(duration, path, **kwargs):
                path.write_bytes(b"background")
            def render(background, audio, captions, path, **kwargs):
                path.write_bytes(b"rendered video")
            def preflight(**kwargs):
                return [{"name": "YouTube credentials", "ok": False, "required": kwargs["upload_required"],
                         "upload": True, "detail": "Connect your channel"}]
            with patch("main.settings.AUTO_UPLOAD_YOUTUBE", True), \
                 patch("pipeline.doctor.check_environment", side_effect=preflight) as checks, \
                 patch("pipeline.select_background.get_duration_seconds", return_value=1), \
                 patch("pipeline.select_background.select_background_clip", side_effect=background), \
                 patch("pipeline.render_video.render_final_video", side_effect=render) as renderer, \
                 patch("pipeline.youtube_package.validate_short", return_value=1), \
                 patch("pipeline.publish_snapchat.prepare_package"), \
                 patch("pipeline.publish_youtube.publish_short", return_value={"url": "https://www.youtube.com/watch?v=mock"}) as publish, \
                 redirect_stdout(io.StringIO()) as output:
                video = main.run(script=script, audio=audio, words=words, no_images=True, output_dir=root / "runs")
                state = read_json(video.parent / "run.json")
                self.assertEqual(state["status"], "upload_pending")
                self.assertTrue(state["options"]["upload"])
                self.assertEqual(state["stages"]["render"]["status"], "complete")
                self.assertTrue(video.is_file())
                self.assertIn("Video saved locally", output.getvalue())
                publish.assert_not_called()
                with self.assertRaisesRegex(ValueError, "Preflight failed"):
                    main.run(resume=video.parent, upload=True)
                checks.side_effect = None
                checks.return_value = []
                main.run(resume=video.parent)
                publish.assert_called_once()
                self.assertNotIn("thumbnail", publish.call_args.kwargs)
                self.assertNotIn("captions", publish.call_args.kwargs)
                renderer.assert_called_once()
                self.assertEqual(read_json(video.parent / "run.json")["status"], "complete")

    def test_empty_image_search_stops_before_render_and_keeps_resume_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, audio, words = root / "fact.json", root / "voice.wav", root / "words.json"
            write_json(script, main.DEMO_FACT)
            audio.write_bytes(b"provided audio")
            write_json(words, [{"word": "Hello", "start": 0, "end": 1}])
            def background(duration, path, **kwargs):
                path.write_bytes(b"background")
            with patch("main.settings.VISUAL_LAYOUT", "cinematic"), \
                 patch("pipeline.doctor.check_environment", return_value=[]), \
                 patch("pipeline.select_background.get_duration_seconds", return_value=1), \
                 patch("pipeline.select_background.select_background_clip", side_effect=background), \
                 patch("pipeline.fetch_images.fetch_images_for_fact", return_value=[]), \
                 patch("pipeline.render_video.render_final_video") as render, redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "No relevant images"):
                    main.run(script=script, audio=audio, words=words, output_dir=root / "runs")
            render.assert_not_called()
            state_path = next((root / "runs").glob("*/run.json"))
            state = read_json(state_path)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["stages"]["images"]["status"], "failed")
            self.assertEqual(state["stages"]["audio"]["status"], "complete")
            self.assertEqual(read_json(state_path.parent / "image_report.json")["selected_scenes"], 0)

    def test_resume_command_preserves_interpreter_and_paths_with_shell_characters(self):
        interpreter = "/project's folder/venv/bin/python"
        project = Path("/project's folder")
        run_dir = project / "output" / "a $(literal) run"
        with patch("main.sys.executable", interpreter), patch("main.settings.PROJECT_ROOT", project):
            command = main.resume_command(run_dir)
        self.assertEqual(shlex.split(command), [interpreter, str(project / "main.py"), "--resume", str(run_dir)])

    def test_script_only_never_calls_providers_or_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = directory / "script.json"
            write_json(script, main.DEMO_FACT)
            with patch("pipeline.generate_fact.generate_fact") as generate, patch("main.subprocess.run") as media, redirect_stdout(io.StringIO()):
                result = main.run(script=script, script_only=True, output_dir=directory / "runs")
            generate.assert_not_called()
            media.assert_not_called()
            self.assertEqual(result.name, "fact.json")
            state = read_json(result.parent / "run.json")
            self.assertEqual(state["status"], "script_ready")
            self.assertEqual(read_json(result)["review_status"], "needs_review")

    def test_missing_inputs_fail_before_generation(self):
        with patch("pipeline.doctor.check_environment", return_value=[{"name": "footage", "ok": False, "required": True, "detail": "missing"}]), patch("pipeline.generate_fact.generate_fact") as generate, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "Preflight failed"):
                main.run()
            generate.assert_not_called()

    def test_incompatible_input_modes_are_rejected(self):
        cases = ({"words": Path("timings.json")}, {"demo": True, "upload": True},
                 {"script_only": True, "upload": True}, {"resume": Path("run"), "category": "new"},
                 {"refresh_images": True})
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                main.run(**case)

    def test_changed_input_file_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audio.wav"
            path.write_bytes(b"first")
            option = main.source_option(path)
            path.write_bytes(b"second")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                main.copy_input(option, Path(temporary) / "copy.wav")

    def test_review_escapes_script_html_and_rejects_script_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            fact = {**main.DEMO_FACT, "title": '<script>alert("x")</script>',
                    "sources": [{"title": "bad", "url": "javascript:alert(1)"}]}
            path = write_review(Path(temporary), fact, duration=8, image_segments=[])
            html = path.read_text()
            self.assertNotIn("<script>", html)
            self.assertNotIn("javascript:", html)
            self.assertIn("&lt;script&gt;", html)

    def test_review_displays_nested_image_credits(self):
        with tempfile.TemporaryDirectory() as temporary:
            segments = [{"provenance": {"photographer": "A & B", "photo_url": "https://www.pexels.com/photo/1/"}}]
            path = write_review(Path(temporary), main.DEMO_FACT, duration=8, image_segments=segments)
            self.assertIn("A &amp; B", path.read_text())
            self.assertIn("https://www.pexels.com/photo/1/", path.read_text())

    def test_review_shows_source_claims_as_escaped_review_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            fact = {**main.DEMO_FACT, "sources": [
                {"title": "Reference", "url": "https://example.org", "claim": "<script>Claim & evidence</script>"},
                {"title": "Older reference", "url": "https://example.org/older"},
            ]}
            html = write_review(Path(temporary), fact, duration=8, image_segments=[]).read_text()
            self.assertIn("Claim to check: &lt;script&gt;Claim &amp; evidence&lt;/script&gt;", html)
            self.assertIn("No supporting claim supplied.", html)
            self.assertNotIn("<script>", html)

    def test_cli_image_mode_defaults_preserve_saved_choice(self):
        for flags, expected in (([], None), (["--no-images"], True), (["--images"], False)):
            with self.subTest(flags=flags), patch("main.run") as run:
                self.assertEqual(main.main(flags), 0)
                self.assertIs(run.call_args.kwargs["no_images"], expected)

    def test_cli_reports_failures_with_nonzero_exit(self):
        with patch("main.run", side_effect=RuntimeError("broken")), redirect_stderr(io.StringIO()):
            self.assertEqual(main.main([]), 1)


if __name__ == "__main__":
    unittest.main()
