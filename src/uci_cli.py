"""``bin/uci``: one entry point for Universal Content Intake.

  bin/uci <链接或一句话>       download (the main feature)
  bin/uci settings [...]      show or change the two first-run choices
  bin/uci setup [...]         guided setup (environment, subtitle tools, automatic monitoring)
  bin/uci status [...]        engine freshness and, when on, the automatic monitoring report
  bin/uci engines             check GitHub for newer download engines now (never installs)

The first time any command runs, two questions are asked: whether to turn on
automatic monitoring, and whether downloaded videos get Chinese subtitles
(on / off / ask every time). Without a terminal to ask in, the command stops and
explains how to answer with ``bin/uci settings``.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Sequence

from src.setup import preferences

SUBCOMMANDS = {"get", "setup", "status", "settings", "engines", "help"}
HELP = """用法：
  bin/uci <链接>                      下载这个链接里的内容（视频、文档、图片、文章、网页），类型自动判断
  bin/uci "下载这个链接里的PDF <链接>"   说明了要什么，就直接用对应方式下载
  bin/uci <链接> --zh / --no-zh        视频：这次翻译压制中文字幕 / 这次只下载原视频
  bin/uci settings                    查看或修改：自动监控、视频翻译压制
  bin/uci setup                       配置向导：检查环境、安装工具、配置自动监控
  bin/uci status                      运行情况：下载引擎是否最新，以及自动监控（启用时）
  bin/uci engines                     现在检查下载引擎在 GitHub 上有没有新版本
