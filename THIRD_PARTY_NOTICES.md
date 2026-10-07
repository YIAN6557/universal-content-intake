# Third-party notices

Bundled in this repository:

| Component | Where | License |
|---|---|---|
| Noto Sans CJK SC Medium (Google / Adobe) | `fonts/NotoSansCJKsc-Medium.otf` | SIL Open Font License 1.1, see `fonts/OFL.txt` |

Downloaded or built on your Mac by `bin/uci-setup build-tools` (not stored in the repository):

| Component | Source | License |
|---|---|---|
| whisper.cpp, newest release at build time, recorded in `tools/whisper.cpp/VERSION` (`whisper-cli`, `whisper-vad-speech-segments`) | https://github.com/ggml-org/whisper.cpp | MIT, copied to `tools/whisper.cpp/LICENSE` |
| Whisper `ggml-small.bin` model (OpenAI Whisper weights in ggml format) | https://huggingface.co/ggerganov/whisper.cpp | MIT |
| Silero VAD `ggml-silero-v6.2.0.bin` | https://huggingface.co/ggml-org/whisper-vad | MIT |

Installed separately and invoked as external programs (not distributed here):
yt-dlp (Unlicense), FFmpeg (LGPL/GPL depending on your build), Deno (MIT),
clasp (Apache-2.0), certifi (MPL-2.0), Trafilatura (Apache-2.0), and optionally
gallery-dl, SingleFile, aria2, rclone and gdown for the non-video providers.
Apple Translation and PDFKit are macOS system frameworks.
