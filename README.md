# Fact Shorts Automation

Turn a researched script into a vertical video with expressive narration, subject footage, animated explanations, and highlighted captions. Generate a draft with Gemini or bring your own script, recording, timestamps, and visual storyboard.

Runs are recoverable: each stage saves its outputs and checksums. Resume after a failure without repeating valid work, review the finished video locally, then explicitly upload it if desired.

## Start here

Requires Python 3.10+ and FFmpeg with **libass** (subtitles) and **libx264**.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On macOS, `brew install ffmpeg-full` provides a subtitle-capable build. If another FFmpeg is already installed, select this build using PATH rather than overwriting it:

```bash
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
```

On Ubuntu/Debian, install FFmpeg with `sudo apt-get install ffmpeg`. On Windows, install a build with libass and add its `bin` directory to PATH; activate the environment with `.venv\Scripts\activate`.

**Test rendering without credentials, downloads, or footage:**

```bash
python main.py --demo
```

This creates an eight-second **silent test video** with synthetic moving footage. It exercises background looping, phrase captions, encoding, checkpoints, and the review page. Only Python and FFmpeg are required for this demo.

For production, add `GEMINI_API_KEY` to `.env` and put your own or licensed footage in `assets/gameplay/`, or pass `--background /path/to/footage.mp4`. `.env` loads automatically; no shell exporting is necessary.

```bash
python main.py --doctor
python main.py --category "space and astronomy"
```

If you already have the project's `venv/` environment, use `source venv/bin/activate`
or run `venv/bin/python main.py` directly. Virtual environments contain absolute paths;
moving the project can break activation and installed commands. If `python` is unavailable
after activation, the direct interpreter can still work. For a portable fresh setup,
create `.venv` at the new location using the installation commands above.

`--doctor` checks the local tools, dependencies, key presence, and footage without contacting providers. It cannot confirm that an API key or model is accepted by a remote service.

## YouTube creator skills

This project includes the local YouTube skills in [`.github/skills/`](.github/skills/). Use them
before publishing when you want an editorial pass in addition to the automated pipeline:

| Skill | Use |
| --- | --- |
| `yt-script` | Turn a topic into scored hooks and a spoken script |
| `yt-package` | Check the title and thumbnail as one click-through package |
| `yt-seo` | Prepare search-facing description and tags |
| `yt-edit` | Find dead air, filler, and retakes in a transcript |
| `yt-chapters` | Create YouTube-valid chapters from a transcript |
| `yt-retention` | Diagnose leaks and cliffs from a Studio export |
| `yt-shorts` | Find short-form clips in a long recording |
| `yt-comment` | Triage comments and draft replies |
| `yt-plan` | Plan uploads around available capacity |
| `yt-viral` | Compare topic and title patterns against channel baselines |
| `yt-audit` | Audit packaging, openings, consistency, and the next priority |

The skills do not publish by themselves. The production pipeline remains the explicit publishing
gate. For a new short, generate and review it first:

```bash
venv/bin/python main.py --category "space and astronomy" --no-upload
venv/bin/python main.py --resume output/<run-directory> --upload --privacy private
```

By default, ordinary `main.py` production runs now execute the deterministic parts of the bundled
skills, save `skill_workflow.json`, and upload publicly to YouTube. Use `--no-upload` to keep a run
local or set `AUTO_UPLOAD_YOUTUBE=false` and `YOUTUBE_PRIVACY_STATUS=private` in `.env` to restore
review-first behavior. Existing uploads are recorded in each run's `youtube.json`.

## Review first, then produce

Generate just the script:

```bash
python main.py --category "deep ocean exploration" --script-only
```

Open the saved `fact.json`, check the claims against its source references, and edit the narration. Then continue using the run directory printed by the command:

```bash
python main.py --resume "output/<run-directory>"
```

Gemini drafts carry `review_status: "needs_review"`, model information, and source links. Those references are **model-supplied and unverified**: this project validates their structure, not whether they exist or support the claim. Source review remains a human step. Stock images are illustrative, not documentary evidence.

Every new run uses a timestamp and random suffix, so repeated titles never overwrite earlier videos. Failed runs return a nonzero exit code and print their resume command. A per-run process lock prevents concurrent writers; it releases automatically when the process exits.

## Use your own inputs

```bash
# Bypass script generation; narration still uses the selected speech provider.
# The example is a workflow tutorial, not a researched fact.
python main.py --script examples/fact.json --background /path/to/gameplay.mp4

# Use your own recording instead of text-to-speech.
python main.py --script my-fact.json --audio voiceover.wav --no-images

# Fully offline production when script, audio, timestamps, and footage are local.
python main.py --script my-fact.json --audio voiceover.wav \
  --words words.json --background gameplay.mp4 --no-images
```

