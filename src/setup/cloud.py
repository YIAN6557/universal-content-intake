"""Google side of setup: clasp project, Web App deployment, shared secret, signed setup calls."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from src.core.policies import PROJECT_DEFAULTS_PATH
from src.queue.client import QueueClient
from src.queue.secrets import KeychainSecretProvider, SecretProviderError
from src.setup import store
from src.setup.environment import PROJECT_ROOT, clasp_path

CLOUD_DIR = PROJECT_ROOT / "cloud" / "apps-script"
CLASP_JSON = PROJECT_ROOT / ".clasp.json"
CLASPRC = Path.home() / ".clasprc.json"
DEFAULT_TITLE = "Universal Content Intake"
DEPLOYMENT_DESCRIPTION = "Universal Content Intake Queue API"
HMAC_SERVICE = "UCI Queue API HMAC"
HMAC_ACCOUNT = "queue-api"
APPS_SCRIPT_API_SETTINGS = "https://script.google.com/home/usersettings"
DEPLOYMENT_ID = re.compile(r"\b(AKfyc[A-Za-z0-9_-]{20,})\b")


class SetupError(RuntimeError):
    """A step cannot continue; the message tells the user what to do."""


def clasp_logged_in() -> bool:
    return CLASPRC.is_file()


def cloud_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(CLOUD_DIR.iterdir()):
        if path.suffix in {".gs", ".json"}:
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def _clasp(args: list[str], *, cwd: Path = PROJECT_ROOT, timeout: float = 300) -> str:
    command = [str(clasp_path()), *args]
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise SetupError("找不到 clasp。请先按 bin/uci-setup doctor 的提示安装 clasp") from None
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        if "Apps Script API" in output or "usersettings" in output:
            raise SetupError(
                "Google 账号还没有开启 Apps Script API（只需开一次，必须本人操作）：\n"
                f"  打开 {APPS_SCRIPT_API_SETTINGS} ，把“Google Apps Script API”切换为“开启”，然后重新运行这一步。"
            )
        if "login" in output.lower() and ("not logged" in output.lower() or "credentials" in output.lower()):
            raise SetupError("clasp 还没登录。请本人在终端运行：clasp login（会打开浏览器，选择你的 Google 账号并同意）")
        raise SetupError(f"clasp {' '.join(args)} 失败：\n{output[-1500:]}")
    return output


def project_ids() -> dict[str, str]:
    if not CLASP_JSON.is_file():
        return {}
    value = json.loads(CLASP_JSON.read_text(encoding="utf-8"))
    parent = value.get("parentId") or []
    return {"script_id": str(value.get("scriptId") or ""), "spreadsheet_id": str(parent[0]) if parent else ""}


def editor_url(script_id: str) -> str:
    return f"https://script.google.com/d/{script_id}/edit"


def spreadsheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def create_project(title: str = DEFAULT_TITLE) -> dict[str, str]:
    """Create a new Google Sheet with a bound Apps Script project and point .clasp.json at our code."""

    if CLASP_JSON.is_file():
        raise SetupError(f"已经有云端项目（{CLASP_JSON.name}）。如需重建，先把该文件改名备份。")
    if not clasp_logged_in():
        raise SetupError("clasp 还没登录。请本人在终端运行：clasp login")
    # Create in a scratch folder so clasp cannot overwrite the project's appsscript.json.
    with tempfile.TemporaryDirectory(prefix="uci-clasp-") as scratch:
        _clasp(["create-script", "--type", "sheets", "--title", title], cwd=Path(scratch))
        created = json.loads((Path(scratch) / ".clasp.json").read_text(encoding="utf-8"))
    script_id = str(created.get("scriptId") or "")
    if not script_id:
        raise SetupError("clasp 没有返回脚本 ID。")
    settings = {"scriptId": script_id, "rootDir": str(CLOUD_DIR.relative_to(PROJECT_ROOT))}
    if created.get("parentId"):
        settings["parentId"] = created["parentId"]
    CLASP_JSON.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    ids = project_ids()
    store.update_state(script_id=ids["script_id"], spreadsheet_id=ids["spreadsheet_id"])
    return ids


def push() -> str:
    if not CLASP_JSON.is_file():
        raise SetupError("还没有云端项目，先运行：bin/uci-setup cloud create")
    _clasp(["push", "--force"])
    digest = cloud_digest()
    store.update_state(pushed_digest=digest)
    return digest


def deploy() -> str:
    """Create (or update) the Web App deployment and record its /exec URL in the user config."""

    state = store.read_state()
    deployment_id = str(state.get("deployment_id") or "")
    if deployment_id:
        output = _clasp(["create-deployment", "--deploymentId", deployment_id, "--description", DEPLOYMENT_DESCRIPTION])
    else:
        output = _clasp(["create-deployment", "--description", DEPLOYMENT_DESCRIPTION])
        match = DEPLOYMENT_ID.search(output)
        if not match:
            raise SetupError(f"没能从 clasp 输出中读到部署 ID：\n{output[-800:]}")
        deployment_id = match.group(1)
    url = f"https://script.google.com/macros/s/{deployment_id}/exec"
    store.update_state(deployment_id=deployment_id, deployed_digest=state.get("pushed_digest"))
    store.update_user_config({"queue_api_url": url})
    return url


# --- shared secret ---------------------------------------------------------

def secret_exists() -> bool:
    try:
        KeychainSecretProvider(service=HMAC_SERVICE, account=HMAC_ACCOUNT).get_secret()
        return True
    except SecretProviderError:
        return False


def create_secret(*, rotate: bool = False) -> None:
    if secret_exists() and not rotate:
        raise SetupError("钥匙串里已有共享密钥。要换新的请加 --rotate（之后要把新值重新粘贴到云端）。")
    value = secrets.token_urlsafe(48)
    # `security -i` reads the command from stdin, so the value never appears in a process listing.
    command = f'add-generic-password -U -s "{HMAC_SERVICE}" -a "{HMAC_ACCOUNT}" -w "{value}"\n'
    result = subprocess.run(["/usr/bin/security", "-i"], input=command, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode != 0 or not secret_exists():
        raise SetupError("无法写入 macOS 钥匙串。请确认钥匙串已解锁后重试。")


def copy_secret_to_clipboard() -> None:
    try:
        value = KeychainSecretProvider(service=HMAC_SERVICE, account=HMAC_ACCOUNT).get_secret()
    except SecretProviderError:
        raise SetupError("钥匙串里没有共享密钥，先运行：bin/uci-setup secret create") from None
    subprocess.run(["/usr/bin/pbcopy"], input=value, text=True, check=True, timeout=10)


# --- signed setup calls ------------------------------------------------------

def client() -> QueueClient:
    return QueueClient.from_defaults(PROJECT_DEFAULTS_PATH, timeout_seconds=60)


def inspect() -> Mapping[str, Any]:
    return client().setup("setup_inspect")


def config_set(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return client().setup("setup_config_set", values=dict(values))


def creators_upsert(creators: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    return client().setup("setup_creators_upsert", creators=[dict(item) for item in creators])
