# Developer reference

## Layout and ownership

| Path | Owns |
|---|---|
| `src/core/` | Job contracts, policy, type resolution, state transitions, error mapping, persisted Provider checkpoints, formal output promotion and cleanup |
| `src/providers/` | Acquisition adapters (video, image, article, webpage, document). They return structured metadata, artifacts, resume tokens or failures and never change Job state |
| `src/media/` | Subtitle discovery/selection, VAD + Whisper ASR, Apple Translation bridge, subtitle layout, burn-in, publish assist |
| `src/output/` | Workspace containment, output paths, `info.md`, delivery to the delivery folder and the Chinese publish sheet |
| `src/queue/` | Signed Queue API client, Keychain secret, LaunchAgent Worker, `uci-status`, weekly yt-dlp updater |
| `src/setup/` | `uci-setup` first-run wizard |
| `src/get_cli.py` | `uci-get`: one-link downloader (stated type, or detection by platform → extension → server response) |
| `cloud/apps-script/` | Creator discovery, snapshots, baselines, HOT scoring, content filter, optional Gemini judge, daily selection, Queue sheet and the HMAC-authenticated Web App |
| `apple-helper/` | Swift sources for the Apple Translation and PDFKit helpers (built by `uci-setup build-tools`) |

Settings come from `config/defaults.yaml`, overlaid by
`~/.config/universal-content-intake/config.yaml` (`UCI_CONFIG` points elsewhere;
`UCI_CONFIG=none` disables the overlay).

## Cloud pipeline

`stage6RunScheduler` runs every 10 minutes:

1. **Discovery.** It runs inside the discovery window and in the final sweep.
   It reads each enabled creator's uploads playlist and records new uploads
   published inside the batch window. Live streams and premieres are tracked
   separately. Uploads longer than `content_max_duration_minutes` are rejected,
   and so are titles that match an enabled `content_title_filters` rule. Both
   become `CONTENT_REJECTED`.
2. **Snapshots and HOT.** View, like and comment counts are recorded at
   T+30/60/120 minutes. Creators without history use a cold-start baseline
   (median of their recent uploads × checkpoint ratio). Creators with history
   use warm relative velocity. A like-rate floor applies in both cases.
3. **Semantic judge (optional).** Gemini receives the title, description and
   statistics, plus your `semantic_editorial_brief`, and accepts or rejects the
   video with a reason code.
4. **Daily selection.** At `daily_selection_time` up to `daily_selection_max`
   candidates are queued. Rank 2 must start before `rank2_start_cutoff`.

The Web App (`doPost` in `api.gs`) accepts only HMAC-SHA256 signed envelopes:
`{timestamp, payload, signature}`, where
`signature = base64(HMAC(secret, timestamp + "\n" + payload))`. It serves the
Worker actions (`claim`, `heartbeat`, `core_started`, `complete`, `fail`), the
read-only `status`, and the setup actions (`setup_inspect`, `setup_config_set`,
`setup_creators_upsert`).

## Worker

The LaunchAgent Worker claims one task at a time and drives the VIDEO Job by
its persisted progress: `video-run` → `stage3-run` → `finalize-job`. After a
restart it continues at the unfinished step.

- **Transient network failures.** `NETWORK_PAUSED` is retried inside the claim
  (60/180/600 s) before the task is paused.
- **Stage 3 pauses.** A missing translation language pack or an undetermined
  ASR language pauses the task, and the pause can be recovered.
- **Delivery.** When idle, the Worker moves finished videos and their publish
  sheets into `output.delivery_root`.
- **Notifications.** It posts macOS notifications for completed, paused,
  failed and cutoff-eliminated tasks.

## Command-line tools

All commands are run from the project root.

```bash
python3 -m src.cli inspect-config
python3 -m src.cli create-job --url https://example.invalid/item --type VIDEO --workspace-root /tmp/uci
python3 -m src.cli video-run --url 'https://www.youtube.com/watch?v=…' --workspace-root /tmp/uci [--quality 720p|1080p|1440p|2160p|highest]
python3 -m src.cli stage3-run --resume-job /tmp/uci/job-<id>/job.json
python3 -m src.cli finalize-job --resume-job /tmp/uci/job-<id>/job.json
python3 -m src.cli translation-preflight --source en --target zh-Hans
python3 -m src.cli image-run | article-run | webpage-run | document-run --url …
```

- **VIDEO.** It probes anonymously and caps quality at 1080p by default
  without upscaling. It prefers H.264/AAC MP4 and never transcodes on
  download. Partial files stay in the Job's `temp/`.
- **Stage 3.**
  - Subtitles: manual and original-language automatic tracks are used first.
    Without them, Silero VAD runs and Whisper `ggml-small` transcribes only
    when speech is detected.
  - Translation and rendering: Apple Translation translates to `zh-Hans`.
    ASS/libass burns in the subtitles with Noto Sans CJK SC.
  - Resume: resume manifests check input identity and output hashes before
    reuse.
- **Stage 5.** It verifies artifact hashes, promotes the deliverable
  (`output/final.mp4` for video), writes `output/info.md` with publish assist,
  then cleans the Job's `temp/`.
- **Other content types.**
  - IMAGE uses gallery-dl.
  - ARTICLE uses Trafilatura.
  - WEBPAGE uses SingleFile + Chrome Headless; set `UCI_SINGLE_FILE_PATH` and
    `UCI_CHROME_PATH`.
  - DOCUMENT uses curl/aria2/rclone/gdown.

  These are optional and not used by the YouTube pipeline.

## Environment variables

| Variable | Purpose |
|---|---|
| `UCI_CONFIG` | user config path (`none` to disable) |
| `UCI_PYTHON` | interpreter used by `bin/uci-setup` and `bin/uci-status` |
| `UCI_YTDLP_PATH`, `UCI_FFMPEG_PATH`, `UCI_FFPROBE_PATH`, `UCI_DENO_PATH`, `UCI_CLASP_PATH` | tool locations |
| `UCI_WHISPER_SMALL_MODEL` | alternative Whisper model path |
| `UCI_PUBLISH_WRITER=off` | force rule-based publish copy |
| `UCI_GEMINI_MODEL` | model for the local publish writer |

## Tests

```bash
python3 -m unittest discover -s tests
node --test tests/*.js
```

The Apps Script files are loaded into a Node `vm` context with in-memory
Sheet, Properties and Trigger fakes, so the cloud logic is tested without a
Google account.