A script file must contain `title`, `description`, `tags` (a nonempty string array), and `fact_text` (10–400 words). Optional `category`, `sources`, and `storyboard` fields are supported. Each source has `title`, `url`, and `claim`. See [examples/fact.json](examples/fact.json). Generated scripts target **80–105 words**, as a **30–40 second experiment**; duration is measured from audio rather than assumed from word count. Longer imported scripts still work. This pipeline limits narration to 180 seconds.

Generated drafts target 8–12 story beats. Each beat has a `role` (`hook`, `setup`, `explain`, `reveal`), a `narration` excerpt, and a `visual` with `kind`, `headline`, `query`, and `labels`. The application assembles `fact_text` directly from generated beats so Gemini does not need to reproduce the narration twice. Beat narration must concatenate to exactly the full `fact_text`; if you edit the script, update its beats too (or remove `storyboard` to use sentence-based visuals). The opening asks a concrete factual question or invites a prediction; the ending gives its answer without a generic recap. Sources and visual labels still need factual review.

Drafts outside the recommended word count or title length carry editorial review notes in the console, `fact.json`, and `review.html`. These targets do not discard an otherwise valid draft; structural validation and the measured audio duration limit still apply.

Gemini 2.x uses its compatible JSON-schema request fields. If a draft still fails structural validation (for example, a source is missing its `claim`), the pipeline requests one correction and validates again. It stops with a specific error if the corrected draft is still invalid. Source claims appear beside their references on the review page; they remain unverified.

A word timing file is a JSON array:

```json
[
  {"word": "A", "start": 0.0, "end": 0.2},
  {"word": "clear", "start": 0.2, "end": 0.5},
  {"word": "idea.", "start": 0.5, "end": 1.0}
]
```

The times must match the provided recording. Small ASR timing overlaps are normalized; invalid, empty, or out-of-order timestamps fail with an explanation. If prompted Whisper transcription misses much of the script or starts late, one local unprompted retry selects the transcript with better script coverage. Imported timestamps are preserved. Estimated storyboard alignment is flagged for review.

## Outputs and recovery

Each run contains:

| Artifact | Purpose |
| --- | --- |
| `video.mp4` | Vertical H.264/yuv420p video, AAC audio, fast-start playback |
| `review.html` | Local video player, narration, source references, image credits, downloads |
| `fact.json` | Normalized script, source references, generation provenance |
| `captions.ass` / `captions.srt` | Animated burn-in captions and portable subtitle track |
| `words.json` | Word-level timestamps |
| `narration.*` / `background.mp4` | Reusable media intermediates |
| `narration.json` | Actual speech provider, voice, delivery settings, and audio checksum |
| `image_segments.json` / `images/` | Subject footage, graphics, timing, creator and source metadata |
| `image_report.json` | Planned searches, selected scene count, and unique image count |
| `storyboard.json` | Story beats aligned to the actual narration, including estimated timings |
| `experiment.json` / `engagement.json` | Experiment labels, actual video identity, and manually entered Studio metrics |
| `run.json` | Stage status, elapsed time, input signatures, output checksums |
| `youtube.json` | Upload result or interrupted-upload record, when requested |

Open `review.html` in a browser to inspect the result. Sources and credits link to their original pages. No local server is required.

```bash
python main.py --resume "output/<run-directory>"
python main.py --resume "output/<run-directory>" --caption-style word
python main.py --resume "output/<run-directory>" --no-images
python main.py --resume "output/<run-directory>" --refresh-images
```

Resume verifies output checksums and relevant settings/code before reusing a stage. Missing or modified generated artifacts are rebuilt. Editing the run's `fact.json` changes dependent stages. Imported audio and word files remain the authoritative recording and timestamps even if you change the script; start a new run to replace those inputs. Keep the original imported files available in case an intermediate needs rebuilding. Use a new run for different input files or a different category.

In the default `story` layout, missing subject visuals become brief kinetic-text passages (up to six words per passage), not paragraph slides. The review reports moving-media, text-led, and missing-visual percentages separately. A fully covered timeline can still be mostly text; the report flags that instead of implying that the visuals are good. Long scenes over four seconds and estimated alignment also appear in `image_report.json` and `review.html`. Legacy `cinematic`/`cards` runs without a storyboard still stop if no images download. Use `--refresh-images` to retry, `--images` to enable visuals, or explicitly choose `--no-images` for gameplay only.

