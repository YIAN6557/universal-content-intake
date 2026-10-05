# Universal Content Intake

[中文说明](README.zh-CN.md)

Universal Content Intake watches a whitelist of YouTube creators, picks the
uploads that are taking off, and turns them into ready-to-post clips with
burned-in Simplified Chinese subtitles plus a Chinese publish sheet (title,
copy, hashtags, and every source fact). The publishing itself is left to you.

It runs in two halves:

- **Cloud (Google Apps Script + a Google Sheet).** Every 10 minutes during your
  discovery window it reads the creators' upload playlists, takes view
  snapshots at T+30/60/120 minutes, and marks a video HOT when it beats that
  creator's own baseline. It applies your content rules, and can also ask
  Gemini for an editorial judgement. At selection time it queues the best one
  or two videos.
- **Your Mac (a LaunchAgent worker).** It claims queued videos and downloads
  them at up to 1080p with yt-dlp. Subtitles come from YouTube or from local
  Whisper speech recognition. They are translated on device with Apple
  Translation and burned in with ffmpeg. The finished video and its publish
  sheet are moved into your delivery folder.

```
 Creators sheet ──► Discovery (every 10 min) ──► Snapshots T+30/60/120 ──► HOT vs creator baseline
                                                                              │
                         content rules + optional Gemini editorial judge ◄────┘
                                                                              │
 Delivery folder ◄── burn-in ◄── Apple Translation ◄── subtitles / Whisper ◄── Mac worker ◄── Queue (Web App, HMAC-signed)
```

## Requirements

- macOS 15 or newer (Apple Translation, PDFKit, LaunchAgent). Apple silicon recommended.
- Python 3.11+, Node.js (for `clasp`), Xcode Command Line Tools.
- ffmpeg/ffprobe, yt-dlp, deno.
- A Google account. **No YouTube API key is needed**: the cloud half uses the
  Apps Script YouTube advanced service under your own Google authorization
  (free quota 10,000 units/day; a typical setup uses a few thousand).
- Optional: a Gemini API key (editorial judge), and an Anthropic or Gemini key
  in the macOS Keychain for LLM-written publish titles and copy.

## Install

```bash
git clone https://github.com/YIAN6557/universal-content-intake.git
cd universal-content-intake
bin/uci-setup
```

`bin/uci-setup` is a step-by-step wizard. Each run checks what is already done
and prints the next step. It marks each step as something an agent (or the
wizard itself) can do, or something only you can do, such as signing in to
Google, granting permissions, or pasting a secret. Follow it until it reports
that the system is ready. [SETUP.md](SETUP.md) walks through every step. If you
use a coding agent such as Claude Code, point it at [SKILL.md](SKILL.md) and ask
it to set the system up with you.

Day to day:

```bash
bin/uci-status            # cloud health, today's batch, queue, local worker
bin/uci-setup config show # schedule, limits, content rules
```

## Configuration

| Where | What |
|---|---|
| Google Sheet → `Config` (edit with `bin/uci-setup config set key=value`) | timezone, discovery window, final sweep, selection time, daily maximum, Rank 2 cutoff, view/like thresholds, `content_max_duration_minutes`, `content_title_filters`, Gemini judge and your editorial brief |
| Google Sheet → `Creators` (edit with `bin/uci-setup creators …`) | the creator whitelist |
| `~/.config/universal-content-intake/config.yaml` | per-Mac settings: Queue API URL, delivery folder, work folder, quality cap, browser-cookie fallback (off by default) |
| macOS Keychain | Queue API shared secret (`UCI Queue API HMAC`), optional `UCI Gemini API` / `UCI Anthropic API` keys for publish copy |
| Apps Script → Script Properties | the same Queue API secret, optional `UCI_GEMINI_API_KEY` |

Your creator list and editorial direction stay in your own Google Sheet and
`~/.config`; nothing about them is stored in this repository.

## Privacy and security

- The Mac and the Web App authenticate every request with an HMAC-SHA256
  signature. The secret lives only in your Keychain and your Script
  Properties. The wizard never prints it.
- Downloads are anonymous. Reading Chrome's sign-in cookies for age- or
  sign-in-gated videos is **off** unless you enable
  `video.allow_browser_cookies`.
- Transcription and translation run locally. The optional Gemini judge sends
  video titles, descriptions and statistics to Google. The optional publish
  writer sends the same plus translated subtitle excerpts to Anthropic or
  Google.

## Responsible use

This tool downloads and re-subtitles other people's videos. **You are
responsible** for having the rights or permission to download, modify and
republish any content, and for following YouTube's Terms of Service and the
laws that apply to you. The authors provide the software as is and accept no
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
