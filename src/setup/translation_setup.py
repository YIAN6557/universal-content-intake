"""``bin/uci setup translation``: pick an online model for subtitle translation and save its API key.

Windows needs one to translate subtitles (there is no Apple Translation there);
on macOS it is an optional upgrade over Apple Translation. The wizard lists
the models, explains how to get a key step by step, saves the key in the
Keychain / Credential Manager and checks it with one test sentence.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Callable

from src.core import compat
from src.media import online_translation as online
from src.setup import store

Out = Callable[[str], None]


def choices_text() -> list[str]:
    lines = []
    for number, key in enumerate(online.RECOMMENDED, start=1):
        provider = online.PROVIDERS[key]
        tag = "（推荐）" if key == "deepseek" else ""
        lines.append(f"  {number}. {provider.name}{tag}")
        lines.append(f"     费用：{provider.cost}")
    return lines


def status_lines(settings: online.Settings | None = None) -> list[str]:
    settings = settings or online.load_settings()
    if settings.engine != "online":
        return ["字幕翻译：Apple 翻译（本机离线翻译，免费）。想换成在线模型（翻译更自然）：bin/uci setup translation list"]
    missing = online.configured(settings)
    head = f"字幕翻译：在线模型 {settings.label}" if settings.provider else "字幕翻译：在线模型（还没选）"
    return [head + ("，已配置" if not missing else f"，{missing}")]


def guide_lines(provider: online.Provider, model: str = "") -> list[str]:
    lines = [f"申请 {provider.name} 的 API Key，按这几步做（大约 3–5 分钟）："]
    lines += [f"  {number}. {step}" for number, step in enumerate(provider.steps, start=1)]
    if provider.key_url:
        lines.append(f"  API Key 页面：{provider.key_url}")
    lines.append("拿到 Key 后，在终端运行 bin/uci setup translation key ，按提示粘贴（屏幕上不会显示，粘贴完按回车）。")
    lines.append("Key 只存在这台电脑的" + ("Windows 凭据管理器" if compat.WINDOWS else "钥匙串") + "里，不会写进任何文件。")
    return lines


def use(provider_key: str, *, model: str = "", base_url: str = "", out: Out = print) -> online.Settings:
    if provider_key not in online.PROVIDERS:
        raise RuntimeError(f"没有这个选项：{provider_key}。可选：{', '.join(online.RECOMMENDED)}")
    provider = online.PROVIDERS[provider_key]
    if provider_key == "custom" and not (model and base_url):
        raise RuntimeError("“其他”需要同时给出 --base-url 和 --model")
    section = {"engine": "online", "provider": provider_key, "model": model or provider.model,
               "base_url": base_url or provider.base_url}
    store.update_user_config({"translation": section})
    settings = online.load_settings()
    out(f"✓ 字幕翻译改用：{settings.label}")
    return settings


def save_key(settings: online.Settings, value: str, *, out: Out = print) -> bool:
    """Try the key with one test sentence, then save it. A key the service rejects is not saved."""

    value = value.strip()
    if not value:
        out("没有收到 Key，什么都没改。")
        return False
    out("用一句话试一下这个 Key…")
    try:
        text = online.test_connection(settings, key=value)
    except online.TranslationKeyRejected as error:
        out(f"✗ {error}")
        out("  这个 Key 没有保存。检查有没有复制完整（通常以 sk- 开头），再运行 bin/uci setup translation key")
        return False
    except online.TranslationError as error:
        online.save_api_key(settings.provider, value)
        out(f"✓ Key 已保存，但试翻译没成功：{error}")
        out("  处理好后运行 bin/uci setup translation test 再试。")
        return False
    online.save_api_key(settings.provider, value)
    _verified(settings, text, out)
    return True


def check(settings: online.Settings | None = None, *, out: Out = print) -> bool:
    settings = settings or online.load_settings()
    try:
        text = online.test_connection(settings)
    except online.TranslationError as error:
        out(f"✗ 试翻译没成功：{error}")
        if isinstance(error, online.TranslationAccountError):
            out("  改好后运行 bin/uci setup translation test 再试；换一个 Key：bin/uci setup translation key")
        return False
    _verified(settings, text, out)
    return True


def _verified(settings: online.Settings, text: str, out: Out) -> None:
    out(f"✓ 试翻译成功：“The rocket landed safely on the drone ship.” → “{text}”")
    out(f"✓ Key 已保存在这台电脑的{'Windows 凭据管理器' if compat.WINDOWS else '钥匙串'}里。以后下载视频，字幕会用 {settings.label} 翻译。")
    store.update_state(translation_verified_at=_now(), translation_verified_model=settings.label)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wizard(*, read: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass, out: Out = print) -> bool:
    """Interactive: choose a model, get a key, save and test it. Returns True when translation works."""

    out("可选的翻译模型：")
    for line in choices_text():
        out(line)
    while True:
        answer = read(f"选哪一个？输入 1–{len(online.RECOMMENDED)}（直接回车 = 1） > ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(online.RECOMMENDED):
            break
    key = online.RECOMMENDED[int(answer) - 1]
    model = base_url = ""
    if key == "custom":
        base_url = read("API 地址（base URL）> ").strip()
        model = read("模型名 > ").strip()
    elif key == "doubao":
        model = read(f"模型 ID（直接回车 = {online.PROVIDERS[key].model}）> ").strip()
    settings = use(key, model=model, base_url=base_url, out=out)
    out("")
    for line in guide_lines(online.PROVIDERS[key])[:-2]:
        out(line)
    out("")
    value = secret("拿到 Key 后粘贴到这里（不会显示），按回车；暂时没有就直接回车 > ")
    if not value.strip():
        out("好的，之后拿到 Key 再运行：bin/uci setup translation key")
        return False
    return save_key(settings, value, out=out)


def offer(*, interactive: bool, read: Callable[[str], str] = input, out: Out = print) -> None:
    """After the user turns subtitles on: say what translation needs and let them decide whether to connect a model."""

    settings = online.load_settings()
    if settings.engine == "online" and not online.configured(settings):
        return
    if compat.WINDOWS:
        out("字幕要翻译成中文，需要接一个在线翻译模型（Windows 没有 Mac 那样的系统自带翻译）。")
        out("国内的 DeepSeek、通义千问、豆包、智谱都可以，费用很低，智谱的免费模型也能用。")
        question = "现在接入吗？[y] 现在接  [n] 以后再说（以后运行 bin/uci setup translation）> "
    else:
        out("字幕默认用 Mac 自带的 Apple 翻译（免费、离线）。也可以接一个在线模型（DeepSeek、通义千问等），翻译更自然，费用很低。")
        question = "要接在线模型吗？[y] 现在接  [n] 先用 Apple 翻译（以后可运行 bin/uci setup translation）> "
    if not interactive:
        out("需要接入时运行：bin/uci setup translation list（看可选模型），再运行 bin/uci setup translation use <名字>")
        return
    if read(question).strip().lower() in {"y", "yes"}:
        out("")
        wizard(read=read, out=out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="uci setup translation", description="字幕翻译用的在线模型")
    parser.add_argument("action", nargs="?", default="status",
                        choices=("status", "list", "use", "key", "test", "apple", "wizard"))
    parser.add_argument("provider", nargs="?", help="use：" + " / ".join(online.RECOMMENDED))
    parser.add_argument("--model", default="", help="模型名（不填用推荐的）")
    parser.add_argument("--base-url", default="", help="API 地址（只有“custom”需要）")
    args = parser.parse_args(argv)
    interactive = sys.stdin.isatty()
    if args.action == "status":
        for line in status_lines():
            print(line)
        settings = online.load_settings()
        if settings.engine == "online" and online.configured(settings):
            print("\n可选的模型：")
            for line in choices_text():
                print(line)
            print("\n选一个：bin/uci setup translation use deepseek（或 qwen / doubao / glm / custom）")
        return 0
    if args.action == "list":
        print("可选的翻译模型：")
        for line in choices_text():
            print(line)
        print("\n选一个：bin/uci setup translation use <名字>，名字依次是：" + "、".join(online.RECOMMENDED))
        return 0
    if args.action == "wizard":
        if not interactive:
            raise RuntimeError("向导需要在终端里运行；也可以用 bin/uci setup translation use <名字>")
        return 0 if wizard() else 1
    if args.action == "use":
        if not args.provider:
            raise RuntimeError("请说明用哪一个：" + "、".join(online.RECOMMENDED))
        settings = use(args.provider, model=args.model, base_url=args.base_url)
        if online.api_key(settings.provider):
            print("这个模型的 Key 之前已经保存过，直接试一下：")
            return 0 if check(settings) else 1
        print("")
        for line in guide_lines(online.PROVIDERS[args.provider]):
            print(line)
        return 0
    if args.action == "apple":
        if compat.WINDOWS:
            raise RuntimeError("Windows 没有 Apple 翻译，只能用在线模型。")
        store.update_user_config({"translation": {"engine": "apple"}})
        print("✓ 字幕翻译改回 Apple 翻译。")
        return 0
    settings = online.load_settings()
    if not settings.provider:
        raise RuntimeError("还没选模型，先运行 bin/uci setup translation list")
    if args.action == "key":
        if interactive:
            name = online.PROVIDERS[settings.provider].name if settings.provider in online.PROVIDERS else settings.provider
            value = getpass.getpass(f"粘贴 {name} 的 API Key（不会显示），按回车 > ")
        else:
            value = sys.stdin.readline()
        return 0 if save_key(settings, value) else 1
    return 0 if check(settings) else 1