Use a **new run** for a revised version of an already uploaded video; its publishing receipt protects against accidentally uploading a different edit as the same artifact. Existing runs created by the original version have no checkpoints; reuse their script with `--script output/<old-run>/fact.json`.

## Customize

| Option | Default / behavior |
| --- | --- |
| `--list-categories` | Show built-in categories |
| `--no-images` | Disable NASA/Pexels/Commons images and use full-screen gameplay |
| `--visual-plan PATH` | Use a local JSON storyboard instead of image searches |
| `VISUAL_LAYOUT` | `story` (default): full-screen subject footage and animated explanations; local plans can request split-screen. `cinematic`: fixed split. `cards`: original overlays |
| `--experiment NAME`, `--variant NAME` | Save labels for a controlled editorial experiment; do not change content automatically |
| `--caption-style phrase` | Up to four words, current word highlighted in gold |
| `--caption-style word` | Original one-word pop animation |
| `--output-dir PATH` | Set the output parent for a new run |
| `GEMINI_MODEL` | `gemini-3.8-flash`; explicit model, no silent fallback |
| `TTS_PROVIDER` | `gemini`; alternatives: `edge`, `gtts` |
| `GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE` | `gemini-3.8-flash-tts`, `Achird` (friendly) |
| `TTS_STYLE` | Warm conversational delivery, connected phrasing, varied intonation, brief pauses |
| `TTS_FALLBACK_PROVIDER` | `none`; explicitly select `edge` or `gtts` to allow a fallback |
| `TTS_VOICE`, `TTS_RATE`, `TTS_PITCH` | Edge only: `en-US-AndrewNeural`, `+0%`, `+0Hz` |
| `WHISPER_MODEL_SIZE` | `base`; local CPU transcription |
| `VIDEO_WIDTH`, `VIDEO_HEIGHT`, `VIDEO_FPS` | `1080`, `1920`, `30`; dimensions must be even |
| `CAPTION_FONT`, `CAPTION_FONT_SIZE` | Arial Black; size scales with output width |
| `GAMEPLAY_DIR`, `OUTPUT_DIR` | Paths relative to the project root unless absolute |

For faster previews: `VIDEO_WIDTH=360 VIDEO_HEIGHT=640 python main.py --demo`. Install your preferred caption font on the machine; FFmpeg uses a substitute if it is unavailable.

### Visual storytelling

The default `story` layout gives the subject the full frame. Stock searches try **moving Pexels footage** before photos for ordinary concrete subjects; scientific species, space, and historical subjects retain their specialist archive routes. Search titles must match the requested subject, downloads are bounded, and creator credits are retained. Repeated footage queries can select a second angle, and subsequent shots advance through each clip instead of restarting it. Search matching is a heuristic, so inspect whether the selected footage illustrates the claim. Pexels API reference: https://www.pexels.com/api/documentation/.

Still photos use alternating push, pull, and pan moves with shots no longer than 3.5 seconds. Diagrams remain fully visible. Generated questions, comparisons, and sequences are encoded as moving clips. Missing subject visuals leave gameplay visible instead of adding large narration cards. Burned-in captions show at most two words at a time, with no extra opening headline; source labels and essential diagram labels remain. Generated scripts request 8–12 short beats, concrete 2–4 word searches, and few generic text scenes. Local plans can use JPG/PNG/WebP or MP4/MOV/WebM and can explicitly request `split` layout. Clip audio is discarded; narration remains the final soundtrack.

Storyboards support `photo` (footage or photography), `question`, `comparison`, `sequence`, and `animation`. Subject animations currently include:

- `golf_airflow`: illustrated balls, airflow, and wakes; views `question`, `overview`, `smooth`, `drag`, `dimples`, `attached`, `wake`, `comparison`.
- `memory_list`: an illustrated list and object close-ups; views `question`, `overview`, `primacy`, `recency`, `middle`, `comparison`. Recognized serial-position scripts automatically use this demonstration even if an older storyboard asked for generic memory photos. Try `python main.py --script examples/memory-story.json --no-upload`.

These are qualitative illustrations, not measured experiments. Generic motion graphics do not invent numerical charts or claims. Captions remain in a lower safe area and move to the seam for explicit split scenes. Set `VISUAL_LAYOUT=cinematic` for a fixed split or `cards` for original overlays; an existing `.env` setting takes precedence.

Sources stay alive through their gated endpoint, and cuts share the output frame grid, preventing fractional-frame gameplay flashes. The report describes planned coverage, not a decoded-frame guarantee.
Try the 86-word golf example with local imagery and no upload:

