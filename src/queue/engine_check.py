"""Every two weeks: check GitHub for new releases of the download engines and say which are out of date.

Nothing is installed. A macOS notification and ``bin/uci engines`` list each
engine that has a newer release, with the command that updates it. yt-dlp is
not in this list because it already updates itself weekly with a probe and
rollback (src.queue.ytdlp_update).

The LaunchAgent fires daily; the check itself runs only when the last completed
one is about 14 days old, so a Mac that was asleep or off that day is still
checked the next time it is awake. The latest release is read from the
``github.com/<repo>/releases/latest`` redirect, which is not subject to the
REST API's 60-requests-per-hour limit for anonymous callers.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from src.providers.video import DENO_PATH, locate_tool
from src.providers.webpage import DEFAULT_SINGLE_FILE_PATH

INTERVAL = timedelta(days=14)
# The agent fires at the same clock time every day; allow for a run that started a little later last time.
SLACK = timedelta(hours=12)
STATE_PATH = Path("~/Library/Application Support/Universal Content Intake/engine-check.json").expanduser()
VERSION = re.compile(r"\d+(?:\.\d+)+")

Fetcher = Callable[[str], str]


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    repo: str
    installed: Callable[[], str | None]
    update: Callable[[str], str]  # latest version -> how to update


@dataclass(frozen=True)
class Finding:
    key: str
    label: str
    repo: str
    installed: str | None
    latest: str | None
    newer: bool
    update: str = ""
    error: str = ""


def parse_version(text: str | None) -> tuple[int, ...] | None:
    match = VERSION.search(text or "")
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def is_newer(latest: str | None, installed: str | None) -> bool:
    new, old = parse_version(latest), parse_version(installed)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


# --- installed versions ----------------------------------------------------

def _cli_version(path: Path | str | None, *args: str) -> str | None:
    if not path:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    try:
        result = subprocess.run([str(path), *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = VERSION.search(result.stdout + "\n" + result.stderr)
    return match.group(0) if match else None


def _python_package(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _single_file() -> str | None:
    try:
        return str(json.loads((DEFAULT_SINGLE_FILE_PATH.parent / "package.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return None


def _whisper_cpp() -> str | None:
    # The binaries have no --version; build-tools records the release it built.
    from src.setup.environment import installed_whisper_version

    version = installed_whisper_version()
    return version.lstrip("v") if version else None


def _pip_update(package: str) -> Callable[[str], str]:
    return lambda latest: f"{sys.executable} -m pip install --user -U {package}"


def _brew_update(formula: str, fallback: str) -> Callable[[str], str]:
    return lambda latest: f"brew upgrade {formula}" if shutil.which("brew") else fallback


def _deno_update(latest: str) -> str:
    return "brew upgrade deno" if str(DENO_PATH).startswith(("/opt/homebrew", "/usr/local")) else "deno upgrade"


ENGINES: tuple[Engine, ...] = (
    Engine("gallery-dl", "gallery-dl（图片与图集）", "mikf/gallery-dl",
           lambda: _cli_version(locate_tool("gallery-dl", "UCI_GALLERY_DL_PATH"), "--version"), _pip_update("gallery-dl")),
    Engine("gdown", "gdown（Google 云端硬盘）", "wkentaro/gdown",
           lambda: _cli_version(locate_tool("gdown", "UCI_GDOWN_PATH"), "--version"), _pip_update("gdown")),
    Engine("trafilatura", "trafilatura（文章正文）", "adbar/trafilatura",
           lambda: _python_package("trafilatura"), _pip_update("trafilatura")),
    Engine("single-file", "SingleFile CLI（整页网页）", "gildas-lormeau/single-file-cli", _single_file,
           lambda latest: f'npm install --prefix "{DEFAULT_SINGLE_FILE_PATH.parents[2]}" single-file-cli@{latest}'),
    Engine("rclone", "rclone（云盘链接）", "rclone/rclone",
           lambda: _cli_version(shutil.which("rclone"), "version"), _brew_update("rclone", "从 rclone.org 下载新版本")),
    Engine("aria2", "aria2（大文件续传）", "aria2/aria2",
           lambda: _cli_version(shutil.which("aria2c"), "--version"), _brew_update("aria2", "从 aria2 的 GitHub 发布页下载新版本")),
    Engine("deno", "deno（yt-dlp 解析 YouTube 用）", "denoland/deno",
           lambda: _cli_version(DENO_PATH, "--version"), _deno_update),
    Engine("whisper.cpp", "whisper.cpp（语音识别，翻译压制用）", "ggml-org/whisper.cpp", _whisper_cpp,
           lambda latest: "bin/uci setup build-tools --skip-models --skip-swift --update-whisper"
                          "（编译新版本，用自带测试录音自检通过才替换，失败保留旧版本；约 2–5 分钟）"),
)


# --- latest releases -------------------------------------------------------

class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


def _tls() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def latest_release(repo: str) -> str:
    """The tag of the newest non-prerelease, e.g. ``v1.32.15``."""

    opener = urllib.request.build_opener(_KeepRedirect, urllib.request.HTTPSHandler(context=_tls()))
    request = urllib.request.Request(f"https://github.com/{repo}/releases/latest", method="HEAD",
                                     headers={"User-Agent": "universal-content-intake-engine-check"})
    location = ""
    try:
        with opener.open(request, timeout=30) as response:
            location = response.headers.get("Location", "")
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise RuntimeError(f"GitHub 返回 HTTP {error.code}") from None
        location = error.headers.get("Location", "")
    marker = "/releases/tag/"
    if marker not in location:
        raise RuntimeError("没有找到正式发布的版本")
    return urllib.parse.unquote(location.split(marker, 1)[1]).strip("/")


def check(engines: Sequence[Engine] = ENGINES, fetch: Fetcher = latest_release) -> list[Finding]:
    findings = []
    for engine in engines:
        installed = engine.installed()
        if installed is None:
            continue  # not installed here: nothing to keep up to date
        try:
            tag = fetch(engine.repo)
        except (OSError, RuntimeError, ValueError) as error:
            findings.append(Finding(engine.key, engine.label, engine.repo, installed, None, False, error=str(error) or type(error).__name__))
            continue
        latest = VERSION.search(tag).group(0) if VERSION.search(tag) else tag
        newer = is_newer(latest, installed)
        findings.append(Finding(engine.key, engine.label, engine.repo, installed, latest, newer,
                                update=engine.update(latest) if newer else ""))
    return findings


# --- state -----------------------------------------------------------------

def read_state(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(findings: list[Finding], now: datetime, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": now.isoformat(), "findings": [asdict(finding) for finding in findings]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def due(state: dict, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(str(state["checked_at"]))
    except (KeyError, ValueError):
        return True
    return now - last >= INTERVAL - SLACK


def summary_lines(state: dict) -> list[str]:
    """For bin/uci status: what the last check found, read from disk (no network)."""

    if not state.get("checked_at"):
        return ["下载引擎：还没有检查过新版本（每两周自动检查一次）。现在检查：bin/uci engines"]
    day = str(state["checked_at"])[:10]
    findings = [Finding(**item) for item in state.get("findings") or []]
    newer = [item for item in findings if item.newer]
    failed = [item for item in findings if item.error]
    tail = [f"  （另有 {len(failed)} 个没能查到：{'、'.join(item.key for item in failed)}，下次检查会再试）"] if failed else []
    if not newer:
        return [f"下载引擎：{day} 检查过，都是最新版本（每两周检查一次；yt-dlp 每周自动更新）。"] + tail
    lines = [f"下载引擎：{day} 检查时，{len(newer)} 个有新版本（只提醒，不会自动安装）："]
    for item in newer:
        lines.append(f"  · {item.label}：{item.installed} → {item.latest}")
        lines.append(f"    更新：{item.update}")
    return lines + tail


def report_lines(findings: list[Finding]) -> list[str]:
    lines = []
    for item in findings:
        if item.error:
            lines.append(f" ? {item.label}：本机 {item.installed}，没能查到最新版本（{item.error}）")
        elif item.newer:
            lines.append(f" ↑ {item.label}：本机 {item.installed}，GitHub 最新 {item.latest}")
            lines.append(f"     更新：{item.update}")
        else:
            lines.append(f" ✓ {item.label}：{item.installed}，已是最新")
    if not findings:
        lines.append("这台 Mac 上还没有安装可检查的下载引擎。")
    lines.append("yt-dlp 不在这里：它每周自动更新，并在新版本出问题时自动回滚。")
    return lines


def notify_text(findings: list[Finding]) -> tuple[str, str] | None:
    newer = [item for item in findings if item.newer]
    if not newer:
        return None
    names = "、".join(f"{item.key} {item.latest}" for item in newer)
    return f"UCI：{len(newer)} 个下载引擎有新版本", f"{names}。查看更新命令：bin/uci engines"


def run(*, force: bool, now: datetime, fetch: Fetcher = latest_release, engines: Sequence[Engine] = ENGINES,
        state_path: Path = STATE_PATH) -> list[Finding] | None:
    """None when the check is not due yet. A run where every lookup failed is not recorded, so it retries tomorrow."""

    if not force and not due(read_state(state_path), now):
        return None
    findings = check(engines, fetch)
    if any(item.latest for item in findings) or not findings:
        save_state(findings, now, state_path)
    return findings


def main(argv: list[str] | None = None, *, notifier: Callable[[str, str], None] | None = None,
         out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="uci engines", description="检查下载引擎在 GitHub 上有没有新版本（只提醒，不安装）")
    parser.add_argument("--scheduled", action="store_true", help="后台定时任务用：未满两周就跳过，有新版本时发系统通知")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("uci.engine_check")
    now = datetime.now(timezone.utc)
    findings = run(force=not args.scheduled, now=now)
    if findings is None:
        log.info("operation=engine_check status=NOT_DUE")
        return 0
    newer = [item.key for item in findings if item.newer]
    failed = [item.key for item in findings if item.error]
    log.info("operation=engine_check status=CHECKED newer=%s failed=%s", ",".join(newer) or "-", ",".join(failed) or "-")
    if args.scheduled:
        message = notify_text(findings)
        if message:
            if notifier is None:
                from src.queue.worker import macos_notifier as notifier
            notifier(*message)
        return 0
    out(f"下载引擎（{now.astimezone().strftime('%Y-%m-%d %H:%M')} 检查，对比各自 GitHub 上的最新正式版本）：")
    for line in report_lines(findings):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
