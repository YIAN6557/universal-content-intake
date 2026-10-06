---
name: universal-content-intake
description: Download any link (video, document, images, article, web page) with bin/uci for a user, and, when they want it, set up and operate the optional automatic YouTube monitoring (first-run choices, guided setup, status, creators, schedule).
---

# Working with Universal Content Intake for a user

The system has two layers:

- **Downloading (the main feature):** `bin/uci <link>`. It handles videos,
  documents, images, articles and whole web pages.
- **Automatic monitoring (optional):** YouTube creators are watched; rising
  videos are downloaded, translated and subtitled automatically.

## First run: ask the two questions

Before anything else works, the user must answer two questions. You cannot
answer them for the user, and the terminal prompt does not reach you. If a
`bin/uci` command prints "首次使用需要先做选择", ask the user both questions in
their language, then record the answers:

1. Turn on automatic monitoring? (on / off). Explain what it does, that it
   needs a Google account, and that setup takes about 30–60 minutes.
2. When they download a video themselves, translate it and burn in Simplified
   Chinese subtitles? (on / off / ask every time). This is independent of
   question 1. Monitored videos always get subtitles.

```bash
bin/uci settings --monitoring on|off --subtitles on|off|ask
```

Then run `bin/uci setup` and continue with the setup loop below. The choices
can be changed later with the same command. Turning monitoring off stops the
local worker and pauses the cloud; nothing is deleted.

## Downloading a link

Pass the user's words along with the link, for example
`bin/uci "download the PDF at …"` or `bin/uci "下载这个视频 …"`. If the request
names a type, that downloader is used without probing. A bare link is
classified automatically.

- **Subtitles setting is "ask".** For a video, ask the user whether to
  translate and burn in Chinese subtitles. Then pass `--zh` or `--no-zh`.
  Without one of them the command stops, because it cannot ask on its own.
- **Other subtitle settings.** Pass `--zh` or `--no-zh` only when the user
  wants an exception for this one video.
- **Type.** Add `--type video|pdf|document|image|images|article|webpage` when
  the user was explicit but their wording is unusual.
- **Quality.** Add `--quality 720p|1440p|4k|highest` only on request; 1080p
  is the default.
- **Destination.** Add `--to <folder>` when they name a destination.
  Otherwise files go to the Downloads folder.
- **Check first.** `--dry-run` shows what a link would be treated as without
  downloading.

If a type fails for a missing tool, run `bin/uci setup doctor` and follow its
fix line.

## Setup loop

The wizard `bin/uci setup` is the source of truth. It lists only the steps the
user's two choices need.

1. Run `bin/uci setup status --json` (or plain `bin/uci setup` to show the
   user).
2. Take the first step with `"done": false`. The optional Gemini step may be
   skipped when the user says so (`bin/uci setup skip gemini`).
3. If the step's `who` starts with `Agent`, run the commands in its `guide`.
   Report failures with the wizard's message; do not improvise workarounds
   that change cloud state.
4. If the step is human work, explain the `guide` lines in the user's language,
   in short numbered steps, and wait for the user to say it is done. Then
   re-run the status.
5. Finish with `bin/uci setup verify` and show the user its report.

## Hard rules

- **Never decide the two first-run choices yourself.** Ask the user.
- **Never type, paste or print secrets.** This covers the Queue API secret, the
  Gemini key and the Anthropic key. The wizard stores the Queue secret in the
  Keychain and copies it to the clipboard (`bin/uci setup secret copy`). The
  user pastes it into Script Properties. API keys are created and pasted by the
  user.
- **Leave Google sign-in and consent to the user.** That includes
  `clasp login`, the Apps Script authorization dialog, the "unverified app"
  screen, and enabling the Apps Script API at
  https://script.google.com/home/usersettings.
- **Leave macOS prompts to the user.** That includes installing the Xcode
  tools, translation language packs, folder access and notifications.
- **Steer the user to the right editor.** Open it with
  `bin/uci setup cloud open` and have the user check that the project name at
  the top left is "Universal Content Intake". Tell them never to click into or
  type in the code area. If an editor run fails with a ReferenceError or
  SyntaxError, the code was edited by accident: run
  `bin/uci setup cloud push` and have them run `uciSetup` again.
- **Confirm the creator list before `bin/uci setup creators apply`.** Show the
  table from `creators resolve`, including the advice column.
- **Write the editorial brief from the user's own words.** Ask what they want
  and what they never want. Draft the brief in plain language, show it, and
  only then run `bin/uci setup config brief --file …`.
- **Keep the user's choices private.** The creator list, editorial brief and
  schedule belong to the user. Never commit them to this repository or put them
  in issues.
- **Do not edit Config cells in the Sheet by hand.** Use
  `bin/uci setup config set`, which validates values and keeps clocks stored as
  text.

## Questions to ask in the monitoring preferences step

- Which timezone do you work in, and which hours of the creators' upload day
  should count? These set `discovery_window_start` and `discovery_window_end`.
- When do you want the picks ready, and by when must the second one start?
  These set `daily_selection_time` (at least 120 minutes after the window ends)
  and `rank2_start_cutoff`.
- How many clips per day (0–2)? What is the maximum length in minutes?
- Which kinds of uploads should be dropped by title alone? The options are
  PODCAST, KEYNOTE, QA, LIVESTREAM, REVIEW, FINANCE, TUTORIAL, GAMING,
  NEWS_ROUNDUP and AD; anything subtler belongs in the brief.
- Where should finished videos go (`delivery_root`)?

## Operating afterwards

| User asks | Do |
|---|---|
| Download a link | `bin/uci "<their request> <link>"` (see above) |
| Change the two choices | ask them, then `bin/uci settings --monitoring … --subtitles …` |
| How is monitoring running? | `bin/uci status` (another day: `bin/uci status --day 2026-10-05`) |
| Add or remove creators | `bin/uci setup creators resolve …` → confirm → `apply`; `enable`/`disable <channel id>` |
| Change times or limits | `bin/uci setup config show`, then `config set key=value …` |
| Change content direction | edit the brief with the user, then `config brief --file …` |
| Cloud code changed (after `git pull`) | `bin/uci setup cloud push` then `bin/uci setup cloud deploy` |
| Worker not running | `bin/uci setup launchagent status`; logs in `~/Library/Logs/Universal Content Intake/` |

Background on the architecture and the per-stage CLI is in
`docs/REFERENCE.md`; the human-readable walkthrough is `SETUP.md`.
