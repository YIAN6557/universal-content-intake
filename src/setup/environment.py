"""Local environment checks (``uci-setup doctor``) and tool builds (``uci-setup build-tools``)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.core.video_stage3_pipeline import (
    FONT_FILE, PROJECT_ROOT, SMALL_MODEL, TRANSLATION_HELPER, VAD_CLI, VAD_MODEL, WHISPER_CLI, WHISPER_ROOT,
)
from src.providers.video import DENO_PATH, FFMPEG_PATH, FFPROBE_PATH, YTDLP_PATH, locate_tool

AGENT = "Agent"
HUMAN = "本人"

WHISPER_TAG = "v1.9.4"
WHISPER_SOURCE = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{WHISPER_TAG}.tar.gz"
PDFINFO_DIR = PROJECT_ROOT / "apple-helper" / "PDFInfo"
TRANSLATION_DIR = PROJECT_ROOT / "apple-helper" / "Translation"


@dataclass(frozen=True)
class Model:
    path: Path
    url: str
    sha256: str
    size: int


MODELS = (
    Model(SMALL_MODEL, "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
          "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b", 487601967),
    Model(VAD_MODEL, "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin",
          "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987", 885098),
)


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool
    who: str = AGENT
    detail: str = ""
    fix: str = ""
    required: bool = True


def _run(command: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def clasp_path() -> Path:
    return locate_tool("clasp", "UCI_CLASP_PATH")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def model_ok(model: Model) -> bool:
    return model.path.is_file() and model.path.stat().st_size == model.size and sha256_of(model.path) == model.sha256


def _pip(package: str) -> str:
    return f"{sys.executable} -m pip install --user -U {package}"


def _brew_or(package: str, fallback: str) -> str:
    return f"brew install {package}" if shutil.which("brew") else fallback


def translation_status(source: str = "en", target: str = "zh-Hans") -> tuple[bool, str]:
    if not _executable(TRANSLATION_HELPER):
        return False, "翻译小工具还没编译"
    result = _run([str(TRANSLATION_HELPER), "preflight", "--source", source, "--target", target], timeout=120)
    if result is None:
        return False, "翻译小工具无法运行"
    try:
        status = str(json.loads(result.stdout).get("status") or "")
    except (ValueError, AttributeError):
        status = ""
    return status == "ready", f"{source}→{target}：{status or '未知'}"


def run_checks(*, deep: bool = True) -> list[Check]:
    checks: list[Check] = []
    mac = platform.mac_ver()[0]
    major = int(mac.split(".")[0]) if mac else 0
    checks.append(Check("macos", "macOS 15 或更新", sys.platform == "darwin" and major >= 15, HUMAN,
                        f"当前 {mac or sys.platform}", "升级 macOS（Apple 翻译需要 macOS 15+）"))
    checks.append(Check("python", "Python 3.11+", sys.version_info >= (3, 11), HUMAN,
                        f"{sys.executable} {platform.python_version()}", "从 python.org 安装 Python 3.11 或更新版本"))
    checks.append(Check("certifi", "Python 包 certifi", importlib.util.find_spec("certifi") is not None,
                        fix=_pip("certifi")))
    checks.append(Check("trafilatura", "Python 包 trafilatura（仅文章/网页抓取用）",
                        importlib.util.find_spec("trafilatura") is not None, fix=_pip("trafilatura"), required=False))
    clt = _run(["/usr/bin/xcode-select", "-p"])
    checks.append(Check("xcode", "Xcode 命令行工具（含 swift）", bool(clt and clt.returncode == 0) and bool(shutil.which("swift")),
                        HUMAN, fix="运行 xcode-select --install，在弹窗里点“安装”"))
    for key, path, fix in (
        ("ffmpeg", FFMPEG_PATH, _brew_or("ffmpeg", "下载 ffmpeg 静态版放到 ~/bin，或设置 UCI_FFMPEG_PATH")),
        ("ffprobe", FFPROBE_PATH, _brew_or("ffmpeg", "下载 ffprobe 静态版放到 ~/bin，或设置 UCI_FFPROBE_PATH")),
        ("yt-dlp", YTDLP_PATH, _pip('"yt-dlp[default]"')),
        ("deno", DENO_PATH, _brew_or("deno", "curl -fsSL https://deno.land/install.sh | sh")),
    ):
        checks.append(Check(key, key, _executable(path), detail=str(path) if _executable(path) else "未找到", fix=fix))
    node = shutil.which("node") or str(locate_tool("node", "UCI_NODE_PATH"))
    checks.append(Check("node", "Node.js（clasp 需要）", _executable(Path(node)), HUMAN if not shutil.which("brew") else AGENT,
                        fix=_brew_or("node", "从 nodejs.org 下载安装 Node.js LTS")))
    clasp = clasp_path()
    checks.append(Check("clasp", "clasp（推送云端代码）", _executable(clasp), detail=str(clasp) if _executable(clasp) else "未找到",
                        fix="npm install -g @google/clasp"))
    build = "bin/uci-setup build-tools"
    for model in MODELS:
        present = model.path.is_file()
        ok = model_ok(model) if (deep and present) else present
        checks.append(Check(f"model:{model.path.name}", f"模型 {model.path.name}", ok,
                            detail="SHA-256 已校验" if ok and deep else ("文件不完整或校验失败" if present else "未下载"), fix=build))
    whisper_ok = _executable(WHISPER_CLI) and _executable(VAD_CLI)
    checks.append(Check("whisper", "whisper.cpp（语音识别）", whisper_ok, fix=build))
    if not whisper_ok:
        checks.append(Check("cmake", "cmake（编译 whisper.cpp 用）", bool(shutil.which("cmake")),
                            fix=_brew_or("cmake", _pip("cmake"))))
    checks.append(Check("translation-helper", "Apple 翻译小工具", _executable(TRANSLATION_HELPER), fix=build))
    checks.append(Check("pdfinfo-helper", "PDF 信息小工具（仅文档抓取用）", _executable(PDFINFO_DIR / "bin" / "uci-pdfinfo"),
                        fix=build, required=False))
    checks.append(Check("font", "字幕字体 Noto Sans CJK SC", FONT_FILE.is_file(), detail=str(FONT_FILE.name)))
    if deep and _executable(TRANSLATION_HELPER):
        ready, detail = translation_status()
        checks.append(Check("translation-pack", "Apple 翻译语言包（英→简中）", ready, HUMAN, detail,
                            "系统设置 → 通用 → 语言与地区 → 翻译语言，下载“英语”和“中文（简体）”"))
    return checks


# --- build-tools ---------------------------------------------------------

def _tls() -> ssl.SSLContext:
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def download(url: str, destination: Path, *, log: Callable[[str], None] = print) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "universal-content-intake-setup"})
    with urllib.request.urlopen(request, timeout=60, context=_tls()) as response, partial.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        last = -1
        for block in iter(lambda: response.read(1 << 20), b""):
            handle.write(block)
            done += len(block)
            if total and done * 10 // total != last:
                last = done * 10 // total
                log(f"  {destination.name}: {done * 100 // total}%")
    partial.replace(destination)


def ensure_models(*, log: Callable[[str], None] = print) -> None:
    for model in MODELS:
        if model_ok(model):
            log(f"✓ {model.path.name} 已存在且校验通过")
            continue
        log(f"下载 {model.path.name}（{model.size / 1e6:.0f} MB）…")
        download(model.url, model.path, log=log)
        if not model_ok(model):
            model.path.unlink(missing_ok=True)
            raise RuntimeError(f"{model.path.name} 的 SHA-256 与预期不符，已删除。请稍后重试或检查来源。")
        log(f"✓ {model.path.name} 校验通过")


def _check(command: list[str], *, cwd: Path | None = None, log: Callable[[str], None] = print) -> None:
    log("$ " + " ".join(command))
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-15:])
        raise RuntimeError(f"命令失败（退出码 {result.returncode}）：{' '.join(command)}\n{tail}")


def build_whisper(*, log: Callable[[str], None] = print) -> None:
    if _executable(WHISPER_CLI) and _executable(VAD_CLI):
        log("✓ whisper.cpp 已编译")
        return
    cmake = shutil.which("cmake") or str(locate_tool("cmake", "UCI_CMAKE_PATH"))
    if not _executable(Path(cmake)):
        raise RuntimeError("需要 cmake：" + _brew_or("cmake", _pip("cmake")))
    with tempfile.TemporaryDirectory(prefix="uci-whisper-") as temporary:
        archive = Path(temporary) / "whisper.tar.gz"
        log(f"下载 whisper.cpp {WHISPER_TAG} 源码…")
        download(WHISPER_SOURCE, archive, log=log)
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        source = next(path for path in Path(temporary).iterdir() if path.is_dir() and path.name.startswith("whisper.cpp"))
        build = source / "build"
        _check([cmake, "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF",
                "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF"], log=log)
        _check([cmake, "--build", str(build), "--config", "Release", "-j", str(os.cpu_count() or 4),
                "--target", "whisper-cli", "whisper-vad-speech-segments"], log=log)
        (WHISPER_ROOT / "bin").mkdir(parents=True, exist_ok=True)
        for name in ("whisper-cli", "whisper-vad-speech-segments"):
            shutil.copy2(build / "bin" / name, WHISPER_ROOT / "bin" / name)
        shutil.copy2(source / "LICENSE", WHISPER_ROOT / "LICENSE")
    log("✓ whisper.cpp 编译完成")


def build_swift_helpers(*, log: Callable[[str], None] = print) -> None:
    _check(["swift", "build", "-c", "release", "--package-path", str(TRANSLATION_DIR)], log=log)
    bin_path = subprocess.run(["swift", "build", "-c", "release", "--package-path", str(TRANSLATION_DIR), "--show-bin-path"],
                              capture_output=True, text=True, check=True).stdout.strip()
    (TRANSLATION_HELPER.parent).mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(bin_path) / "uci-translation", TRANSLATION_HELPER)
    (PDFINFO_DIR / "bin").mkdir(parents=True, exist_ok=True)
    _check(["swiftc", "-O", str(PDFINFO_DIR / "main.swift"), "-o", str(PDFINFO_DIR / "bin" / "uci-pdfinfo")], log=log)
    log("✓ Swift 小工具编译完成")


def build_tools(*, models: bool = True, whisper: bool = True, swift: bool = True, log: Callable[[str], None] = print) -> None:
    if models:
        ensure_models(log=log)
    if whisper:
        build_whisper(log=log)
    if swift:
        build_swift_helpers(log=log)
