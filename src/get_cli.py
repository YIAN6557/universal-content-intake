"""``bin/uci``: download one link with the right tool.

If the request says what to fetch ("下载这个链接里的 PDF", ``--type video``), that
type is used as is and nothing is probed. Otherwise the type is decided from the
link: known platforms first, then the file extension, then what the server
returns (Content-Type, and for HTML pages whether there is an article to extract).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from src.core.job import ContentType, Job
from src.core.policies import PROJECT_DEFAULTS_PATH, load_config, load_default_policies
from src.output.paths import OutputWorkspace
from src.providers.video import YTDLP_PATH

PROJECT_ROOT = Path(__file__).resolve().parents[1]
URL = re.compile(r"(?:https?|rclone)://\S+", re.IGNORECASE)

# Words in the request that name what to download, most specific first.
TYPE_WORDS: tuple[tuple[ContentType, tuple[str, ...]], ...] = (
    (ContentType.IMAGE_SET, ("图集", "相册", "组图", "所有图片", "全部图片", "gallery", "album", "images", "photos")),
    (ContentType.IMAGE, ("图片", "照片", "image", "photo", "picture", "jpg", "png")),
    (ContentType.VIDEO, ("视频", "影片", "短片", "video", "mp4")),
    (ContentType.DOCUMENT, ("pdf", "文档", "文件", "资料", "电子书", "表格", "幻灯片", "document", "doc", "docx", "word",
                            "excel", "xlsx", "ppt", "pptx", "epub", "file")),
    (ContentType.WEBPAGE, ("网页", "整个页面", "页面", "离线", "webpage", "page")),
    (ContentType.ARTICLE, ("文章", "正文", "新闻", "article")),
)
WEAK_TYPES = (ContentType.WEBPAGE, ContentType.ARTICLE)
TYPE_NAMES = {
    "video": ContentType.VIDEO, "document": ContentType.DOCUMENT, "pdf": ContentType.DOCUMENT,
    "image": ContentType.IMAGE, "images": ContentType.IMAGE_SET, "gallery": ContentType.IMAGE_SET,
    "article": ContentType.ARTICLE, "webpage": ContentType.WEBPAGE,
}
LABELS = {
    ContentType.VIDEO: "视频", ContentType.DOCUMENT: "文档", ContentType.IMAGE: "图片",
    ContentType.IMAGE_SET: "图集", ContentType.ARTICLE: "文章", ContentType.WEBPAGE: "网页",
}
COMMANDS = {
    ContentType.VIDEO: "video-run", ContentType.DOCUMENT: "document-run", ContentType.IMAGE: "image-run",
    ContentType.IMAGE_SET: "image-run", ContentType.ARTICLE: "article-run", ContentType.WEBPAGE: "webpage-run",
}

VIDEO_HOSTS = ("youtube.com", "youtu.be", "bilibili.com", "b23.tv", "vimeo.com", "dailymotion.com", "tiktok.com",
               "douyin.com", "ixigua.com", "twitch.tv", "v.qq.com", "youku.com", "nicovideo.jp", "ted.com")
DOCUMENT_HOSTS = ("drive.google.com", "docs.google.com")
GALLERY_HOSTS = ("imgur.com", "flickr.com", "pixiv.net", "artstation.com", "deviantart.com", "pinterest.com")
# Posts on these sites hold either a video or pictures; yt-dlp tells which.
MIXED_HOSTS = ("x.com", "twitter.com", "instagram.com", "facebook.com", "reddit.com", "weibo.com", "xiaohongshu.com")
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf",
                       ".txt", ".csv", ".md", ".epub", ".zip", ".7z", ".rar", ".tar", ".gz", ".key", ".pages", ".numbers"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif", ".bmp", ".tif", ".tiff", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".m3u8", ".flv"}
ARTICLE_MIN_CHARS = 600


class GetError(RuntimeError):
    """The download cannot continue; the message says why."""


@dataclass(frozen=True)
class Probe:
    content_type: str
    disposition: str
    body: str


@dataclass(frozen=True)
class Decision:
    content_type: ContentType
    reason: str
    declared: bool


def _host_matches(host: str, domains: Sequence[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def parse_request(words: Sequence[str]) -> tuple[str, ContentType | None]:
    """Split a request like "帮我下载这个链接里的PDF https://…" into the link and a named type."""

    text = " ".join(words)
    links = URL.findall(text)
    if len(links) != 1:
        raise GetError("请给出一个链接（http/https 开头）。" if not links else "一次只能下载一个链接。")
    link = links[0].rstrip("，。、）)>\"'")
    rest = text.replace(links[0], " ").casefold()
    found = []
    for content_type, keywords in TYPE_WORDS:
        for word in keywords:
            pattern = rf"(?<![a-z]){re.escape(word)}(?![a-z])" if word.isascii() else re.escape(word)
            if re.search(pattern, rest):
                found.append(content_type)
                break
    # "图集" also contains "图"-type words; keep the most specific of the image kinds.
    if ContentType.IMAGE_SET in found and ContentType.IMAGE in found:
        found.remove(ContentType.IMAGE)
    # "这个网页里的视频" / "这篇文章的PDF": the page or article is where it lives, not what to get.
    if any(item not in WEAK_TYPES for item in found):
        found = [item for item in found if item not in WEAK_TYPES]
    if len(found) > 1:
        names = "、".join(LABELS[item] for item in found)
        raise GetError(f"请求里同时提到了{names}，请只说一种，或用 --type 指定。")
    return link, (found[0] if found else None)


def _default_probe(url: str) -> Probe:
    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) universal-content-intake"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            disposition = str(response.headers.get("Content-Disposition") or "")
            body = response.read(2_000_000).decode("utf-8", errors="replace") if "html" in content_type else ""
    except urllib.error.HTTPError as error:
        raise GetError(f"打不开这个链接（HTTP {error.code}）。如果知道它是什么，可以直接说明类型，例如 --type pdf。") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise GetError(f"打不开这个链接（{getattr(error, 'reason', error)}）。") from None
    return Probe(content_type, disposition, body)


def _article_chars(html: str) -> int:
    try:
        import trafilatura
    except ImportError:
        return 0
    return len(trafilatura.extract(html) or "")


def _ytdlp_finds_video(url: str) -> bool:
    try:
        result = subprocess.run([str(YTDLP_PATH), "--simulate", "--no-playlist", "--quiet", "--no-warnings", url],
                                capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def detect(
    url: str,
    *,
    probe: Callable[[str], Probe] = _default_probe,
    has_video: Callable[[str], bool] = _ytdlp_finds_video,
    article_chars: Callable[[str], int] = _article_chars,
) -> Decision:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() == "rclone":
        return Decision(ContentType.DOCUMENT, "rclone 云盘路径", False)
    if _host_matches(host, VIDEO_HOSTS):
        return Decision(ContentType.VIDEO, f"{host} 是视频网站", False)
    if _host_matches(host, DOCUMENT_HOSTS):
        return Decision(ContentType.DOCUMENT, "Google 云端硬盘/文档链接", False)
    if _host_matches(host, GALLERY_HOSTS):
        return Decision(ContentType.IMAGE_SET, f"{host} 是图片网站", False)
    if _host_matches(host, MIXED_HOSTS):
        if has_video(url):
            return Decision(ContentType.VIDEO, f"{host} 的这条内容里有视频", False)
        return Decision(ContentType.IMAGE_SET, f"{host} 的这条内容里没有视频，按图片下载", False)
    extension = Path(parts.path).suffix.lower()
    if extension in DOCUMENT_EXTENSIONS:
        return Decision(ContentType.DOCUMENT, f"链接指向 {extension} 文件", False)
    if extension in IMAGE_EXTENSIONS:
        return Decision(ContentType.IMAGE, f"链接指向 {extension} 图片", False)
    if extension in VIDEO_EXTENSIONS:
        return Decision(ContentType.VIDEO, f"链接指向 {extension} 视频", False)
    found = probe(url)
    mime = found.content_type.split(";", 1)[0].strip()
    if mime.startswith("video/") or mime in {"application/vnd.apple.mpegurl", "application/x-mpegurl"}:
        return Decision(ContentType.VIDEO, f"服务器返回 {mime}", False)
    if mime.startswith("image/"):
        return Decision(ContentType.IMAGE, f"服务器返回 {mime}", False)
    if mime and "html" not in mime:
        return Decision(ContentType.DOCUMENT, f"服务器返回 {mime} 文件", False)
    if "attachment" in found.disposition.lower():
        return Decision(ContentType.DOCUMENT, "服务器以附件形式返回", False)
    if article_chars(found.body) >= ARTICLE_MIN_CHARS:
        return Decision(ContentType.ARTICLE, "网页里有完整的正文", False)
    return Decision(ContentType.WEBPAGE, "普通网页，整页保存", False)


def _run(step: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "src.cli", *step], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)


def download(
    url: str,
    content_type: ContentType,
    *,
    quality: str | None = None,
    chinese_subtitles: bool = False,
    output_root: Path | None = None,
    log: Callable[[str], None] = print,
) -> list[Path]:
    root = output_root or load_default_policies(PROJECT_DEFAULTS_PATH).output_root
    job_id = str(uuid.uuid4())
    job_file = OutputWorkspace.for_job(root, job_id).paths.job_dir / "job.json"
    first = [COMMANDS[content_type], "--url", url, "--workspace-root", str(root), "--job-id", job_id]
    if content_type is ContentType.VIDEO and quality:
        first += ["--quality", quality]
    if content_type in {ContentType.IMAGE, ContentType.IMAGE_SET}:
        first += ["--type", content_type.value]
    steps = [first]
    if content_type is ContentType.VIDEO and chinese_subtitles:
        steps.append(["stage3-run", "--resume-job", str(job_file)])
    steps.append(["finalize-job", "--resume-job", str(job_file)])
    for step in steps:
        log(f"  → {step[0]}")
        result = _run(step)
        if result.returncode != 0:
            raise GetError(_failure(step[0], result, job_file))
    job = Job.from_json(job_file.read_text(encoding="utf-8"))
    output = Path(job.output_path)
    return sorted(path for path in output.rglob("*") if path.is_file())


def _failure(step: str, result: subprocess.CompletedProcess[str], job_file: Path) -> str:
    detail = ""
    try:
        payload = json.loads(result.stdout or "{}")
        error = payload.get("error") or (payload.get("job") or {}).get("error") or {}
        detail = str(error.get("cause") or error.get("message") or "")
    except (ValueError, AttributeError):
        pass
    if not detail and job_file.is_file():
        error = Job.from_json(job_file.read_text(encoding="utf-8")).error
        detail = f"{error.code.value} {error.message}" if error else ""
    detail = detail or (result.stderr or "").strip()[-300:] or f"退出码 {result.returncode}"
    if step == "stage3-run" and result.returncode == 3:
        detail = "字幕阶段暂停（多半是缺少翻译语言包，见 系统设置 → 通用 → 语言与地区 → 翻译语言）"
    return f"{step} 没有完成：{detail.splitlines()[0][:300]}"


# Provider file names that say nothing about the content; delivered under the title instead.
GENERIC_STEMS = {"final", "article", "source_video", "content"}
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _info_title(info: Path | None) -> str:
    if info is None:
        return ""
    text = info.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^- (?:Original Title|Title): *(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _safe_name(value: str) -> str:
    return re.sub(r"\s+", " ", UNSAFE_NAME.sub(" ", value)).strip(" .")[:80]


def _free(path: Path) -> Path:
    candidate, counter = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    return candidate


def deliver(files: Sequence[Path], destination: Path) -> list[Path]:
    """Move a finished download to ``destination`` under readable names; never overwrite.

    One file lands directly in the folder with its description next to it
    ("<name> - 信息.md"); several files (a gallery, a saved webpage) go into a
    sub-folder named after the title.
    """

    info = next((path for path in files if path.name == "info.md"), None)
    content = [path for path in files if path is not info]
    title = _safe_name(_info_title(info)) or (content[0].stem if content else "download")
    destination.mkdir(parents=True, exist_ok=True)
    folder = destination if len(content) <= 1 else _free(destination / title)
    folder.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in content:
        name = f"{title}{path.suffix}" if path.stem.casefold() in GENERIC_STEMS else path.name
        target = _free(folder / name)
        shutil.move(str(path), target)
        moved.append(target)
    if info is not None:
        label = f"{moved[0].stem} - 信息.md" if len(content) == 1 else "信息.md"
        target = _free(folder / label)
        shutil.move(str(info), target)
        moved.append(target)
    return moved


def downloads_root() -> Path:
    output = load_config(PROJECT_DEFAULTS_PATH).get("output") or {}
    return Path(str(output.get("downloads_root") or "~/Downloads/")).expanduser()


def subtitle_tools_missing() -> list[str]:
    from src.setup import environment

    missing = [model.path.name for model in environment.MODELS if not model.path.is_file()]
    if not (environment.WHISPER_CLI.is_file() and environment.VAD_CLI.is_file()):
        missing.append("whisper.cpp")
    if not environment.TRANSLATION_HELPER.is_file():
        missing.append("Apple 翻译小工具")
    return missing


def decide_subtitles(
    *,
    zh: bool,
    no_zh: bool,
    preference: str | None,
    interactive: bool,
    read: Callable[[str], str] = input,
) -> bool:
    """Whether to translate and burn in Chinese subtitles for this video download."""

    if zh and no_zh:
        raise GetError("--zh 和 --no-zh 只能选一个。")
    if zh or no_zh:
        return zh
    if preference == "on":
        return True
    if preference == "off":
        return False
    if not interactive:
        raise GetError("当前设置为“每次询问”是否翻译压制中文字幕，但这里没法提问。"
                       "请先问用户，再加 --zh（翻译并压制）或 --no-zh（只下载原视频）重新运行。")
    while True:
        reply = read("这个视频要翻译成简体中文并压制字幕吗？[y] 要  [n] 不要 > ").strip().lower()
        if reply in {"y", "yes"}:
            return True
        if reply in {"n", "no"}:
            return False


def main(argv: list[str] | None = None, *, prog: str = "uci") -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog=prog,
        description="下载一个链接。说明了类型（例如“下载这个链接里的PDF”或 --type pdf）就直接下载，否则自动判断。",
    )
    parser.add_argument("request", nargs="+", help="链接，可以附带一句说明，例如：下载这个PDF https://…")
    parser.add_argument("--type", choices=sorted(TYPE_NAMES), help="直接指定类型，不做检测")
    parser.add_argument("--quality", choices=("720p", "1080p", "1440p", "2160p", "2k", "4k", "highest"),
                        help="视频画质上限，默认 1080p（不会放大）")
    parser.add_argument("--zh", action="store_true", help="视频：这次翻译成简体中文并压制字幕（不管设置是什么）")
    parser.add_argument("--no-zh", action="store_true", help="视频：这次只下载原视频（不管设置是什么）")
    parser.add_argument("--to", type=Path, help="保存到这个文件夹（默认：output.downloads_root，即“下载”文件夹）")
    parser.add_argument("--dry-run", action="store_true", help="只显示会用哪种方式下载，不下载")
    args = parser.parse_args(argv)
    try:
        url, named = parse_request(args.request)
        if args.type:
            decision = Decision(TYPE_NAMES[args.type], f"指定了 --type {args.type}", True)
        elif named is not None:
            decision = Decision(named, f"请求里说明了要{LABELS[named]}", True)
        else:
            decision = detect(url)
        how = "按你的说明，不做检测" if decision.declared else f"自动判断：{decision.reason}"
        print(f"类型：{LABELS[decision.content_type]}（{how}）")
        chinese = False
        if decision.content_type is ContentType.VIDEO:
            from src.setup import preferences

            chinese = decide_subtitles(zh=args.zh, no_zh=args.no_zh, preference=preferences.load().subtitles,
                                       interactive=sys.stdin.isatty())
            print("字幕：翻译成简体中文并压制" if chinese else "字幕：只下载原视频，不加字幕")
            if chinese and subtitle_tools_missing():
                raise GetError("翻译压制需要的语音识别模型和翻译工具还没装（" + "、".join(subtitle_tools_missing())
                               + "）。先运行 bin/uci setup build-tools，或这次加 --no-zh 只下载原视频。")
        elif args.zh or args.no_zh:
            print("提示：--zh / --no-zh 只对视频有效，已忽略。")
        if args.dry_run:
            return 0
        files = download(url, decision.content_type, quality=args.quality, chinese_subtitles=chinese)
        files = deliver(files, (args.to or downloads_root()).expanduser())
        print("✓ 下载完成：")
        for path in files:
            print(f"  {path}")
        return 0
    except GetError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