```bash
VISUAL_LAYOUT=story python main.py --script examples/golf-story.json \
  --visual-plan assets/visuals/golf-engagement/visual-plan.json \
  --background assets/gameplay/minecraft-parkour.mp4 --no-upload \
  --experiment story-format --variant story-v1
```

This example still calls the configured speech provider and local Whisper. It demonstrates a factual prediction, photo context, progressive explanations, and a comparison at the payoff. The saved review page has clickable beat timestamps.

The animated revision reuses the saved narration and timestamps, so it needs no model calls:

```bash
VISUAL_LAYOUT=story python main.py --script examples/golf-motion-story.json \
  --audio assets/visuals/golf-engagement/narration.mp3 \
  --words assets/visuals/golf-engagement/words.json \
  --visual-plan assets/visuals/golf-engagement/visual-plan.json \
  --background assets/gameplay/minecraft-parkour.mp4 --no-upload
```

Animation beats use `kind: "animation"`, `template: "golf_airflow"`, a `view`, a short `headline`, and empty `query`/`labels`. They produce local MP4 scene assets; `image_segments.json` retains its historical name and marks them with `media_type: "video"`. The regression suite decodes every frame across fractional cuts, including still/video and full/split transitions, to catch missing overlays that timeline coverage alone cannot see.

