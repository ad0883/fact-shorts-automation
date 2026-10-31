"""Recoverable fact-short production. Run `python main.py --help` for workflows."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from config import settings
from pipeline.run_store import RunStore, create_run_dir, file_hash, read_json, run_lock, write_json


DEMO_FACT = {
    "title": "Your next short starts here",
    "category": "Offline render demo",
    "description": "An eight-second silent preview of the local video pipeline, animated captions, and looping background.",
    "tags": ["demo"],
    "fact_text": "Your next short starts here. Clear captions, smooth footage, and a workflow you can pick up anytime.",
    "sources": [],
}


def resume_command(run_dir: Path) -> str:
    return shlex.join([sys.executable, str(settings.PROJECT_ROOT / "main.py"), "--resume", str(run_dir)])


def code_hash(*names):
    return {name: file_hash(settings.PROJECT_ROOT / "pipeline" / f"{name}.py") for name in names}


def source_option(path: Path | None):
    if path is None:
        return None
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Input file is missing or empty: {path}")
    return {"path": str(path), "sha256": file_hash(path)}


def copy_input(source: dict, destination: Path):
    path = Path(source["path"])
    if not path.is_file() or file_hash(path) != source["sha256"]:
        raise ValueError(f"Original input is missing or changed: {path}. Start a new run with the updated input.")
    temporary = destination.with_name(f".{destination.stem}.copy{destination.suffix}")
    try:
        shutil.copyfile(path, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def media_command(arguments):
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Could not create demo media: {result.stderr[-2000:]}")


def upload_options(upload: bool | None, privacy: str | None, options: dict, *, script_only=False):
    if options.get("demo") or script_only:
        if upload:
            raise ValueError("Demo and script-only runs cannot be uploaded")
        return False, privacy or settings.YOUTUBE_PRIVACY_STATUS
    enabled = upload if upload is not None else options.get("upload", settings.AUTO_UPLOAD_YOUTUBE)
    visibility = privacy or options.get("privacy", settings.YOUTUBE_PRIVACY_STATUS)
    if privacy and not enabled:
        raise ValueError("--privacy requires --upload or AUTO_UPLOAD_YOUTUBE=true")
    if enabled and visibility not in {"private", "unlisted", "public"}:
        raise ValueError("YouTube privacy must be private, unlisted, or public")
    options.update(upload=bool(enabled), privacy=visibility)
    return bool(enabled), visibility


def run(category: str | None = None, upload: bool | None = None, privacy: str | None = None, *,
        script: Path | None = None, audio: Path | None = None, words: Path | None = None,
        background: Path | None = None, resume: Path | None = None, output_dir: Path | None = None,
        visual_plan: Path | None = None, experiment: str | None = None, variant: str | None = None,
        no_images: bool | None = None, caption_style: str | None = None,
        script_only: bool = False, demo: bool = False, refresh_images: bool = False,
        snapchat: bool | None = None, instagram: bool | None = None, facebook: bool | None = None) -> Path:
    from pipeline.build_captions import build_ass_captions, build_srt_captions, validate_words
    from pipeline.doctor import check_environment, print_checks
    from pipeline.generate_fact import generate_fact, slugify, validate_fact
    from pipeline.select_background import get_duration_seconds, select_background_clip

    if words and not audio:
        raise ValueError("--words requires --audio so timestamps match the narration")
    if demo and any((script, audio, words, background, category, script_only, upload, visual_plan)):
        raise ValueError("--demo cannot be combined with production inputs, --script-only, or --upload")
    if resume and any((script, audio, words, background, category, demo, output_dir, visual_plan)):
        raise ValueError("--resume uses the saved inputs; start a new run to change input files")
    if script_only and upload:
        raise ValueError("--script-only cannot be combined with --upload")
    if refresh_images and (not resume or no_images):
        raise ValueError("--refresh-images requires --resume and cannot be combined with --no-images")
    if visual_plan and no_images:
        raise ValueError("--visual-plan cannot be combined with --no-images")

    fact_input = None
    if resume:
        run_dir = resume.expanduser().resolve()
        if not (run_dir / "run.json").is_file():
            raise ValueError("--resume needs a run directory containing run.json; use --script for older fact.json files")
        options = read_json(run_dir / "run.json").get("options", {})
        demo = bool(options.get("demo"))
        if demo and upload:
            raise ValueError("Demo runs cannot be uploaded")
        if no_images is not None:
            options["no_images"] = no_images
        if caption_style is not None:
            options["caption_style"] = caption_style
    else:
        if script:
            fact_input = validate_fact(read_json(script), category=category)
        if demo:
            fact_input = validate_fact(DEMO_FACT)
        options = {"category": category, "demo": demo, "no_images": bool(no_images or demo),
                   "caption_style": caption_style or "phrase", "audio": source_option(audio),
                   "words": source_option(words), "background": source_option(background)}
        if visual_plan:
            from pipeline.visual_assets import visual_plan_option
            options["visual_plan"] = visual_plan_option(visual_plan)
        run_dir = None

    if options.get("caption_style") not in {"phrase", "word"}:
        raise ValueError("Caption style must be 'phrase' or 'word'")
    if refresh_images:
        if demo:
            raise ValueError("Offline demo runs do not use stock images")
        options["no_images"] = False
    if experiment is not None:
        options["experiment"] = experiment.strip()
    if variant is not None:
        options["variant"] = variant.strip()
    for key in ("experiment", "variant"):
        if key in options and (not options[key] or len(options[key]) > 100):
            raise ValueError(f"--{key} must contain 1-100 characters")

    if (demo or script_only) and snapchat:
        raise ValueError("Demo and script-only runs cannot be posted to Spotlight")
    snapchat_enabled = False if demo or script_only else (
        snapchat if snapchat is not None else options.get("snapchat", settings.AUTO_UPLOAD_SNAPCHAT))
    if upload is False and snapchat is None:
        snapchat_enabled = False
    options["snapchat"] = bool(snapchat_enabled)

    if (demo or script_only) and instagram:
        raise ValueError("Demo and script-only runs cannot be posted to Instagram")
    instagram_enabled = False if demo or script_only else (
        instagram if instagram is not None else options.get("instagram", settings.AUTO_UPLOAD_INSTAGRAM))
    if upload is False and instagram is None:
        instagram_enabled = False
    options["instagram"] = bool(instagram_enabled)

    if (demo or script_only) and facebook:
        raise ValueError("Demo and script-only runs cannot be posted to Facebook")
    facebook_enabled = False if demo or script_only else (
        facebook if facebook is not None else options.get("facebook", settings.AUTO_UPLOAD_FACEBOOK))
    if upload is False and facebook is None:
        facebook_enabled = False
    options["facebook"] = bool(facebook_enabled)

    explicit_upload = upload is True
    upload, privacy = upload_options(upload, privacy, options, script_only=script_only)
    upload_issues = []

    # Fail before a model call or model download when required local assets are absent.
    if script_only:
        if not fact_input and not (run_dir and (run_dir / "fact.json").is_file()):
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not key or key == "your_key_here":
                raise ValueError("Set GEMINI_API_KEY in .env, or provide --script")
            if importlib.util.find_spec("requests") is None:
                raise ValueError("Install requests: python -m pip install requests")
    else:
        def existing(name, stage=None):
            if not run_dir or not (run_dir / name).is_file():
                return False
            if stage is None:
                return True
            record = read_json(run_dir / "run.json").get("stages", {}).get(stage, {})
            expected = record.get("artifacts", {}).get(name)
            return record.get("status") == "complete" and expected == file_hash(run_dir / name)

        audio_name = ("narration.wav" if demo else "narration" + Path(options["audio"]["path"]).suffix.lower()
                      if options.get("audio") else "narration.mp3")
        checks = check_environment(
            need_generation=not (fact_input or existing("fact.json")),
            need_tts=not (demo or options.get("audio") or existing(audio_name, "audio")),
            need_whisper=not (demo or options.get("words") or existing("words.json", "transcription")),
            need_footage=not (demo or existing("background.mp4", "background")),
            source_path=Path(options["background"]["path"]) if options.get("background") else None,
            upload=upload,
            upload_required=explicit_upload,
            need_graphics=not (demo or options.get("no_images")),
        )
        upload_issues = [check["detail"] for check in checks if check.get("upload") and not check["ok"]]
        if not print_checks(checks):
            raise ValueError("Preflight failed. Resolve the checks above; --demo tests rendering without online services.")
        if upload_issues:
            print("YouTube upload is pending setup. Video creation will continue locally.")
    if run_dir is None:
        label = slugify(fact_input["title"] if fact_input else category or "fact-short")
        run_dir = create_run_dir(output_dir or settings.OUTPUT_DIR, label)
    print(f"\nRun: {run_dir}", flush=True)

    with run_lock(run_dir):
        store = RunStore(run_dir, options)
        if refresh_images:
            store.data["stages"].pop("images", None)
        store.save(options=options, status="running", error=None)
        try:
            fact_path = run_dir / "fact.json"
            if fact_path.exists():
                # Deliberate edits to a run's script invalidate downstream cached stages.
                fact = validate_fact(read_json(fact_path))
                write_json(fact_path, fact)
            else:
                def create_fact():
                    generated = fact_input or generate_fact(options.get("category"))
                    write_json(fact_path, generated)
                    return [fact_path]
                store.stage("script", {"category": options.get("category"), "model": settings.GEMINI_MODEL}, create_fact)
                fact = validate_fact(read_json(fact_path))
                write_json(fact_path, fact)
            print(f"\n{fact['title']}\n{fact['fact_text']}\n", flush=True)
            for note in fact.get("quality_warnings", []):
                print(f"  Review note: {note}", flush=True)
            fact_digest = file_hash(fact_path)
            # Keep script provenance accurate even when the user has edited fact.json.
            store.data["stages"]["script"] = {"status": "complete", "artifacts": {"fact.json": fact_digest}}
            if script_only:
                store.save(status="script_ready", current_stage=None)
                print(f"Script saved: {fact_path}\nContinue after review: {resume_command(run_dir)}")
                return fact_path

            audio_path = run_dir / ("narration.wav" if demo else ("narration" + Path(options["audio"]["path"]).suffix.lower() if options.get("audio") else "narration.mp3"))
            def create_audio():
                if demo:
                    media_command(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "8", str(audio_path)])
                    narration_metadata = {"provider": "silent_demo"}
                elif options.get("audio"):
                    copy_input(options["audio"], audio_path)
                    narration_metadata = {"provider": "user_recording"}
                else:
                    from pipeline.generate_audio import generate_audio
                    narration_metadata = generate_audio(fact["fact_text"], audio_path)
                metadata_path = run_dir / "narration.json"
                write_json(metadata_path, {**narration_metadata, "audio_sha256": file_hash(audio_path)})
                return [audio_path, metadata_path]
            store.stage("audio", {"text": fact["fact_text"], "source": options.get("audio"), "demo": demo,
                                  "provider": settings.TTS_PROVIDER, "fallback": settings.TTS_FALLBACK_PROVIDER,
                                  "gemini_model": settings.GEMINI_TTS_MODEL, "gemini_voice": settings.GEMINI_TTS_VOICE,
                                  "style": settings.TTS_STYLE,
                                  "voice": settings.TTS_VOICE, "rate": settings.TTS_RATE, "pitch": settings.TTS_PITCH,
                                  "code": code_hash("generate_audio", "gemini_speech")}, create_audio)
            duration = get_duration_seconds(audio_path)
            if not 0 < duration <= 180:
                raise ValueError(f"Narration is {duration:.1f}s; use a script/audio between 0 and 180 seconds")
            if not demo and not 30 <= duration <= 40:
                print(f"  Pacing review: narration is {duration:.1f}s; the shorter-format experiment targets 30-40s.", flush=True)
            audio_digest = file_hash(audio_path)
            words_path = run_dir / "words.json"
            def create_words():
                if demo:
                    tokens = fact["fact_text"].split()
                    spacing = duration / len(tokens)
                    timed = [{"word": word, "start": i * spacing, "end": (i + 1) * spacing} for i, word in enumerate(tokens)]
                elif options.get("words"):
                    source = options["words"]
                    if file_hash(Path(source["path"])) != source["sha256"]:
                        raise ValueError("Original timestamps changed. Start a new run with the updated --words file.")
                    timed = read_json(Path(source["path"]))
                else:
                    from pipeline.transcribe import transcribe_words
                    timed = transcribe_words(audio_path, initial_prompt=fact["fact_text"])
                write_json(words_path, validate_words(timed))
                return [words_path]
            store.stage("transcription", {"audio": audio_digest, "text": fact["fact_text"], "source": options.get("words"),
                                          "model": settings.WHISPER_MODEL_SIZE, "demo": demo,
                                          "code": code_hash("transcribe", "build_captions")}, create_words)
            timed_words = validate_words(read_json(words_path))
            if timed_words[-1]["end"] > duration + 0.25:
                raise ValueError("Word timestamps extend past the narration. Supply timestamps for this exact audio.")

            background_path = run_dir / "background.mp4"
            def create_background():
                source = None
                if demo:
                    source = run_dir / "demo-source.mp4"
                    media_command(["-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
                elif options.get("background"):
                    source = Path(options["background"]["path"])
                    if file_hash(source) != options["background"]["sha256"]:
                        raise ValueError("Background source changed. Start a new run using the updated --background file.")
                select_background_clip(duration, background_path, source_path=source)
                return [background_path]
            store.stage("background", {"duration": duration, "source": options.get("background"), "demo": demo,
                                       "dimensions": [settings.VIDEO_WIDTH, settings.VIDEO_HEIGHT, settings.VIDEO_FPS],
                                       "code": code_hash("select_background")}, create_background)

            segments_path = run_dir / "image_segments.json"
            image_report_path = run_dir / "image_report.json"
            storyboard_path = run_dir / "storyboard.json"
            images_enabled = not options.get("no_images") and not demo
            from pipeline.storyboard import time_storyboard, compose_visuals, coverage_report
            beats = time_storyboard(fact, timed_words, duration)
            write_json(storyboard_path, {"beats": beats, "duration_seconds": duration})
            def create_images():
                segments = []
                report = {"enabled": images_enabled}
                if images_enabled:
                    if options.get("visual_plan"):
                        from pipeline.visual_assets import import_visual_plan
                        segments = import_visual_plan(options["visual_plan"], run_dir / "images", duration)
                    else:
                        from pipeline.fetch_images import fetch_images_for_fact
                        segments = fetch_images_for_fact(fact["fact_text"], fact["category"], timed_words, run_dir / "images", report=report, subject_hint=fact["title"],
                                                         storyboard=beats if fact.get("storyboard") else None)
                        if segments and 0 <= duration - segments[-1]["end"] <= 1.0:
                            segments[-1]["end"] = duration
                if images_enabled and (settings.VISUAL_LAYOUT == "story" or fact.get("storyboard")):
                    segments, quality = compose_visuals(beats, segments, run_dir / "images", duration)
                    report["quality"] = quality
                elif images_enabled:
                    quality = coverage_report(segments, duration)
                    quality["warnings"] = (["Visual gaps leave gameplay visible; use VISUAL_LAYOUT=story to add relevant fallback cards."] if quality["gaps"] else [])
                    if quality["long_scenes"]:
                        quality["warnings"].append("Some visual scenes exceed 6 seconds; review their pacing.")
                    report["quality"] = quality
                write_json(segments_path, segments)
                report.update(selected_scenes=len(segments), unique_images=len({s["image_path"] for s in segments}))
                write_json(image_report_path, report)
                if images_enabled and not segments and settings.VISUAL_LAYOUT != "story" and not fact.get("storyboard"):
                    raise RuntimeError("No relevant images could be downloaded. Progress is saved; retry with --resume and --refresh-images, "
                                       "or use --no-images only if you want a gameplay-only video. See image_report.json for the searches.")
                return [segments_path, image_report_path] + [Path(seg["image_path"]) for seg in segments]
            store.stage("images", {"enabled": images_enabled, "fact": fact_digest, "words": file_hash(words_path),
                                   "visual_plan": options.get("visual_plan"),
                                   "storyboard": file_hash(storyboard_path),
                                   "layout": [settings.VISUAL_LAYOUT, settings.VIDEO_WIDTH, settings.VIDEO_HEIGHT],
                                   "fps": settings.VIDEO_FPS,
                                   "code": code_hash("fetch_images", "stock_video", "gemini_client", "visual_assets", "commons_images", "storyboard", "story_graphics", "motion_graphics", "editorial_motion")}, create_images)
            segments = read_json(segments_path)
            if images_enabled:
                if not segments and settings.VISUAL_LAYOUT != "story" and not fact.get("storyboard"):
                    raise RuntimeError("Saved image stage is empty. Resume with --refresh-images to find visuals, or --no-images for gameplay only.")
                print(f"  Visuals: {len(segments)} scenes, {len({s['image_path'] for s in segments})} assets", flush=True)
                for warning in read_json(image_report_path).get("quality", {}).get("warnings", []):
                    print(f"  Visual review: {warning}", flush=True)
            # Reject edited manifests referring to files outside this run.
            for segment in segments:
                image_path = Path(segment["image_path"]).resolve()
                if not image_path.is_relative_to(run_dir):
                    raise ValueError("Image segments must refer to files inside their run directory")
            captions_path = run_dir / "captions.ass"
            srt_path = run_dir / "captions.srt"
            def create_captions():
                build_ass_captions(timed_words, captions_path, style=options["caption_style"], image_segments=segments)
                build_srt_captions(timed_words, srt_path)
                return [captions_path, srt_path]
            store.stage("captions", {"words": file_hash(words_path), "style": options["caption_style"],
                                     "visuals": file_hash(segments_path), "layout": [settings.VISUAL_LAYOUT, settings.CINEMATIC_SPLIT],
                                     "font": [settings.CAPTION_FONT, settings.CAPTION_FONT_SIZE],
                                     "colors": [settings.CAPTION_COLOR, settings.CAPTION_HIGHLIGHT_COLOR, settings.CAPTION_OUTLINE_COLOR],
                                     "dimensions": [settings.VIDEO_WIDTH, settings.VIDEO_HEIGHT],
                                     "code": code_hash("build_captions")}, create_captions)
            final_path = run_dir / "video.mp4"
            def create_video():
                from pipeline.render_video import render_final_video
                render_final_video(background_path, audio_path, captions_path, final_path, image_segments=segments or None)
                actual = get_duration_seconds(final_path)
                if abs(actual - duration) > 0.3:
                    raise RuntimeError(f"Rendered duration {actual:.2f}s does not match narration {duration:.2f}s")
                return [final_path]
            store.stage("render", {"background": file_hash(background_path), "audio": audio_digest,
                                   "captions": file_hash(captions_path), "segments": file_hash(segments_path),
                                   "images": {s["image_path"]: file_hash(Path(s["image_path"])) for s in segments},
                                   "layout": [settings.VISUAL_LAYOUT, settings.CINEMATIC_SPLIT, settings.VIDEO_WIDTH, settings.VIDEO_HEIGHT, settings.VIDEO_FPS, settings.IMAGE_WIDTH, settings.IMAGE_Y_POS, settings.IMAGE_FADE_DURATION],
                                   "code": code_hash("render_video", "select_background")}, create_video)
            if not demo:
                from pipeline.youtube_package import prepare_package
                store.stage("youtube_package", {"video": file_hash(final_path), "fact": fact_digest,
                            "visuals": file_hash(segments_path), "privacy": privacy,
                            "language": settings.YOUTUBE_LANGUAGE, "category": settings.YOUTUBE_CATEGORY_ID,
                            "code": code_hash("youtube_package", "upload_youtube")},
                            lambda: prepare_package(run_dir, fact, segments, privacy))
                from pipeline.skill_workflow import prepare_skill_workflow
                store.stage("youtube_skills", {"fact": fact_digest, "captions": file_hash(srt_path),
                                               "code": code_hash("skill_workflow")},
                            lambda: prepare_skill_workflow(run_dir, fact, srt_path))
            instagram_export_error = None
            if not demo:
                from pipeline.publish_snapchat import prepare_package as prepare_snapchat_package
                if snapchat_enabled:
                    prepare_snapchat_package(run_dir, fact, segments)
                from pipeline.publish_instagram import prepare_package as prepare_instagram_package
                try:
                    prepare_instagram_package(run_dir, fact, segments)
                    store.save(instagram_export_error=None)
                except Exception as exc:
                    instagram_export_error = str(exc)
                    store.save(instagram_export_error=instagram_export_error)
                    print(f"Instagram export unavailable: {exc}")
            from pipeline.review import write_review
            from pipeline.engagement import prepare_experiment
            prepare_experiment(run_dir, fact, duration, options, settings.VISUAL_LAYOUT)
            write_review(run_dir, fact, duration=duration, image_segments=segments)
            store.save(status="complete", current_stage=None, duration_seconds=duration, video="video.mp4", upload_error=None)
            publishing_errors = [f"Instagram export: {instagram_export_error}"] if (instagram_enabled or facebook_enabled) and instagram_export_error else []
            if upload and upload_issues:
                store.save(status="upload_pending", upload_error="; ".join(upload_issues))
                print("Video saved locally. YouTube upload pending setup.")
            elif upload:
                from pipeline.publish_youtube import publish_short
                try:
                    result = publish_short(final_path, fact, privacy=privacy, image_segments=segments,
                                           receipt=run_dir / "youtube.json")
                    store.save(youtube_url=result["url"])
                except Exception as exc:
                    publishing_errors.append(f"YouTube: {exc}")
                    store.save(upload_error=str(exc))
            if snapchat_enabled:
                from pipeline.publish_snapchat import setup_issue, publish as publish_spotlight
                issue = setup_issue()
                if issue:
                    store.save(status="upload_pending", snapchat_error=issue)
                    print(f"Spotlight pending setup: {issue}\nManual upload instructions: {run_dir / 'snapchat_upload.txt'}")
                else:
                    try:
                        result = publish_spotlight(run_dir)
                        store.save(snapchat_id=result["spotlight_id"], snapchat_error=None)
                        print(f"Spotlight submitted: {result['spotlight_id']}")
                    except Exception as exc:
                        publishing_errors.append(f"Snapchat: {exc}")
                        store.save(snapchat_error=str(exc))
            if instagram_enabled and not instagram_export_error:
                from pipeline.publish_instagram import setup_issue as instagram_setup_issue, publish as publish_reel
                issue = instagram_setup_issue()
                if issue:
                    store.save(status="upload_pending", instagram_error=issue)
                    print(f"Instagram pending setup: {issue}")
                else:
                    try:
                        result = publish_reel(run_dir)
                        store.save(instagram_id=result["media_id"], instagram_error=None)
                        print(f"Instagram Reel published: {result['media_id']}")
                    except Exception as exc:
                        publishing_errors.append(f"Instagram: {exc}")
                        store.save(instagram_error=str(exc))
            if facebook_enabled and not instagram_export_error:
                from pipeline.publish_facebook import setup_issue as facebook_setup_issue, publish as publish_facebook
                issue = facebook_setup_issue()
                if issue:
                    store.save(status="upload_pending", facebook_error=issue)
                    print(f"Facebook pending setup: {issue}")
                else:
                    try:
                        result = publish_facebook(run_dir)
                        store.save(facebook_id=result["video_id"], facebook_error=None)
                        print(f"Facebook Reel published: {result['video_id']}")
                    except Exception as exc:
                        publishing_errors.append(f"Facebook: {exc}")
                        store.save(facebook_error=str(exc))
            if publishing_errors:
                raise RuntimeError("; ".join(publishing_errors))
            print(f"\nVideo ready: {final_path}\nReview: {run_dir / 'review.html'}")
            return final_path
        except BaseException as exc:
            store.save(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            print(f"\nProgress saved. Resume with:\n  {resume_command(run_dir)}", file=sys.stderr)
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Produce and, by default, publicly upload recoverable fact shorts.", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python main.py --doctor\n  python main.py --demo\n  python main.py --category 'space and astronomy' --script-only\n  python main.py --resume output/<run>\n  python main.py --script examples/fact.json --background gameplay.mp4\n  python main.py --resume output/<run> --upload --privacy private")
    parser.add_argument("--category", help="Topic for a generated script")
    parser.add_argument("--script", type=Path, help="Use your own fact.json instead of Gemini")
    parser.add_argument("--audio", type=Path, help="Use recorded narration instead of TTS")
    parser.add_argument("--words", type=Path, help="Word timestamps JSON for --audio; skips Whisper")
    parser.add_argument("--background", type=Path, help="Use a specific footage file")
    parser.add_argument("--visual-plan", type=Path, help="Use a local JSON storyboard with timed images and headlines")
    parser.add_argument("--experiment", help="Name a single-change engagement experiment, e.g. shorter-scripts")
    parser.add_argument("--variant", help="Label this run, e.g. baseline or shorter; saved for later metric comparisons")
    parser.add_argument("--resume", type=Path, help="Continue a saved run; verified completed stages are reused")
    parser.add_argument("--output-dir", type=Path, help="Parent directory for new runs")
    image_mode = parser.add_mutually_exclusive_group()
    image_mode.add_argument("--no-images", action="store_true", default=None, help="Skip optional fact visuals and use full-screen gameplay")
    image_mode.add_argument("--images", dest="no_images", action="store_false", help="Enable optional images, including on a previously disabled run")
    parser.add_argument("--refresh-images", action="store_true", help="Repeat image acquisition for a resumed run")
    parser.add_argument("--caption-style", choices=["phrase", "word"], help="Phrase highlight (default) or one word at a time")
    parser.add_argument("--script-only", action="store_true", help="Save a script for review before generating media")
    parser.add_argument("--demo", action="store_true", help="Render an 8-second silent demo completely offline")
    parser.add_argument("--doctor", action="store_true", help="Check tools, dependencies, credentials and footage without network calls")
    parser.add_argument("--list-categories", action="store_true", help="Print the built-in topic categories")
    upload_mode = parser.add_mutually_exclusive_group()
    upload_mode.add_argument("--upload", action="store_true", default=None, help="Upload the finished video to YouTube (default)")
    upload_mode.add_argument("--no-upload", dest="upload", action="store_false", help="Keep this run local and override automatic upload")
    instagram_mode = parser.add_mutually_exclusive_group()
    instagram_mode.add_argument("--instagram", action="store_true", default=None, help="Also publish an Instagram Reel when API and media hosting are configured")
    instagram_mode.add_argument("--no-instagram", dest="instagram", action="store_false", help="Disable Instagram publishing for this run")
    facebook_mode = parser.add_mutually_exclusive_group()
    facebook_mode.add_argument("--facebook", action="store_true", default=None, help="Also publish a Facebook Page Reel")
    facebook_mode.add_argument("--no-facebook", dest="facebook", action="store_false", help="Disable Facebook publishing for this run")
    parser.add_argument("--check-meta", action="store_true", help="Check Meta credentials and media hosting without publishing")
    snapchat_mode = parser.add_mutually_exclusive_group()
    snapchat_mode.add_argument("--snapchat", action="store_true", default=None, help="Also post to Spotlight when approved API credentials are configured")
    snapchat_mode.add_argument("--no-snapchat", dest="snapchat", action="store_false", help="Disable Spotlight publishing for this run")
    parser.add_argument("--connect-youtube", action="store_true", help="Connect and display your channel without uploading a video")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], help="YouTube privacy; defaults to configured value, otherwise private")
    args = parser.parse_args(argv)
    if args.list_categories:
        print("\n".join(settings.FACT_CATEGORIES))
        return 0
    if args.doctor:
        from pipeline.doctor import check_environment, print_checks
        enabled = args.upload if args.upload is not None else settings.AUTO_UPLOAD_YOUTUBE
        checks = check_environment(upload=enabled, need_graphics=True)
        from pipeline import publish_instagram, publish_facebook
        for name, active, module in [("Instagram", args.instagram if args.instagram is not None else settings.AUTO_UPLOAD_INSTAGRAM, publish_instagram),
                                     ("Facebook", args.facebook if args.facebook is not None else settings.AUTO_UPLOAD_FACEBOOK, publish_facebook)]:
            if active and args.upload is not False:
                issue = module.setup_issue()
                checks.append({"name": name + " configuration", "ok": not issue,
                               "detail": issue or "Configured locally; run --check-meta for live validation", "required": True})
        return 0 if print_checks(checks) else 1
    try:
        if args.check_meta:
            from pipeline.meta_check import check
            return check()
        if args.connect_youtube:
            from pipeline.publish_youtube import connect_channel
            connect_channel()
            return 0
        options = vars(args)
        options.pop("doctor")
        options.pop("check_meta")
        options.pop("list_categories")
        options.pop("connect_youtube")
        run(**options)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted; completed stages are saved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
