---
name: universal-content-intake-setup
description: Guide a user through installing and configuring Universal Content Intake on their Mac with bin/uci-setup, and help them operate it afterwards (status, creators, schedule, content rules).
---

# Setting up Universal Content Intake with a user

You are helping a person install this system on their own Mac and Google
account. The wizard `bin/uci-setup` is the source of truth for progress. Run it,
do the steps marked **Agent**, and walk the person through the steps marked
**本人** (human). Re-run it after every step until it reports that the system
is ready.

## Loop

1. Run `bin/uci-setup status --json` (or plain `bin/uci-setup` to show the user).
2. Take the first step with `"done": false`. The optional Gemini step may be
   skipped when the user says so (`bin/uci-setup skip gemini`).
3. If the step's `who` starts with `Agent`, run the commands in its `guide`.
   Report failures with the wizard's message; do not improvise workarounds
   that change cloud state.
4. If the step is human work, explain the `guide` lines in the user's language,
   in short numbered steps, and wait for the user to say it is done. Then
   re-run the status.
5. Finish with `bin/uci-setup verify` and show the user its report.

## Hard rules

- **Never type, paste or print secrets.** This covers the Queue API secret, the
  Gemini key and the Anthropic key. The wizard stores the Queue secret in the
  Keychain and copies it to the clipboard (`bin/uci-setup secret copy`). The
  user pastes it into Script Properties. API keys are created and pasted by the
  user.
- **Leave Google sign-in and consent to the user.** That includes
  `clasp login`, the Apps Script authorization dialog, the "unverified app"
  screen, and enabling the Apps Script API at
  https://script.google.com/home/usersettings.
- **Leave macOS prompts to the user.** That includes installing the Xcode
  tools, translation language packs, folder access and notifications.
- **Confirm the creator list before `bin/uci-setup creators apply`.** Show the
  table from `creators resolve`, including the advice column.
- **Write the editorial brief from the user's own words.** Ask what they want
  and what they never want. Draft the brief in plain language, show it, and
  only then run `bin/uci-setup config brief --file …`.
- **Keep the user's choices private.** The creator list, editorial brief and
  schedule belong to the user. Never commit them to this repository or put them
  in issues.
- **Do not edit Config cells in the Sheet by hand.** Use
  `bin/uci-setup config set`, which validates values and keeps clocks stored as
  text.

## Questions to ask in the preferences step

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

## Downloading a link for the user

When the user asks to download something, use `bin/uci-get`. Pass their words
along with the link: "download the PDF at …" or "下载这个视频 …". If the request
names a type, that downloader is used without probing. A bare link is
classified automatically.

- Add `--type video|pdf|document|image|images|article|webpage` when the user
  was explicit but their wording is unusual.
- Add `--quality 720p|1440p|4k|highest` only on request; 1080p is the default.
- Add `--zh` when they want Chinese subtitles burned into a video.
- Add `--to <folder>` when they name a destination.
- Use `--dry-run` to check what a link would be treated as before
  downloading.

Downloads need only setup steps 1–3. If a type fails for a missing tool, run
`bin/uci-setup doctor` and follow its fix line.

## Operating afterwards

| User asks | Do |
|---|---|
| Download a link | `bin/uci-get "<their request> <link>"` (see above) |
| How is it running? | `bin/uci-status` (another day: `bin/uci-status --day 2026-10-05`) |
| Add or remove creators | `bin/uci-setup creators resolve …` → confirm → `apply`; `enable`/`disable <channel id>` |
| Change times or limits | `bin/uci-setup config show`, then `config set key=value …` |
| Change content direction | edit the brief with the user, then `config brief --file …` |
| Cloud code changed (after `git pull`) | `bin/uci-setup cloud push` then `bin/uci-setup cloud deploy` |
| Worker not running | `bin/uci-setup launchagent status`; logs in `~/Library/Logs/Universal Content Intake/` |

Background on the architecture and the per-stage CLI is in
`docs/REFERENCE.md`; the human-readable walkthrough is `SETUP.md`.
