"""The few things that differ between macOS and Windows, kept in one place.

Everything else in UCI is plain Python plus cross-platform tools (yt-dlp,
ffmpeg, Chrome), so the rest of the code calls these helpers instead of
checking the platform itself.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
APP_NAME = "Universal Content Intake"


def data_dir() -> Path:
    """Where UCI keeps its own state (not the user's settings, which stay in ~/.config)."""

    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    return Path.home() / "Library" / "Application Support" / APP_NAME


def log_dir() -> Path:
    if WINDOWS:
        return data_dir() / "Logs"
    return Path.home() / "Library" / "Logs" / APP_NAME


def tool_dirs() -> list[Path]:
    """Places tools get installed to that a background job's short PATH may not include."""

    dirs = [Path(sysconfig.get_path("scripts")), Path(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))]
    if WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        dirs += [local / "Microsoft" / "WinGet" / "Links", Path.home() / "scoop" / "shims",
                 Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "npm"]
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    return dirs + [Path.home() / "bin"]


def executable_names(name: str) -> list[str]:
    """``ffmpeg`` is ``ffmpeg.exe`` on Windows; npm tools such as ``single-file`` are ``.cmd`` files."""

    if not WINDOWS or Path(name).suffix:
        return [name]
    return [name + suffix for suffix in (".exe", ".cmd", ".bat")]


def is_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if WINDOWS:
        return path.suffix.lower() in {".exe", ".cmd", ".bat", ".com"}
    return os.access(path, os.X_OK)


def lock(fd: int, *, blocking: bool = True) -> None:
    """Take an exclusive lock on an open lock file. Raises BlockingIOError when ``blocking`` is False and it is taken."""

    if WINDOWS:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise BlockingIOError("the lock is held by another process") from None
                time.sleep(0.2)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def unlock(fd: int) -> None:
    if WINDOWS:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def fsync_dir(path: Path) -> None:
    """Make a rename inside ``path`` durable. Windows cannot open a directory for this and does not need it."""

    if WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_private(fd: int) -> None:
    """Owner-only permissions for a file holding state. Windows user folders are already private."""

    if not WINDOWS:
        os.fchmod(fd, 0o600)


def new_process_group() -> dict[str, object]:
    """Popen arguments that start a child in its own group, so the whole group can be stopped later."""

    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process_group(process: subprocess.Popen, *, grace_seconds: float = 5) -> None:
    """Stop a child started with new_process_group() and everything it started (Chrome spawns helpers)."""

    if process.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait(timeout=grace_seconds)


def chrome_path() -> Path:
    if WINDOWS:
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if root:
                candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if candidate.is_file():
                    return candidate
        return Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files") / "Google" / "Chrome" / "Application" / "chrome.exe"
    return Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


# Windows PowerShell 5.1 ships with every Windows 10/11 and can raise a toast without extra modules.
# Title and text arrive through the environment so nothing has to be quoted into the script.
_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:UCI_TOAST_TITLE)) > $null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:UCI_TOAST_TEXT)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def notify(title: str, message: str) -> None:
    """Show a desktop notification. Best effort: never raises."""

    try:
        if WINDOWS:
            env = {**os.environ, "UCI_TOAST_TITLE": title, "UCI_TOAST_TEXT": message}
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST], env=env,
                           capture_output=True, timeout=20, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        elif MAC:
            script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
            subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
