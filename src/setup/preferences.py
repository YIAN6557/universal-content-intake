"""The two first-run choices: automatic monitoring on/off, and Chinese subtitles for downloaded videos.

Both live in the user config (``features.monitoring``, ``features.video_subtitles``)
and are asked the first time any ``bin/uci`` command runs. ``bin/uci settings``
changes them later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.setup import store
from src.setup.launchagent import plist_path
from src.queue.status_cli import WORKER_LABEL

SUBTITLE_CHOICES = ("on", "off", "ask")
SUBTITLE_LABELS = {"on": "启用", "off": "不启用", "ask": "每次询问"}


@dataclass(frozen=True)
class Preferences:
    monitoring: bool | None
    subtitles: str | None

    @property
    def complete(self) -> bool:
        return self.monitoring is not None and self.subtitles is not None

    @property
    def needs_subtitle_tools(self) -> bool:
        """Whisper, the models and Apple Translation are needed for --zh downloads and for every monitored video."""

        return bool(self.monitoring) or self.subtitles in {"on", "ask"}


def load() -> Preferences:
    features = store.read_user_config().get("features") or {}
    monitoring = features.get("monitoring")
    subtitles = features.get("video_subtitles")
    return Preferences(
        monitoring=monitoring if isinstance(monitoring, bool) else None,
        subtitles=subtitles if subtitles in SUBTITLE_CHOICES else None,
    )


def save(*, monitoring: bool | None = None, subtitles: str | None = None) -> Preferences:
    changes: dict[str, object] = {}
    if monitoring is not None:
        changes["monitoring"] = bool(monitoring)
    if subtitles is not None:
        if subtitles not in SUBTITLE_CHOICES:
            raise ValueError(f"subtitles must be one of {', '.join(SUBTITLE_CHOICES)}")
        changes["video_subtitles"] = subtitles
    if changes:
        store.update_user_config({"features": changes})
    return load()


def monitoring_already_configured() -> bool:
    """An installation that set up the cloud or the background worker before this choice existed."""

    config = store.read_user_config()
    return bool(str(config.get("queue_api_url") or "").strip()) or bool(store.read_state().get("deployment_id")) \
        or plist_path(WORKER_LABEL).is_file()


MONITORING_QUESTION = """问题 1：是否启用自动监控？
  启用后，你指定一批 YouTube 作者，系统会自动发现他们正在起量的新视频，
  自动下载、翻译并压制中文字幕，附上视频信息和发布文案。
  需要一个 Google 账号；配置有向导一步步带你完成，大约 30–60 分钟。
  不启用的话，这套系统就是一个“给链接就能下载”的工具，以后随时可以再开启。
  [y] 启用    [n] 不启用"""

SUBTITLE_QUESTION = """问题 2：你自己下载视频时，是否自动翻译成简体中文并压制字幕？
  （只影响你给链接下载的视频；自动监控选出的视频始终会加中文字幕）
  启用或每次询问，需要额外安装约 490 MB 的语音识别模型和翻译工具。
  [1] 启用    [2] 不启用    [3] 每次询问"""


def _ask(question: str, answers: dict[str, object], read: Callable[[str], str], out: Callable[[str], None]) -> object:
    out(question)
    while True:
        reply = read("> ").strip().lower()
        if reply in answers:
            return answers[reply]
        out("请输入方括号里的选项。")


def ensure_first_run(
    *,
    interactive: bool,
    read: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> Preferences | None:
    """Make sure both choices are made. Returns None when they are missing and nobody can be asked."""

    prefs = load()
    if prefs.monitoring is None and monitoring_already_configured():
        prefs = save(monitoring=True)
        out("检测到这台 Mac 已经配置过自动监控，已记为“启用”。")
    if prefs.complete:
        return prefs
    if not interactive:
        missing = []
        if prefs.monitoring is None:
            missing.append("--monitoring on|off")
        if prefs.subtitles is None:
            missing.append("--subtitles on|off|ask")
        out("首次使用需要先做选择，请先问用户，再运行：")
        out(f"  bin/uci settings {' '.join(missing)}")
        out("  --monitoring：是否启用自动监控（on 启用 / off 不启用）")
        out("  --subtitles：下载视频时是否自动翻译压制中文字幕（on 启用 / off 不启用 / ask 每次询问）")
        return None
    out("欢迎使用 Universal Content Intake。开始之前有两个问题，以后可以用 bin/uci settings 修改。\n")
    if prefs.monitoring is None:
        prefs = save(monitoring=bool(_ask(MONITORING_QUESTION, {"y": True, "yes": True, "n": False, "no": False}, read, out)))
        out("")
    if prefs.subtitles is None:
        prefs = save(subtitles=str(_ask(SUBTITLE_QUESTION, {"1": "on", "2": "off", "3": "ask"}, read, out)))
        out("")
    out(f"已保存：自动监控 {'启用' if prefs.monitoring else '不启用'}；视频翻译压制 {SUBTITLE_LABELS[prefs.subtitles]}。")
    return prefs


def describe(prefs: Preferences) -> list[str]:
    return [
        f"自动监控：{'启用' if prefs.monitoring else ('不启用' if prefs.monitoring is False else '未选择')}",
        f"下载视频时翻译压制中文字幕：{SUBTITLE_LABELS.get(prefs.subtitles or '', '未选择')}",
    ]