Space topics use the [NASA Image and Video Library](https://images.nasa.gov/), without an API key. When a specific scene has no match, supported space subjects can reuse a NASA image of the main subject. Other topics use Pexels when `PEXELS_API_KEY` is configured. Selection checks overlap between the requested subject and the provider's title/description; weak or unlabeled results are skipped. This is a text-based relevance check, so review the actual pictures before publishing. NASA artist concepts are labeled on screen; credits and source links are saved in the review page.

History and archaeology topics first look for the named subject on Wikimedia Commons; Commons also serves as a fallback for other non-space topics. It needs no API key and can provide actual artifact photographs when stock search only has generic machinery. Each search is bounded and downloads a small pool of matching raster images. The automatic pool accepts public-domain, CC0, and CC BY assets with attribution metadata. Author/license credits appear on screen; the review page includes source and license links and crop/resize details. Reconstructions identified by their titles are labeled as models.

Species explicitly named in the narration use their scientific name for archive searches and skip generic stock imagery that could show a different species or habitat. For other subjects, if the shared archive search returns no downloadable images, the pipeline also tries each scene's query. If too few original images download, one batch of smaller archive thumbnails can supply additional photos. Failed URLs and successfully downloaded photos are not repeated within that search. `image_report.json` records the archive queries, accepted candidate counts, and successful downloads to help diagnose missing visuals.

For precise editorial control, pass `--visual-plan` with 1–24 scenes:

```json
{
  "scenes": [
    {"start": 0, "end": 4.2, "image_path": "spacecraft.jpg", "headline": "AN INVISIBLE BORDER", "fit": "cover"},
    {"start": 4.2, "end": 10, "image_path": "diagram.png", "headline": "THE CLUE", "fit": "contain"}
  ]
}
```

Image paths are relative to the plan. `cover` fills the scene; `contain` preserves diagrams against a plain dark background. Optional `layout: "full"` or `"split"` controls each scene in story mode; without it, the first and last imported scenes use full screen. For scripts with structured beats, planned graphics take priority over photos during their own passages. The first photo scene's headline supplies the opening hook; other photo headlines remain in the storyboard metadata. Times are seconds in the narration and must be ordered without overlap. The plan and image checksums are recorded so changed inputs cannot silently alter a resumed run. Keep the original inputs available.

### Engagement experiments

The tracker stores **real metrics you enter from YouTube Studio**. It does not request Analytics permissions or infer performance from an upload. Start by changing one variable across similar topics, such as opening style, duration, or layout. Use the same observation window after publication for both variants. The initial `story-v1` combines several improvements and is a format preview, not an isolated test of any one change.

Tag existing videos without re-rendering them:

```bash
python -m pipeline.engagement label "output/<old-run>" \
  --experiment shorter-scripts --variant baseline \
  --changed-variable duration --hypothesis "A shorter explanation retains more viewers"
python -m pipeline.engagement label "output/<new-run>" \
  --experiment shorter-scripts --variant shorter
```

Enter the first 48 hours' Studio metrics (the numbers below are illustrative, not observed results):

```bash
python -m pipeline.engagement record "output/<run>" --window-hours 48 \
  --stayed-to-watch 65 --average-percentage-viewed 85 \
  --average-view-duration 29.75 --engaged-views 1000 --retention-drop 12.5
python -m pipeline.engagement compare --output-dir output \
  --experiment shorter-scripts --baseline baseline
```

Percentages are entered on a 0–100 scale; average percentage viewed may exceed 100 when viewers replay. Average duration and retention-drop timestamps use seconds. Repeating a window corrects that snapshot; another window adds a snapshot. Comparisons show video counts, median duration, median engagement metrics and differences against the baseline **within the same window**. They are descriptive comparisons, not randomized A/B tests or proof of causation. Use stayed-to-watch for opening experiments and retention-drop timestamps to inspect the relevant story beat. The [YouTube metric definitions](https://support.google.com/youtube/answer/12220281?hl=en) explain these measures.

Metrics are tied to the rendered video's checksum. Re-rendering a measured video archives its previous metric snapshots and starts an empty record for the new edit. The review page shows recorded metrics after `record` or the next resume. No audience metrics are fabricated.

The included Voyager example has a 43-second narration, nine curated scenes, and NASA credits:

```bash
venv/bin/python main.py --script assets/visuals/voyager/fact.json \
  --audio assets/visuals/voyager/narration.mp3 --words assets/visuals/voyager/words.json \
  --visual-plan assets/visuals/voyager/visual-plan-balanced.json \
  --background assets/visuals/voyager/gameplay.mp4
```

These assets live outside `output/`, so deleting generated runs does not remove the reusable example. Its story follows [NASA's account of the interstellar crossing](https://www.nasa.gov/news-release/nasa-spacecraft-embarks-on-historic-journey-into-interstellar-space/).

Narration defaults to [Gemini expressive speech](https://ai.google.dev/gemini-api/docs/generate-content/speech-generation), using the existing `GEMINI_API_KEY`. Delivery directions are separate from the verbatim script, so they are not read aloud. Audio is generated as a continuous passage, validated, and normalized for consistent loudness before conversion to MP3. Generated scripts favor contractions, connected ideas, and sentences that are easy to say aloud.

Set `TTS_STYLE` in `.env` to adjust the delivery, for example `Warm, curious, relaxed storytelling with gentle emphasis and brief sentence pauses.` The style is guidance, not an exact duration guarantee. Changing the provider, voice, or style and resuming a run regenerates narration and its dependent captions and video. The actual provider settings are saved in `narration.json`.

`TTS_PROVIDER=edge` uses Edge neural speech; `TTS_PROVIDER=gtts` explicitly chooses the simpler English voice. A failed expressive voice stops the run by default. To allow a different voice after a failure, set `TTS_FALLBACK_PROVIDER=edge` or `gtts`; fallback use is printed and recorded. All online speech providers use bounded retries. Existing completed videos stay unchanged until you resume them.

Whisper runs locally but downloads its chosen model on first use. Gemini and Pexels require internet and provider credentials; availability, quotas, and charges depend on your provider accounts. Generated scripts and image queries use a bounded REST client with structured output. The configured model follows the [Gemini lifecycle reference](https://ai.google.dev/gemini-api/docs/deprecations), and output handling follows [Gemini's structured-output API](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

If Gemini returns **503**, the service is temporarily unavailable. Text requests display the active model and retry progress, with up to three attempts and increasing delays with jitter. Server-provided `Retry-After` waits are honored up to 60 seconds; longer waits stop immediately with an explanation so you can resume later. A **429** is reported separately as a quota or rate limit. These errors do not require reconnecting YouTube.

For persistent overload on `gemini-3.8-flash`, you can explicitly set `GEMINI_MODEL=gemini-3.5-flash-lite` in `.env`; [Flash-Lite supports structured output](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite). Availability and quota still depend on your account. The program never silently changes models. Restart the command after changing `.env`, and use the printed `--resume` command to continue the same run. The speech model is configured separately with `GEMINI_TTS_MODEL`.

## Automatic YouTube Shorts uploads

```bash
venv/bin/python -m pip install -r requirements-youtube.txt
```

Connect your channel once:

1. Open [Google Cloud Console](https://console.cloud.google.com/apis/library/youtube.googleapis.com), select or create your project, and enable **YouTube Data API v3**.
2. Configure **Google Auth Platform** for your account. If the app is in testing, add your Google account as a test user.
3. Create an OAuth client with application type **Desktop app**. Download its JSON and save it as `client_secret.json` in this project, next to `main.py`. A Gemini API key is not a substitute for YouTube OAuth credentials.
4. Run the connection command, sign in, and select the YouTube channel you want to use. Check the channel name printed in the terminal.

```bash
venv/bin/python main.py --connect-youtube
```

This command does not upload a video. It saves `token.json` with owner-only permissions and records the channel in `youtube_channel.json`. New connections request video-upload and channel-read permissions. Older compatible tokens continue to work. The uploader checks the saved channel before every upload. To switch accounts, move the old token aside, run `--connect-youtube`, and choose the new account/channel.

Enable automatic delivery in `.env`:

```dotenv
AUTO_UPLOAD_YOUTUBE=true
YOUTUBE_PRIVACY_STATUS=public
YOUTUBE_LANGUAGE=en
```

Then run the pipeline normally:

```bash
venv/bin/python main.py --category "space and astronomy"
```

Every completed production run uploads only the final video, with its title, description, relevant hashtags, source references, and image credits. Animated captions are already part of the video. No custom thumbnail or separate caption track is generated or uploaded.

The local `review.html` previews the metadata saved in `youtube_metadata.json`. These publishing features do not make extra Gemini calls. There is no guarantee of virality; the title and hashtags describe the actual story rather than unrelated trends.

Hashtags favor supplied tags that appear as complete phrases in the title and narration, followed by other supplied topic tags. Up to three topic hashtags plus `#Shorts` are included; generic filler is skipped and promotional tags such as `viral` and `fyp` are removed from upload tags as well. Generated tags are requested in specific-to-broad order. This is a local relevance heuristic, not live trend research; review supplied tags for relevance before publishing.

Generated storyboards request subject photos/footage for at least 80% of beats, including the opening, with at most one generic diagram/text beat. Pexels photo searches consider 15 candidates across all orientations. Missing non-space, non-species scenes get one batch of alternative concrete searches; this can add one Gemini planning call when configured. Exact-species archive matching and NASA subject fallbacks remain in place. Unresolved gaps show gameplay with short captions.

When automatic uploading is enabled but YouTube credentials or dependencies are missing, video creation still finishes locally. The run is saved as `upload_pending`, and the terminal prints the setup instructions and resume command. After connecting, resume that run to upload it; older pending videos are not uploaded in the background. An explicit `--upload` or `--doctor --upload` still checks upload prerequisites before proceeding.

**Public** uploads are visible to everyone. The template defaults to private until you configure another visibility. `--upload` enables a single run; `--no-upload` keeps a run local. Demo and script-only runs never upload automatically. The selected upload mode and visibility are saved with each production run and retained on resume; use `--privacy public` to override an older run's saved visibility.

```bash
# Render locally, including upload metadata.
venv/bin/python main.py --category "space and astronomy" --no-upload

# Upload an existing finished run, or retry its remaining publishing steps.
venv/bin/python main.py --resume "output/<run-directory>" --upload --privacy public

# Read-only setup checks.
venv/bin/python main.py --doctor --upload
```

Successful receipts identify the video by SHA-256 and prevent a repeat upload. If a connection fails after submission, an uncertain receipt blocks blind retries: check your YouTube channel first, then use the standalone uploader's explicit override only if another upload is needed:

```bash
venv/bin/python pipeline/upload_youtube.py --video "output/<run-directory>/video.mp4" \
  --fact "output/<run-directory>/fact.json" --privacy private --force-upload
```

`video.mp4` shares the same `youtube.json` receipt in both CLIs. Other filenames use `<stem>.youtube.json`, or a custom `--receipt` path. A stale `.lock` from an interrupted upload must be removed only after confirming that no upload process remains. Successful local rendering is retained when upload fails. Reusing a successful receipt does not change an existing video's privacy or metadata; edit those in YouTube Studio. Shorts classification is determined by YouTube; a hashtag does not guarantee it. Before uploading, the pipeline checks that the video is square or vertical, has audio, and is at most three minutes long, following [YouTube's Shorts criteria](https://support.google.com/youtube/answer/15424877?hl=en).

API references: [video uploads](https://developers.google.com/youtube/v3/docs/videos/insert), [channel identity](https://developers.google.com/youtube/v3/docs/channels/list). Google account authorization and YouTube API quota must remain available for unattended runs; if consent expires, run `--connect-youtube` again.

## Development and checks

```bash
python -m unittest discover -v
```

The test suite covers validation, provider retries with mocked responses, image-download limits and attribution, checkpoint recovery, duplicate upload protection, caption timing, and real FFmpeg renders. Media integration tests skip when FFmpeg/subtitle support is unavailable. Tests never authenticate, upload videos, or call live generation services.

The code is organized around `main.py` (workflow), `pipeline/run_store.py` (checkpoints), `pipeline/doctor.py` (preflight), and the individual stages in `pipeline/`. Configuration lives in `config/settings.py`. YouTube dependencies are optional; help, offline demos, and local script validation do not import them.

## Snapchat Spotlight alongside YouTube

Every completed production run now writes `snapchat_metadata.json` and
`snapchat_upload.txt`. Use the original `video.mp4` in Snapchat → Spotlight → `+`,
paste the prepared description, check whether sharing to Story is enabled, and submit.
These local exports do not publish anything. Existing YouTube behavior is retained.
To export a previously rendered run without rerendering or publishing:

```bash
python -m pipeline.publish_snapchat --run output/<run> --export
```

Automatic Spotlight publishing requires an **allowlisted Public Profile API app**.
Create a Snap Business account and OAuth app in Ads Manager → Business Details,
then request API approval through your Snap contact. An ordinary Snap Kit app does
not provide this access. See [Snap's setup guide](https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted).
Until approval is granted, use the manual exports.

After approval:

1. Install `python -m pip install -r requirements-snapchat.txt`.
2. Set `SNAPCHAT_CLIENT_ID`, `SNAPCHAT_CLIENT_SECRET`, `SNAPCHAT_PROFILE_ID`,
   and `SNAPCHAT_REDIRECT_URI` in `.env`. The redirect URI must exactly match the
   one registered with Snap. Keep secrets out of chat and version control.
3. Run `python -m pipeline.publish_snapchat --connect`. Authorize in the browser,
   then paste the final redirect URL into the hidden terminal prompt. A local
   callback server is not required; copy the URL even if its page cannot load.
   Credentials are stored in ignored, owner-readable `snapchat_token.json`.
4. Review media rights and attribution for Snapchat. The 160-character Spotlight
   description cannot hold full credits; ensure required attribution is visible
   in the video or otherwise satisfied before setting `SNAPCHAT_RIGHTS_REVIEWED=true`.
5. Run `python main.py --upload --snapchat`, or set `AUTO_UPLOAD_SNAPCHAT=true`
   for future automatic posts. Leave it false until setup is complete.

`--no-snapchat` disables Snapchat only. `--no-upload` disables both defaults unless
`--snapchat` is explicitly supplied. Demo and script-only runs never post to Snapchat.
If credentials are missing, the video and manual instructions are still saved and
Snapchat is marked pending. Platform upload failures are recorded separately;
both requested destinations are attempted even if one upload fails.

To post an existing completed video without rendering or touching YouTube:

```bash
python -m pipeline.publish_snapchat --run output/<run>
```

The [Spotlight API](https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement)
currently accepts MP4 videos of **6–60 seconds**, at least **540×960**, with a description
up to **160 characters**. The exporter reports eligibility; it never silently trims
or stretches narration. API limits can differ from the Snapchat app.

A successful submission is saved in `snapchat.json` with its Spotlight ID. Submission
is not a guarantee of distribution or monetization. If a post request times out,
the receipt is left as `submitting` and retries stop to prevent duplicate posts.
Check the profile first; only remove an uncertain receipt after confirming that
Snapchat did not create a post. Never delete a successful receipt to force a retry.

## Instagram Reels alongside YouTube

Instagram is the next publishing destination; Snapchat automatic posting is disabled
in this workspace. The Snapchat integration remains available only if explicitly configured.
New production runs prepare `instagram_reel.mp4`, `instagram_metadata.json`, and
`instagram_upload.txt`. The export copies the video stream and converts audio to
AAC at 128 kbps/48 kHz without altering captions or visuals. YouTube keeps using
`video.mp4`. Failed optional Instagram exports do not prevent YouTube publishing.

**First switch your personal Instagram account to Creator or Business.** In the
Instagram app, look under Profile → menu → Account type and tools → Switch to
professional account → Creator. Menu labels may vary. Professional accounts are
public. You can manually post the exported Reel while API setup is pending.

For automatic publishing, use Meta's **Instagram API with Instagram Login**:

1. Create a Meta developer app and configure Instagram Login. For testing your own
   account, follow the app dashboard's account-role/tester instructions and authorize
   that account. Serving other people's accounts may require App Review/advanced access.
2. Obtain an Instagram user access token with `instagram_business_basic` and
   `instagram_business_content_publish`. This integration does not use Facebook Page
   tokens or your Instagram password. The Instagram Login route does not require
   linking a Facebook Page.
3. Set `INSTAGRAM_USER_ID`, `INSTAGRAM_ACCESS_TOKEN`, and `INSTAGRAM_API_VERSION`
   in `.env`, using the supported API version shown in your Meta app. Tokens expire;
   renew them through Meta's supported flow. This initial integration does not automate
   OAuth token issuance or renewal.
4. Host the exact `instagram_reel.mp4` on HTTPS where Meta can retrieve it without
   login. Hosting/uploading to storage is **not configured automatically**. You may
   use a signed URL that stays valid throughout processing. Never expose the entire
   project folder, `.env`, or OAuth files through a web server.
5. Publish an existing run:

```bash
python -m pipeline.publish_instagram --run output/<run> --video-url 'https://your-media-host/reel.mp4'
```

For recurring runs, configure your storage sync separately and set
`INSTAGRAM_MEDIA_BASE_URL` to a public directory mirroring
`<run>/instagram_reel.mp4`. Then use:

```bash
python main.py --upload --instagram
```

Alternatively set `AUTO_UPLOAD_INSTAGRAM=true` after setup. `--no-instagram` disables
Instagram; `--no-upload` disables publishing defaults unless a destination is explicitly
enabled. Without credentials, Instagram is marked pending and local files remain available.
YouTube and Instagram uploads are attempted independently.

To prepare an existing video for manual posting without network calls:

```bash
python -m pipeline.publish_instagram --run output/<run> --export
```

For per-run URLs, you can also create `instagram_media_url.json` with `video_url` and
`sha256` (the SHA-256 of `instagram_reel.mp4`). It is rejected if the export changes.
Reel processing is polled for a bounded period, with its container saved in
`instagram.json` for resume. Completed posts are not repeated. An ambiguous final
publish request stops retries until you check the profile; do not delete receipts
unless you have confirmed that no post exists. There is no live account verification
until your credentials and account are configured.

References: [Meta Instagram Login collection](https://www.postman.com/meta/instagram/folder/6raa77c/instagram-api-with-instagram-login),
[Meta content publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/).


## YouTube + Instagram + Facebook from one run

This workspace enables `AUTO_UPLOAD_YOUTUBE=true`, `AUTO_UPLOAD_INSTAGRAM=true`, and
`AUTO_UPLOAD_FACEBOOK=true` in `.env`. New production runs attempt all three:

```sh
python main.py --check-meta   # Read-only account and hosting diagnostics
python main.py              # Generate, render, and attempt all enabled destinations
python main.py --resume output/<run> --instagram --facebook --upload
```

Each destination is attempted independently, so one failure does not prevent the
others. `--no-upload` keeps the run local unless a destination is explicitly enabled;
`--no-instagram` and `--no-facebook` disable individual destinations. Demo and
script-only runs never publish. YouTube retains its configured privacy.

Facebook requires `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN`, and
`META_API_VERSION`. The Page token needs `pages_manage_posts` and
`pages_read_engagement`; `pages_show_list` is used to discover managed Pages.
The publisher uploads the local MP4 directly to Meta, saves `facebook.json`, and
only reports published after Meta confirms the publishing phase. An uncertain
final request is reconciled by polling the same video ID on resume. Never delete
receipts to retry an uncertain submission.

Instagram supports either the existing Instagram Login credentials, or
`INSTAGRAM_LOGIN_MODE=facebook` to use `FACEBOOK_PAGE_ACCESS_TOKEN` and
`META_API_VERSION` against graph.facebook.com. Set `INSTAGRAM_USER_ID` to the linked
professional Instagram account's ID (not the Facebook Page ID). The Facebook Login
route needs `instagram_basic` and `instagram_content_publish` as well as Page access.
An Instagram Login token cannot be used as a Facebook Page token.

Instagram also needs a public HTTPS copy of each exact exported video. Configure
`INSTAGRAM_MEDIA_BASE_URL` with a host mirroring `<run>/instagram_reel.mp4`, or use
the documented per-run URL mapping. This setting does not upload files to hosting.
Until valid tokens and hosting are configured, the Meta integration is not live:
account linking in Business Suite alone does not authorize the API.

The eight videos previously published through Business Suite are recorded in
`output/instagram_upload_queue.json`. Matching run names and video hashes are
blocked from re-posting through the APIs. Their original run folders are no longer
present in this workspace; the history is retained independently.

API references: [Meta Facebook Reels collection](https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api)
and [Meta Instagram Facebook Login collection](https://www.postman.com/meta/instagram/folder/u4g5a2a/instagram-api-with-facebook-login).
