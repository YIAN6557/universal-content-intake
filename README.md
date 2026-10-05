# Universal Content Intake

[中文说明](README.zh-CN.md)

Universal Content Intake is a content collection and organization toolkit for
the Mac. It works on two levels:

1. **An automated pipeline.** Name a set of YouTube creators and the system
   watches them for new uploads and spots the ones gaining traction. It
   downloads them, translates the subtitles and burns them in as Simplified
   Chinese, then attaches the full video details and a ready-to-use publish
   description.
2. **A standalone downloader.** Give it one link and it downloads what is
   behind it: videos, PDFs and other documents, images and galleries,
   articles, or a complete web page.

Use both, or only the downloader.

## Level 1: the automated pipeline

```
 Creators ─► periodic discovery ─► early view tracking ─► rules + optional AI editorial check ─► daily pick
                                                                                                   │
 Delivery folder ◄─ burn-in ◄─ on-device translation ◄─ subtitles / on-device speech recognition ◄─ Mac worker
```

- **In the cloud (Google Apps Script + one Google Sheet).**
  - During your discovery window it checks the creators every 10 minutes and
    records views at 30, 60 and 120 minutes after publishing.
  - It compares each video with that creator's own history to find the ones
    taking off.
  - It applies your length and title rules. With Gemini connected, it also
    checks each video against your editorial direction.
  - Once a day it picks one or two videos for the Mac.
- **On your Mac (a background worker).**
  - It downloads at **1080p by default**. 720p, 1440p, 4K or the best
    available are configurable; low-resolution sources are never upscaled.
  - It uses the video's own subtitles when they exist, or on-device Whisper
    speech recognition when they don't.
  - It translates on device with Apple Translation and burns the Chinese
    subtitles into the picture.
  - The finished video is delivered together with a publish sheet: title,
    copy, hashtags, creator, original link and publish time.

## Level 2: the downloader

```bash
bin/uci-get "download the PDF at https://example.com/report"
bin/uci-get "下载这个视频 https://www.youtube.com/watch?v=…"
bin/uci-get https://example.com/some-page        # just a link: the type is detected
```

- **Say what you want and it downloads exactly that.** Requests like "the
  PDF at this link", "this video" or "save this page" pick the matching
  downloader without probing. `--type
  video|pdf|document|image|images|article|webpage` does the same.
- **Give only a link and it decides.** It checks, in order:
  - known platforms: video sites such as YouTube and Bilibili, Google Drive,
    image sites;
  - the file extension;
  - what the server returns. HTML pages are then classified as an article or
    a page to keep whole.
- **Videos.** Any of the thousands of sites yt-dlp supports, 1080p by default
  (`--quality` to change). `--zh` also translates and burns in Chinese
  subtitles.
- **Documents.** Direct links to PDF, Word, Excel, PowerPoint, archives and
  more; Google Drive shares; configured rclone remotes.
- **Images.** A single picture, or a whole album or gallery.
- **Web pages.** Articles are extracted as Markdown. Whole pages are saved as
  offline HTML, Markdown and PDF.

Downloads land in your Downloads folder under their original title, next to a
short description file. Multi-file results, such as galleries, get their own
folder. `--to <folder>` saves them elsewhere.

## Requirements

- macOS 15 or newer; Apple silicon recommended.
- Python 3.11+, Xcode Command Line Tools, ffmpeg, yt-dlp, deno.
- **Downloader only.** Finishing the first three setup steps (environment,
  models and tools, translation pack) is enough; no Google account is needed.
  Some content types use extra tools: gallery-dl for images, SingleFile and
  Chrome for whole pages, rclone for cloud drives. `bin/uci-setup doctor`
  lists what is missing and how to install it.
- **Automated pipeline.** Also a Google account and Node.js (for `clasp`).
  **No YouTube API key is needed.** The cloud part uses your own Google
  authorization, with a free quota of 10,000 units a day, of which a typical
  setup uses one to two thousand.
- **Recommended.** A free Gemini API key, which takes about two minutes to
  create. It filters videos by your editorial direction and writes the publish
  title and copy. Without it everything still runs, but the picks are less
  focused and the copy is plainer.

## Install and set up

```bash
git clone https://github.com/YIAN6557/universal-content-intake.git
cd universal-content-intake
bin/uci-setup
```

`bin/uci-setup` is a step-by-step wizard. Each run checks your progress and
says what to do next. It marks every step as something an agent can do, or
something you do yourself, such as signing in to Google, approving access or
pasting a secret. Follow it until it reports that the system is ready.
[SETUP.md](SETUP.md) walks through every step. If you use a coding agent such
as Claude Code, point it at [SKILL.md](SKILL.md) and it can guide you through
setup.

Day to day:

```bash
bin/uci-status            # cloud health, today's batch, queue, local worker
bin/uci-setup config show # schedule, daily count, rules, editorial direction
bin/uci-get <link>        # one-off download
```

## Configuration

| Where | What |
|---|---|
| Google Sheet → `Config` (edit with `bin/uci-setup config set key=value`) | timezone, discovery window, final sweep, selection time, daily count, latest start time for the second pick, view thresholds, maximum length, title rules, Gemini check and your editorial direction |
| Google Sheet → `Creators` (edit with `bin/uci-setup creators …`) | the creator whitelist |
| `~/.config/universal-content-intake/config.yaml` | per-Mac settings: cloud endpoint, delivery folder, downloads folder, work folder, video quality (1080p by default), Chrome sign-in fallback (off by default) |
| macOS Keychain | the shared secret between Mac and cloud; optional Gemini / Anthropic keys for publish copy |
| Apps Script → Script Properties | the same shared secret; optional `UCI_GEMINI_API_KEY` |

Your creator list and editorial direction stay in your own Google Sheet and
local config; none of it is stored in this repository.

## Privacy and security

- Every request between the Mac and the cloud is signed with HMAC-SHA256. The
  secret lives only in your Keychain and Script Properties, and the wizard
  never displays it.
- Downloads are anonymous. Chrome's sign-in state is used for sign-in-gated
  videos only when you enable `video.allow_browser_cookies`.
- Speech recognition and translation run on your Mac. The optional Gemini
  check sends video titles, descriptions and statistics to Google. The
  optional publish writer also sends translated subtitle excerpts.

## Responsible use

Download, process and publish content only where you have the rights or
permission to do so, and follow each platform's terms of service and the laws
that apply to you. The software is provided as is, and its authors accept no
liability for how it is used.

## Development

```bash
python3 -m unittest discover -s tests   # Python
node --test tests/*.js                  # Apps Script (run in a Node VM)
```

The per-stage command-line tools and module ownership are described in
[docs/REFERENCE.md](docs/REFERENCE.md).

## License

Universal Content Intake is dual-licensed:

- **Personal, non-commercial use** is free under the
  [Universal Content Intake Personal Use License 1.0](LICENSE).
- **Commercial use** requires a written commercial license and the applicable
  fee, obtained in advance; see [Commercial Licensing](COMMERCIAL_LICENSING.md)
  or email `ohhhhhmya@gmail.com`.

Use by companies, organizations, studios, teams, employees, contractors or for
client projects counts as commercial use. So does use for paid services,
commercial content, marketing, advertising, brand communication, or
integration into products and workflows. Internal use, trials, not yet being
profitable, or not charging a client separately do not make a use
non-commercial.

Third-party components are not relicensed by this repository; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Maintainer: [@YIAN6557](https://github.com/YIAN6557)