更多选项：bin/uci get --help、bin/uci setup --help"""


def _interactive() -> bool:
    return sys.stdin.isatty()


def cmd_settings(argv: Sequence[str], *, interactive: bool, read: Callable[[str], str] = input,
                 out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="uci settings", description="查看或修改自动监控和视频翻译压制的选择")
    parser.add_argument("--monitoring", choices=("on", "off"), help="启用或关闭自动监控")
    parser.add_argument("--subtitles", choices=preferences.SUBTITLE_CHOICES,
                        help="下载视频时翻译压制中文字幕：on 启用 / off 不启用 / ask 每次询问")
    args = parser.parse_args(list(argv))
    before = preferences.load()
    if args.monitoring is None and args.subtitles is None:
        for line in preferences.describe(before):
            out(line)
        out("修改：bin/uci settings --monitoring on|off --subtitles on|off|ask")
        return 0
    if args.subtitles is not None:
        prefs = preferences.save(subtitles=args.subtitles)
        out(f"✓ 下载视频时翻译压制中文字幕：{preferences.SUBTITLE_LABELS[args.subtitles]}")
        _offer_subtitle_tools(prefs, interactive=interactive, read=read, out=out)
    if args.monitoring is not None:
        _set_monitoring(args.monitoring == "on", before.monitoring, out=out)
    return 0


def _offer_subtitle_tools(prefs: preferences.Preferences, *, interactive: bool, read: Callable[[str], str],
                          out: Callable[[str], None]) -> None:
    from src.get_cli import subtitle_tools_missing, translation_missing
    from src.setup import translation_setup

    if not prefs.needs_subtitle_tools:
        return
    if subtitle_tools_missing():
        out("翻译压制需要的语音识别模型和工具还没装（约 490 MB）。")
        if interactive and read("现在安装吗？[y] 安装  [n] 以后再说 > ").strip().lower() in {"y", "yes"}:
            from src.setup import environment

            environment.build_tools()
            out("✓ 已安装。" + ("还需要 Apple 翻译语言包：运行 bin/uci setup 查看。" if translation_missing() is None else ""))
        else:
            out("之后运行 bin/uci setup build-tools 安装，再运行 bin/uci setup 核对。")
    if translation_missing() is None or translation_missing():
        out("")
        translation_setup.offer(interactive=interactive, read=read, out=out)


def _set_monitoring(enabled: bool, previously: bool | None, *, out: Callable[[str], None]) -> None:
    from src.queue.client import QueueApiError
    from src.setup import cloud, launchagent, store

    configured = bool(str(store.read_user_config().get("queue_api_url") or "").strip()) and cloud.secret_exists()
    if enabled:
        preferences.save(monitoring=True)
        if configured:
            try:
                cloud.set_monitoring(True)
                out("✓ 云端已恢复：发现、快照和选片继续运行。")
            except QueueApiError as error:
                out(f"⚠ 云端没能恢复（{error.code}）。稍后重试：bin/uci settings --monitoring on")
            if store.read_state().get("verified_at"):
                launchagent.install(labels=launchagent.labels_for(True))
                out("✓ 本机后台程序已启动。")
        out("✓ 自动监控：启用。" + ("" if configured else "接下来运行 bin/uci setup，向导会一步步带你完成配置。"))
        return
    if configured:
        try:
            cloud.set_monitoring(False)
            out("✓ 云端已暂停：不再发现、记录和选片，不消耗 YouTube 配额；作者名单和设置都保留。")
        except QueueApiError as error:
            out(f"⚠ 云端没能暂停（{error.code}）。稍后重试：bin/uci settings --monitoring off")
    if launchagent.installed_here():
        # Only the Worker: the yt-dlp update and the engine check stay on for the downloader.
        launchagent.uninstall(labels=(launchagent.WORKER_LABEL,))
        out("✓ 本机后台程序已停止（下载引擎的定期更新和检查保留）。")
    preferences.save(monitoring=False)
    out("✓ 自动监控：不启用。重新开启：bin/uci settings --monitoring on")


def cmd_status(argv: Sequence[str], *, out: Callable[[str], None] = print) -> int:
    from src.queue import engine_check

    if not preferences.load().monitoring:
        out("自动监控没有启用，这台电脑只使用下载功能。")
        out("开启：bin/uci settings --monitoring on")
        code = 0
    else:
        from src.queue import status_cli

        code = status_cli.main(list(argv))
    if "--json" not in argv:
        out("")
        for line in engine_check.summary_lines(engine_check.read_state()):
            out(line)
    return code


def _console_setup() -> None:
    """Windows consoles and pipes may use a legacy code page; never crash on a ✓ or a Chinese title."""

    from src.core import compat

    if compat.WINDOWS:
        os.environ.setdefault("PYTHONUTF8", "1")
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _console_setup()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"help", "-h", "--help"}:
        print(HELP)
        prefs = preferences.load()
        if prefs.complete:
            print("\n当前设置：" + "；".join(preferences.describe(prefs)))
        return 0
    command, rest = (argv[0], argv[1:]) if argv[0] in SUBCOMMANDS else ("get", argv)
    interactive = _interactive()
    answering = command == "settings" and any(arg.startswith("--monitoring") or arg.startswith("--subtitles") for arg in rest)
    if not answering:
        first_time = not preferences.load().complete
        prefs = preferences.ensure_first_run(interactive=interactive)
        if prefs is None:
            return 2
        if first_time and prefs.complete and command != "setup":
            # Walk the user straight into the steps their choices need.
            from src.setup import cli as setup_cli

            print("\n根据你的选择，需要完成下面这些步骤：\n")
            setup_cli.main([])
            print("")
        if command == "get" and prefs.monitoring and not _monitoring_ready():
            print("提示：自动监控还没配置完。下载功能可以先用；配置自动监控请运行 bin/uci setup。\n")
    if command == "settings":
        return cmd_settings(rest, interactive=interactive)
    if command == "status":
        return cmd_status(rest)
    if command == "engines":
        from src.queue import engine_check

        return engine_check.main(rest)
    if command == "setup":
        from src.setup import cli as setup_cli

        return setup_cli.main(rest)
    from src import get_cli

    return get_cli.main(rest, prog="uci")


def _monitoring_ready() -> bool:
    from src.setup import store

    return bool(store.read_state().get("verified_at"))


if __name__ == "__main__":
    raise SystemExit(main())
