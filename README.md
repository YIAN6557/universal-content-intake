# Universal Content Intake

[中文说明](README.zh-CN.md)

Universal Content Intake is a download tool for the Mac. **Give it one link
and it downloads what is behind it**: a video, a document, images, an article
or a whole web page. The result is filed in your Downloads folder.

On top of that it offers an optional companion feature, **automatic
monitoring**. Name a set of YouTube creators and the system finds their new
videos that are taking off, downloads them, translates them and burns in
Chinese subtitles, then adds the video details and a publish description. You
decide whether to use it the first time you run the tool.

## Quick start

```bash
git clone https://github.com/YIAN6557/universal-content-intake.git
cd universal-content-intake
bin/uci https://example.com/report.pdf
```

The first run asks two questions:

1. **Turn on automatic monitoring?** Yes starts a guided setup. No keeps the
   system a pure downloader, and you can switch monitoring on later.
2. **When you download a video, translate it and burn in Simplified Chinese
   subtitles?** The answers are on, off, or ask every time. This is
   independent of the first question. Videos picked by automatic monitoring
   always get Chinese subtitles.

It then lists the preparation your choices need, such as checking that ffmpeg
and yt-dlp are installed. Change your answers any time with
`bin/uci settings`.

## Downloading: one link is enough

```bash
bin/uci https://example.com/some-page                       # just a link: the type is detected
bin/uci "download the PDF at https://example.com/report"    # say what you want and it does exactly that
bin/uci "下载这个视频 https://www.youtube.com/watch?v=…"
```

- **Say what you want and it downloads exactly that.** "The PDF at this
  link", "this video" or "save this page" pick the matching downloader
  without probing. `--type video|pdf|document|image|images|article|webpage`
  does the same.
- **Give only a link and it decides.** It checks, in order:
  - known platforms: video sites such as YouTube and Bilibili, Google Drive,
    image sites;
  - the file extension;
  - what the server returns. HTML pages are then classified as an article or
    a page to keep whole.
- **Videos.** Any of the thousands of sites yt-dlp supports, at 1080p by
  default (`--quality 720p|1440p|4k|highest`). Low-resolution sources are
  never upscaled.
  - Subtitles follow your setting: on adds Chinese subtitles, off downloads
    the original only, and ask asks you on each download.
  - `--zh` or `--no-zh` overrides the setting for one download.
- **Documents.** Direct links to PDF, Word, Excel, PowerPoint, archives and
  more; Google Drive shares; configured rclone remotes.
- **Images.** A single picture, or a whole album or gallery.
- **Web pages.** Articles are extracted as Markdown. Whole pages are saved as
  offline HTML, Markdown and PDF.

Downloads land in your Downloads folder under their original title. A short
description file sits next to them with the source, author and original link.
Multi-file results, such as galleries, get their own folder. `--to <folder>`
saves them elsewhere.

## Companion feature: automatic monitoring (optional)

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
- **On your Mac (a background worker).** It downloads the picked videos,
  translates them, burns in Chinese subtitles and delivers each one with a
  publish sheet: title, copy, hashtags and source.

Turning it on and off:

```bash
bin/uci settings --monitoring on     # on; if it has never been set up, run bin/uci setup as prompted
bin/uci settings --monitoring off    # off: stops the local worker and pauses the cloud; creators and settings are kept
bin/uci status                       # how automatic monitoring is running
```

Setup needs a Google account, and a wizard walks you through it in about 30–60
minutes. **No YouTube API key is needed.** The cloud part uses your own Google
authorization, with a free quota of 10,000 units a day, of which a typical
setup uses one to two thousand. A free Gemini API key is recommended, which
takes about two minutes to create. It filters videos by your editorial
direction and writes the publish copy.

## Requirements

| Feature | Needs |
|---|---|
| Downloading (everyone) | macOS 15 or newer (Apple silicon recommended), Python 3.11+, ffmpeg, yt-dlp, deno; gallery-dl for images, SingleFile and Chrome for whole pages, rclone for cloud drives |
| Subtitles (when set to on or ask) | Xcode Command Line Tools; the wizard downloads the ~490 MB Whisper speech model and builds the translation helper; Apple Translation language pack (English → Simplified Chinese) |
| Automatic monitoring (when on) | All of the above, plus a Google account and Node.js (for `clasp`); a free Gemini API key is recommended |

`bin/uci setup doctor` lists anything missing with the command to install it.
Tools for features you have switched off are marked as not needed.

## Settings and setup

```bash
bin/uci settings          # show or change the two choices
bin/uci setup             # guided setup: only the steps your choices need, and what to do next
bin/uci setup verify      # acceptance check: a trial download
```

Every wizard step says whether an agent can do it or you must do it yourself,
for example signing in to Google, approving access or pasting a secret.
[SETUP.md](SETUP.md) walks through every step. If you use a coding agent such
as Claude Code, point it at [SKILL.md](SKILL.md). It asks you the two
questions first, then guides you through setup.

The older `bin/uci-get`, `bin/uci-setup` and `bin/uci-status` still work as
aliases of `bin/uci get`, `bin/uci setup` and `bin/uci status`.

## Where settings live

| Where | What |
|---|---|
| `~/.config/universal-content-intake/config.yaml` | the two choices (`features.monitoring`, `features.video_subtitles`); downloads, delivery and work folders; video quality (1080p by default); Chrome sign-in fallback (off by default); cloud endpoint |
| Google Sheet → `Config` (monitoring; edit with `bin/uci setup config set key=value`) | timezone, discovery window, final sweep, selection time, daily count, latest start time for the second pick, view thresholds, maximum length, title rules, Gemini check and your editorial direction |
| Google Sheet → `Creators` (monitoring; edit with `bin/uci setup creators …`) | the creator whitelist |
| macOS Keychain | the shared secret between Mac and cloud; optional Gemini / Anthropic keys for publish copy |
| Apps Script → Script Properties | the same shared secret; optional `UCI_GEMINI_API_KEY` |

Your creator list and editorial direction stay in your own Google Sheet and
local config; none of it is stored in this repository.

## Privacy and security

- Downloads are anonymous. Chrome's sign-in state is used for sign-in-gated
  videos only when you enable `video.allow_browser_cookies`.
- Speech recognition and translation run on your Mac. The optional Gemini
  check sends video titles, descriptions and statistics to Google. The
  optional publish writer also sends translated subtitle excerpts.
- With automatic monitoring, every request between the Mac and the cloud is
  signed with HMAC-SHA256. The secret lives only in your Keychain and Script
  Properties, and the wizard never displays it.

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
