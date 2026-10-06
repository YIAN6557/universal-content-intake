"""``bin/uci setup``: guided first-run setup. Run without arguments for progress and the next step."""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.core.policies import PROJECT_DEFAULTS_PATH, load_config
from src.queue.client import QueueApiError
from src.queue.status_cli import WORKER_LABEL
from src.setup import cloud, creators as creator_tools, environment, launchagent, preferences, store
from src.setup.environment import AGENT, HUMAN

SHEETS = ("Creators", "Baseline", "Videos", "Snapshots", "Config", "Queue", "QueueClaimRequests")
SCHEDULE_KEYS = ("production_timezone", "discovery_window_start", "discovery_window_end", "final_sweep_time",
                 "daily_selection_time", "rank2_start_cutoff", "daily_selection_max")
# Cloud settings the local Worker also reads.
MIRRORED_KEYS = {"production_timezone", "rank2_start_cutoff"}
# Settings that live only in the local user config.
LOCAL_KEYS = {
    "delivery_root": ("output", "delivery_root"),
    "output_root": ("output", "root"),
    "downloads_root": ("output", "downloads_root"),
    "target_resolution": ("video", "target_resolution"),
    "allow_browser_cookies": ("video", "allow_browser_cookies"),
}
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_KEY_URL = "https://aistudio.google.com/apikey"
CONFIRMABLE = ("preferences", "macos-permissions")
# A small, stable public PDF for the download-only acceptance check.
TEST_PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


@dataclass
class Step:
    key: str
    title: str
    who: str
    done: bool
    optional: bool = False
    detail: str = ""
    guide: list[str] = field(default_factory=list)


@dataclass
class Context:
    checks: list[environment.Check]
    state: dict[str, Any]
    ids: dict[str, str]
    config: dict[str, Any]
    secret: bool
    inspect: Mapping[str, Any] | None
    inspect_error: str
    prefs: preferences.Preferences = field(default_factory=lambda: preferences.Preferences(True, "on"))


def _inspect_error(error: QueueApiError) -> str:
    if error.code == "AUTH_FAILED":
        return "签名校验没通过：云端还没有粘贴共享密钥，或粘贴的值与本机钥匙串里的不一致。"
    if error.code == "API_RESPONSE_INVALID" and (error.detail or "").startswith("html"):
        return "云端返回了网页而不是数据：通常是还没在编辑器里运行 uciSetup 完成授权，或部署还没生效。"
    detail = f"（{error.detail}）" if error.detail else ""
    return f"{error.code}: {error.message}{detail}"


def gather(*, deep: bool = False, online: bool = True) -> Context:
    checks = environment.run_checks(deep=deep)
    config = load_config(PROJECT_DEFAULTS_PATH)
    secret = cloud.secret_exists()
    inspect: Mapping[str, Any] | None = None
    error = ""
    if online and preferences.load().monitoring and config.get("queue_api_url") and secret:
        try:
            inspect = cloud.inspect()
        except QueueApiError as failure:
            error = _inspect_error(failure)
    prefs = preferences.load()
    if not prefs.monitoring:
        inspect, error = None, ""
    return Context(checks, store.read_state(), cloud.project_ids(), config, secret, inspect, error, prefs)


def _check_lines(checks: list[environment.Check]) -> list[str]:
    return [f"[{check.who}] {check.label}：{check.fix}" for check in checks if not check.ok and check.required]


def build_steps(ctx: Context) -> list[Step]:
    by_key = {check.key: check for check in ctx.checks}
    prefs = ctx.prefs
    env_keys = ("macos", "python", "certifi", "ffmpeg", "ffprobe", "yt-dlp", "deno")
    if prefs.needs_subtitle_tools:
        env_keys += ("xcode", "cmake")
    if prefs.monitoring:
        env_keys += ("node", "clasp")
    tool_keys = ("model:ggml-small.bin", "model:ggml-silero-v6.2.0.bin", "whisper", "translation-helper", "font")
    env_checks = [by_key[key] for key in env_keys if key in by_key]
    tool_checks = [by_key[key] for key in tool_keys if key in by_key]
    state, ids, info = ctx.state, ctx.ids, ctx.inspect or {}
    settings = info.get("settings") or {}
    properties = info.get("properties") or {}
    steps: list[Step] = []

    env_parts = "Python、ffmpeg、yt-dlp、deno" + ("、Xcode 工具" if prefs.needs_subtitle_tools else "") + ("、Node、clasp" if prefs.monitoring else "")
    steps.append(Step("env", f"本机环境：{env_parts}", f"{AGENT}（Xcode 工具需{HUMAN}点确认）" if prefs.needs_subtitle_tools else AGENT,
                      all(check.ok for check in env_checks if check.required), guide=_check_lines(env_checks)))
    if prefs.needs_subtitle_tools:
        steps.append(Step("tools", "翻译压制工具：Whisper 语音识别模型、whisper.cpp、Apple 翻译小工具", AGENT,
                          all(check.ok for check in tool_checks),
                          guide=["运行 bin/uci setup build-tools（下载约 490 MB 模型并校验 SHA-256，编译 whisper.cpp 和 Swift 小工具）"]))
        ready, detail = environment.translation_status()
        steps.append(Step("translation", "Apple 翻译语言包（英→简中）", HUMAN, ready, detail=detail, guide=[
            "系统设置 → 通用 → 语言与地区 → 翻译语言，下载“英语”和“中文（简体）”。",
            "下载完成后重新运行 bin/uci setup 核对。"]))
    if not prefs.monitoring:
        steps.append(Step("verify-download", "验收：试下载一个文件" + ("，并试一次翻译压制" if prefs.needs_subtitle_tools else ""),
                          AGENT, bool(state.get("download_verified_at")),
                          guide=["运行 bin/uci setup verify（下载一个公开的测试 PDF"
                                 + ("，再下载一条 67 秒的 NASA 视频并翻译压制中文字幕" if prefs.needs_subtitle_tools else "")
                                 + "，结果放进“下载”文件夹里的“uci-setup 试跑”）"]))
        return steps
    steps.append(Step("clasp-login", "登录 clasp，开启 Apps Script API", HUMAN, cloud.clasp_logged_in(), guide=[
        "1. 在终端运行 clasp login，浏览器会打开，选择要存放数据的 Google 账号，点“允许”。",
        f"2. 打开 {cloud.APPS_SCRIPT_API_SETTINGS} ，把“Google Apps Script API”切到“开启”。",
        "这两件事只需要做一次。做完后重新运行 bin/uci setup。"]))
    digest = cloud.cloud_digest()
    pushed = bool(ids.get("script_id")) and state.get("pushed_digest") == digest
    steps.append(Step("cloud-create", "创建 Google 表格并推送云端代码", AGENT, pushed,
                      detail=f"表格：{cloud.spreadsheet_url(ids['spreadsheet_id'])}" if ids.get("spreadsheet_id") else "",
                      guide=["运行 bin/uci setup cloud push（云端代码有更新）" if ids.get("script_id")
                             else "运行 bin/uci setup cloud create（新建表格 + 绑定脚本 + 推送代码）"]))
    deployed = bool(ctx.config.get("queue_api_url")) and bool(state.get("deployment_id")) and state.get("deployed_digest") == state.get("pushed_digest")
    steps.append(Step("deploy", "部署 Queue API（Web App），写入本机配置", AGENT, deployed,
                      guide=["运行 bin/uci setup cloud deploy"]))
    steps.append(Step("secret", "生成共享密钥，存进本机钥匙串", AGENT, ctx.secret,
                      guide=["运行 bin/uci setup secret create（密钥不会显示在屏幕上）"]))
    sheets_ok = all((info.get("sheets") or {}).get(name) for name in SHEETS)
    authorized = ctx.inspect is not None and sheets_ok and info.get("scheduler_triggers") == 1
    editor = cloud.editor_url(ids["script_id"]) if ids.get("script_id") else "（先完成“创建 Google 表格”这一步）"
    steps.append(Step("authorize", "在 Apps Script 编辑器里粘贴密钥、运行 uciSetup 并授权", HUMAN, authorized,
                      detail=ctx.inspect_error, guide=[
        f"打开编辑器：运行 bin/uci setup cloud open（自动在浏览器里打开正确的项目），或手动打开 {editor}",
        "⚠ 先核对：页面左上角的项目名必须是“Universal Content Intake”。如果你的账号里还有别的 Apps Script 项目，"
        "不要在别的项目里操作，否则密钥和初始化都不会生效。",
        "⚠ 全程不要点进中间的代码区域，也不要在里面打字，只用左侧菜单、顶部按钮和设置页的输入框。",
        "A. 粘贴密钥：先在终端运行 bin/uci setup secret copy（密钥进剪贴板）→ 编辑器左侧齿轮“项目设置”→ 页面底部“脚本属性”"
        "→“添加脚本属性”，属性填 UCI_QUEUE_HMAC_SECRET，值处粘贴 → “保存脚本属性”。",
        "B. 运行初始化：左侧“编辑器”→ 在文件列表里点 setup.gs → 顶部函数下拉框选 uciSetup →“运行”→“审核权限”→ 选你的账号"
        " →（出现“Google 尚未验证此应用”时）点“高级”→“转至 Universal Content Intake（不安全）”→“允许”。",
        "   执行日志出现“Universal Content Intake setup complete”即成功。uciSetup 可重复运行，不会重复建表。",
        "   如果执行日志报 ReferenceError、SyntaxError 这类错误（例如“xx is not defined”），说明代码被误改了："
        "运行 bin/uci setup cloud push 恢复原样，再点一次“运行”。",
        "做完后重新运行 bin/uci setup，向导会自动核对。"]))
    has_gemini = bool(properties.get("UCI_GEMINI_API_KEY"))
    judge_on = settings.get("semantic_judge_enabled") is True
    skipped = store.is_confirmed(state, "skip-gemini")
    gemini_guide = [
        "Gemini Key 免费，约 2 分钟就能申请好。Gemini 在 Google 的服务器上调用，不经过你本机的网络。",
        "用它做两件事：一是按你写的内容方向判断每条视频合不合适（语义判断），二是写发布标题和文案。",
        f"1.（{HUMAN}）打开 {GEMINI_KEY_URL} ，用同一个 Google 账号登录；第一次打开要先同意服务条款。",
        "2.（本人）点“Create API key”（创建 API 密钥），项目选默认的或新建一个，复制生成的 Key（以 AIza 开头）。",
        "   不需要绑定付款方式，免费额度对这套系统足够。",
        "3.（本人）运行 bin/uci setup cloud open，在同一个“Universal Content Intake”项目的“项目设置 → 脚本属性”里"
        "添加 UCI_GEMINI_API_KEY，值粘贴这个 Key，保存。",
        f"4.（{AGENT}）运行 bin/uci setup config set semantic_judge_enabled=true semantic_gemini_model={GEMINI_MODEL}",
        "5.（本人，可选）让本机也用它写发布文案：在终端运行 security add-generic-password -s \"UCI Gemini API\" -a api-key -w ，按提示粘贴 Key。",
        "不想用的话可以跳过：bin/uci setup skip gemini。但要知道跳过的代价：",
        "  · 选片只剩时长、标题规则和热度，你写的内容方向不起作用，选出来的视频会更杂；",
        "  · 发布标题只是原标题的直译，文案从字幕里摘句子，质量明显差一截。",
    ]
    if has_gemini and not judge_on:
        gemini_guide = [line for line in gemini_guide if line.startswith("4.")]
    steps.append(Step("gemini", "（可选，推荐）Gemini：语义判断与发布文案", f"{HUMAN}申请 Key / {AGENT}开启", (has_gemini and judge_on) or skipped,
                      optional=True, detail="已跳过：选片只靠规则，发布文案为规则生成" if skipped and not has_gemini else "",
                      guide=gemini_guide))
    enabled = [item for item in info.get("creators") or [] if item.get("enabled") is True]
    pending = state.get("creators_pending") or []
    steps.append(Step("creators", "第一批作者白名单", f"{HUMAN}提供名单 / {AGENT}解析写入", bool(enabled),
                      detail=f"已启用 {len(enabled)} 个频道" if enabled else (f"有 {len(pending)} 个待确认" if pending else ""),
                      guide=["1.（本人）给出要监控的 YouTube 频道：@handle、频道链接或频道 ID 都可以。",
                             "2.（Agent）bin/uci setup creators resolve @handle1 @handle2 …  解析频道并抽查最近 20 条视频。",
                             "3.（本人）看过列表后确认，（Agent）运行 bin/uci setup creators apply 写入云端。"]))
    schedule = "，".join(f"{key}={settings.get(key)}" for key in SCHEDULE_KEYS if key in settings)
    steps.append(Step("preferences", "内容方向与监控时间", f"{HUMAN}决定 / {AGENT}写入",
                      store.is_confirmed(state, "preferences"), detail=schedule, guide=[
        "1.（Agent）bin/uci setup config show  列出当前时区、发现时段、补扫、选片时间、每日上限、时长上限、标题过滤规则和内容方向。",
        "2.（本人）说明想要/不要的内容和时间安排；（Agent）用 bin/uci setup config set key=value …"
        " 和 bin/uci setup config brief --file 内容方向.txt 写入（不合理的时间会被拒绝并说明原因）。"
        " 标题过滤默认不开启，可选规则：PODCAST、KEYNOTE、QA、LIVESTREAM、REVIEW、FINANCE、TUTORIAL、GAMING、NEWS_ROUNDUP、AD，"
        "例如 content_title_filters=PODCAST,AD。",
        "3.（本人）确认后：bin/uci setup confirm preferences"]))
    worker_ok = launchagent.is_loaded(WORKER_LABEL) and launchagent.installed_here()
    steps.append(Step("worker", "安装本机后台程序（LaunchAgent）", AGENT, worker_ok,
                      guide=["运行 bin/uci setup launchagent install"]))
    steps.append(Step("permissions", "macOS 权限：通知、下载文件夹和交付文件夹访问", HUMAN,
                      store.is_confirmed(state, "macos-permissions"), guide=[
        "后台程序第一次写入“下载”或“桌面/文稿”里的文件夹时，macOS 会弹窗询问是否允许 Python 访问，点“允许”。",
        "系统设置 → 通知：允许“脚本编辑器”发送通知（完成和失败提醒用）。",
        "确认后：bin/uci setup confirm macos-permissions"]))
    verified = bool(state.get("verified_at")) and settings.get("daily_selection_enabled") is True
    steps.append(Step("verify", "验收：连通性、配置自检、端到端试跑，然后开启每日选片", AGENT, verified,
                      guide=["运行 bin/uci setup verify（含一次约 1–3 分钟的本地试跑；跳过试跑加 --no-smoke）"]))
    return steps


def print_status(steps: list[Step], *, out: Callable[[str], None] = print,
                 prefs: preferences.Preferences | None = None) -> int:
    required = [step for step in steps if not step.optional]
    done = sum(1 for step in required if step.done)
    out(f"Universal Content Intake 配置 · 进度 {done}/{len(required)}（可选步骤不计）")
    if prefs is not None:
        out("  " + "；".join(preferences.describe(prefs)) + "（修改：bin/uci settings）")
    for index, step in enumerate(steps, start=1):
        mark = "✓" if step.done else ("○" if step.optional else "✗")
        line = f" {mark} {index:>2}. {step.title}  [{step.who}]"
        if step.detail and (not step.done or step.key in {"cloud-create", "creators", "preferences"}):
            line += f"\n        {step.detail}"
        out(line)
    pending = next((step for step in steps if not step.done), None)
    if pending is None:
        out("\n全部完成，系统可用。下载：bin/uci <链接>")
        if any(step.key == "verify" for step in steps):
            out("自动监控的运行情况：bin/uci status")
        return 0
    out(f"\n下一步 · 第 {steps.index(pending) + 1} 步：{pending.title}（由 {pending.who} 完成）")
    for line in pending.guide:
        out("  " + line)
    return 1


# --- commands ------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    ctx = gather(online=not args.offline)
    steps = build_steps(ctx)
    if args.json:
        print(json.dumps([asdict(step) for step in steps], ensure_ascii=False, indent=2))
        return 0 if all(step.done for step in steps) else 1
    return print_status(steps, prefs=ctx.prefs)


# Checks that only matter for one of the optional features.
SUBTITLE_ONLY_CHECKS = {"xcode", "cmake", "model:ggml-small.bin", "model:ggml-silero-v6.2.0.bin", "whisper",
                        "translation-helper", "font", "translation-pack"}
MONITORING_ONLY_CHECKS = {"node", "clasp"}


def relevant(check: environment.Check, prefs: preferences.Preferences) -> str:
    """Empty when the check applies; otherwise why it can be ignored."""

    if check.key in SUBTITLE_ONLY_CHECKS and not prefs.needs_subtitle_tools:
        return "开启翻译压制或自动监控时才需要"
    if check.key in MONITORING_ONLY_CHECKS and not prefs.monitoring:
        return "开启自动监控时才需要"
    return ""


def cmd_doctor(_: argparse.Namespace) -> int:
    failed = 0
    prefs = preferences.load()
    for check in environment.run_checks(deep=True):
        unused = relevant(check, prefs)
        required = check.required and not unused
        mark = "✓" if check.ok else ("○" if not required else "✗")
        failed += 0 if check.ok or not required else 1
        line = f" {mark} {check.label}" + (f"  {check.detail}" if check.detail else "")
        if not check.ok:
            line += f"  （{unused}）" if unused else f"\n      [{check.who}] {check.fix}"
        print(line)
    print("\n环境就绪。" if not failed else f"\n还有 {failed} 项未就绪。")
    return 1 if failed else 0


def cmd_build_tools(args: argparse.Namespace) -> int:
    environment.build_tools(models=not args.skip_models, whisper=not args.skip_whisper, swift=not args.skip_swift)
    return 0


def cmd_cloud(args: argparse.Namespace) -> int:
    if args.action == "create":
        ids = cloud.create_project(args.title)
        print(f"✓ 已创建表格：{cloud.spreadsheet_url(ids['spreadsheet_id'])}")
        cloud.push()
        print("✓ 已推送云端代码")
    elif args.action == "push":
        cloud.push()
        print("✓ 已推送云端代码；如果已经部署过，再运行 bin/uci setup cloud deploy 让 Web App 用上新代码")
    elif args.action == "deploy":
        url = cloud.deploy()
        print(f"✓ Queue API 已部署并写入 {store.config_path()}：{url}")
    elif args.action == "open":
        ids = cloud.project_ids()
        if not ids:
            raise cloud.SetupError("还没有云端项目，先运行 bin/uci setup cloud create")
        editor = cloud.editor_url(ids["script_id"])
        print(f"编辑器：{editor}\n表格：{cloud.spreadsheet_url(ids['spreadsheet_id'])}")
        if not args.no_browser:
            subprocess.run(["/usr/bin/open", editor], check=False)
            print("已在浏览器里打开编辑器。核对左上角项目名是“Universal Content Intake”再操作。")
    return 0


def cmd_secret(args: argparse.Namespace) -> int:
    if args.action == "create":
        cloud.create_secret(rotate=args.rotate)
        print("✓ 共享密钥已生成并存进钥匙串（服务名 “UCI Queue API HMAC”）。")
        print("  下一步把它粘贴到云端：bin/uci setup secret copy，然后按 bin/uci setup 的提示操作。")
    else:
        cloud.copy_secret_to_clipboard()
        print("✓ 共享密钥已复制到剪贴板（不会显示）。请粘贴到 Apps Script“项目设置 → 脚本属性”的 UCI_QUEUE_HMAC_SECRET。")
        print("  粘贴完成后建议随便复制点别的内容，覆盖剪贴板。")
    return 0


def _max_minutes(settings: Mapping[str, Any]) -> int:
    value = settings.get("content_max_duration_minutes")
    return int(value) if isinstance(value, (int, float)) and value > 0 else 15


def cmd_creators(args: argparse.Namespace) -> int:
    if args.action == "resolve":
        limit = 15
        try:
            limit = _max_minutes(cloud.inspect().get("settings") or {})
        except Exception:
            pass
        found, failed = [], []
        for query in args.queries:
            try:
                candidate = creator_tools.resolve(query, max_minutes=limit)
            except (ValueError, json.JSONDecodeError) as error:
                failed.append(str(error))
                continue
            found.append(candidate)
            share = "-" if candidate.share_within_limit is None else f"{candidate.share_within_limit:.0%}"
            print(f"• {candidate.creator_name}（{candidate.handle or candidate.channel_id}）"
                  f"  订阅 {candidate.subscribers or '-'}｜近 {candidate.recent_count} 条中位时长 {candidate.median_minutes or '-'} 分钟"
                  f"｜≤{limit} 分钟占 {share}｜每周约 {candidate.uploads_per_week or '-'} 条｜{creator_tools.advice(candidate, max_minutes=limit)}")
        for message in failed:
            print("✗ " + message)
        pending = {item["channel_id"]: item for item in store.read_state().get("creators_pending") or []}
        pending.update({candidate.channel_id: candidate.to_dict() for candidate in found})
        store.update_state(creators_pending=list(pending.values()))
        print(f"\n待确认 {len(pending)} 个频道。确认无误后运行：bin/uci setup creators apply"
              "（不想要的先用 bin/uci setup creators drop <频道ID或@handle> 去掉）")
        return 1 if failed else 0
    if args.action == "drop":
        targets = set(args.queries)
        kept = [item for item in store.read_state().get("creators_pending") or []
                if item.get("channel_id") not in targets and item.get("handle") not in targets]
        store.update_state(creators_pending=kept)
        print(f"待确认还剩 {len(kept)} 个频道。")
        return 0
    if args.action == "apply":
        pending = store.read_state().get("creators_pending") or []
        if not pending:
            print("没有待确认的频道。先运行 bin/uci setup creators resolve …")
            return 1
        rows = [{"creator_name": item["creator_name"], "channel_id": item["channel_id"], "enabled": True,
                 "notes": item.get("handle") or ""} for item in pending]
        result = cloud.creators_upsert(rows)
        store.update_state(creators_pending=[])
        print(f"✓ 新增 {len(result.get('added') or [])} 个，更新 {len(result.get('updated') or [])} 个。"
              "云端会在接下来的 10–20 分钟里为新频道建立播放量基线。")
        return 0
    if args.action == "import":
        if len(args.queries) != 1:
            raise cloud.SetupError("用法：bin/uci setup creators import 名单.tsv（列：creator_name, channel_id, enabled, trusted_creator, notes）")
        rows = read_creator_table(Path(args.queries[0]).expanduser())
        result = cloud.creators_upsert(rows)
        print(f"✓ 导入 {len(rows)} 个频道（新增 {len(result.get('added') or [])}，更新 {len(result.get('updated') or [])}，"
              f"其中启用 {sum(1 for row in rows if row['enabled'])}）。")
        return 0
    if args.action in {"enable", "disable"}:
        current = {item.get("channel_id"): item for item in cloud.inspect().get("creators") or []}
        rows = []
        for query in args.queries:
            match = current.get(query) or next((item for item in current.values() if item.get("notes") == query), None)
            if not match:
                print(f"✗ 云端没有 {query}")
                return 1
            rows.append({"creator_name": match["creator_name"], "channel_id": match["channel_id"], "enabled": args.action == "enable"})
        cloud.creators_upsert(rows)
        print(f"✓ 已{'启用' if args.action == 'enable' else '停用'} {len(rows)} 个频道")
        return 0
    for item in cloud.inspect().get("creators") or []:
        mark = "✓" if item.get("enabled") is True else "–"
        print(f" {mark} {item.get('creator_name')}  {item.get('channel_id')}  {item.get('notes') or ''}"
              f"  基线：{item.get('cold_baseline_status') or '待建立'}")
    return 0


def read_creator_table(path: Path) -> list[dict[str, Any]]:
    """Read a tab-separated creator list (header row required), e.g. one exported from another installation."""

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise cloud.SetupError(f"{path} 是空的")
    headers = [name.strip() for name in lines[0].split("\t")]
    if "channel_id" not in headers or "creator_name" not in headers:
        raise cloud.SetupError("名单第一行必须是表头，至少包含 creator_name 和 channel_id（用 Tab 分隔）")
    truthy = {"true", "yes", "1", "是"}
    rows = []
    for line in lines[1:]:
        values = dict(zip(headers, (cell.strip() for cell in line.split("\t"))))
        if not creator_tools.CHANNEL_ID.match(values.get("channel_id", "")):
            raise cloud.SetupError(f"频道 ID 不对：{values.get('channel_id')!r}（{values.get('creator_name')}）")
        rows.append({
            "creator_name": values["creator_name"],
            "channel_id": values["channel_id"],
            "enabled": values.get("enabled", "true").lower() in truthy,
            "trusted_creator": values.get("trusted_creator", "false").lower() in truthy,
            "notes": values.get("notes", ""),
        })
    return rows


def parse_value(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text.strip()


def apply_settings(pairs: Mapping[str, Any]) -> list[str]:
    cloud_values = {key: value for key, value in pairs.items() if key not in LOCAL_KEYS}
    changed = []
    if cloud_values:
        try:
            cloud.config_set(cloud_values)
        except QueueApiError as error:
            raise cloud.SetupError(f"云端拒绝了这次修改：{(error.detail or error.message).removeprefix('server:')}") from None
        changed += sorted(cloud_values)
    local: dict[str, Any] = {key: str(pairs[key]) for key in MIRRORED_KEYS if key in pairs}
    for key, (section, name) in LOCAL_KEYS.items():
        if key in pairs:
            local.setdefault(section, {})[name] = pairs[key]
            changed.append(key)
    if local:
        store.update_user_config(local)
    return changed


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "set":
        pairs = {}
        for item in args.pairs:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise cloud.SetupError(f"格式应为 key=value：{item}")
            pairs[key.strip()] = parse_value(value)
        print("✓ 已更新：" + "、".join(apply_settings(pairs)))
        return 0
    if args.action == "brief":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or "")
        apply_settings({"semantic_editorial_brief": text})
        print(f"✓ 内容方向已写入云端（{len(text.strip())} 字）")
        return 0
    settings = cloud.inspect().get("settings") or {}
    local = store.read_user_config()
    print("【云端设置】")
    for key, value in settings.items():
        shown = value
        if key == "semantic_editorial_brief":
            text = str(value or "")
            shown = (text[:80] + "…") if len(text) > 80 else (text or "（空：使用内置的通用示例）")
        print(f"  {key} = {shown}")
    print(f"【本机设置】{store.config_path()}")
    defaults = load_config(PROJECT_DEFAULTS_PATH)
    for key, (section, name) in LOCAL_KEYS.items():
        value = (defaults.get(section) or {}).get(name)
        print(f"  {key} = {value}" + ("" if name not in (local.get(section) or {}) else "（已自定义）"))
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    store.confirm(args.step)
    print(f"✓ 已记录：{args.step}")
    return 0


def cmd_skip(args: argparse.Namespace) -> int:
    store.confirm(f"skip-{args.step}")
    print(f"✓ 已跳过：{args.step}")
    if args.step == "gemini":
        print("  之后选片只靠时长、标题规则和热度，内容方向不起作用；发布标题和文案改用规则生成。")
        print(f"  随时可以补上：按 bin/uci setup 第 9 步的说明申请免费 Key（{GEMINI_KEY_URL}）。")
    return 0


def cmd_launchagent(args: argparse.Namespace) -> int:
    if args.action == "install":
        for path in launchagent.install():
            print(f"✓ 已安装并加载 {path}")
    elif args.action == "uninstall":
        launchagent.uninstall()
        print("✓ 已卸载后台程序")
    else:
        for label in (WORKER_LABEL, launchagent.UPDATE_LABEL):
            print(f" {'✓' if launchagent.is_loaded(label) else '✗'} {label}")
    return 0


def quota_estimate(settings: Mapping[str, Any], creators: int) -> int:
    def minutes(value: Any) -> int:
        hours, _, mins = str(value or "00:00").partition(":")
        return int(hours) * 60 + int(mins or 0)

    window = max(0, minutes(settings.get("discovery_window_end")) - minutes(settings.get("discovery_window_start")))
    cadence = settings.get("discovery_cadence_minutes") if isinstance(settings.get("discovery_cadence_minutes"), int) else 10
    polls = window // max(1, cadence) + 1
    # One playlistItems call per creator per poll, plus videos.list snapshots and the T+30/60/120 checkpoints.
    return creators * polls + polls * 2 + creators * 3


def verify_downloads(ctx: Context, args: argparse.Namespace) -> int:
    from src.get_cli import deliver, download, downloads_root
    from src.core.job import ContentType

    folder = downloads_root() / "uci-setup 试跑"
    print(f"试下载一个 PDF：{TEST_PDF_URL}")
    files = deliver(download(TEST_PDF_URL, ContentType.DOCUMENT, log=lambda line: print(line)), folder)
    print(f"✓ 文档下载正常：{files[0]}")
    if ctx.prefs.needs_subtitle_tools and not args.no_smoke:
        print(f"试一次视频下载 + 翻译压制：{args.smoke_url}")
        from src.setup.smoke import run_smoke

        outcome = run_smoke(args.smoke_url, downloads_root(), log=lambda line: print(line))
        print(f"✓ 视频下载和翻译压制正常：{outcome.destination}")
    store.update_state(download_verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(f"\n下载功能可用。试跑的文件在 {folder}，看过后可以删掉。")
    print("用法：bin/uci <链接>，也可以带一句说明，例如 bin/uci \"下载这个链接里的PDF https://…\"")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ctx = gather(deep=True)
    problems = [step for step in build_steps(ctx) if not step.done and not step.optional and step.key not in {"verify", "verify-download"}]
    if problems:
        print("还有步骤没完成，先把它们做完：")
        for step in problems:
            print(f"  ✗ {step.title}")
        return 1
    if not ctx.prefs.monitoring:
        return verify_downloads(ctx, args)
    info = ctx.inspect or {}
    settings = info.get("settings") or {}
    print("✓ 本机能连上云端，签名请求通过")
    print(f"✓ 表格结构完整，定时触发器 {info.get('scheduler_triggers')} 个")
    status = cloud.client().status()
    print(f"✓ 云端状态接口正常（批次 {status.get('day') or '-'}）")
    enabled = [item for item in info.get("creators") or [] if item.get("enabled") is True]
    waiting = [item.get("creator_name") for item in enabled if not item.get("cold_baseline_status")]
    print(f"✓ 白名单 {len(enabled)} 个频道" + (f"；{len(waiting)} 个还在建立基线（云端每 10 分钟处理一批，属正常）" if waiting else ""))
    if not args.no_smoke:
        delivery = Path(str((ctx.config.get("output") or {}).get("delivery_root") or "~/Movies/Universal Content Intake/")).expanduser()
        print(f"端到端试跑：{args.smoke_url}")
        from src.setup.smoke import run_smoke

        outcome = run_smoke(args.smoke_url, delivery)
        print(f"✓ 试跑成功，成片和发布信息已交付到：{outcome.destination}（看过后可以删掉这个试跑文件夹）")
    if settings.get("daily_selection_enabled") is not True:
        cloud.config_set({"daily_selection_enabled": True})
        print("✓ 已开启每日选片")
    store.update_state(verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print("\n系统可用。")
    print(f"  时区 {settings.get('production_timezone')}：每天 {settings.get('discovery_window_start')}–{settings.get('discovery_window_end')} 发现新视频，"
          f"{settings.get('final_sweep_time')} 补扫，{settings.get('daily_selection_time')} 选片（最多 {settings.get('daily_selection_max')} 条），"
          f"第 2 条须在 {settings.get('rank2_start_cutoff')} 前开始处理。")
    print(f"  YouTube 配额估算：约 {quota_estimate(settings, len(enabled))} 单位/天（免费上限 10,000）。")
    print("  日常查看：bin/uci status")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uci setup", description="Universal Content Intake 首次配置向导")
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status", help="显示进度和下一步（默认）")
    status.add_argument("--json", action="store_true", help="机器可读输出，给 Agent 用")
    status.add_argument("--offline", action="store_true", help="不连云端")
    sub.add_parser("doctor", help="检查本机环境（含模型 SHA-256 和翻译语言包）")
    build = sub.add_parser("build-tools", help="下载模型并编译 whisper.cpp 与 Swift 小工具")
    build.add_argument("--skip-models", action="store_true")
    build.add_argument("--skip-whisper", action="store_true")
    build.add_argument("--skip-swift", action="store_true")
    cloud_parser = sub.add_parser("cloud", help="云端：create | push | deploy | open")
    cloud_parser.add_argument("action", choices=("create", "push", "deploy", "open"))
    cloud_parser.add_argument("--title", default=cloud.DEFAULT_TITLE)
    cloud_parser.add_argument("--no-browser", action="store_true", help="open：只显示链接，不打开浏览器")
    secret = sub.add_parser("secret", help="共享密钥：create | copy")
    secret.add_argument("action", choices=("create", "copy"))
    secret.add_argument("--rotate", action="store_true")
    creators = sub.add_parser("creators", help="白名单：resolve | drop | apply | import | list | enable | disable")
    creators.add_argument("action", choices=("resolve", "drop", "apply", "import", "list", "enable", "disable"))
    creators.add_argument("queries", nargs="*", help="@handle、频道链接或频道 ID")
    config = sub.add_parser("config", help="设置：show | set key=value … | brief --file")
    config.add_argument("action", choices=("show", "set", "brief"))
    config.add_argument("pairs", nargs="*")
    config.add_argument("--file")
    config.add_argument("--text")
    confirm = sub.add_parser("confirm", help="记录一个必须本人完成的步骤已完成")
    confirm.add_argument("step", choices=CONFIRMABLE)
    skip = sub.add_parser("skip", help="跳过可选步骤")
    skip.add_argument("step", choices=("gemini",))
    agent = sub.add_parser("launchagent", help="后台程序：install | uninstall | status")
    agent.add_argument("action", choices=("install", "uninstall", "status"))
    verify = sub.add_parser("verify", help="验收并开启每日选片")
    verify.add_argument("--no-smoke", action="store_true", help="跳过端到端试跑")
    from src.setup.smoke import DEFAULT_SMOKE_URL

    verify.add_argument("--smoke-url", default=DEFAULT_SMOKE_URL)
    return parser


COMMANDS = {
    "status": cmd_status, "doctor": cmd_doctor, "build-tools": cmd_build_tools, "cloud": cmd_cloud,
    "secret": cmd_secret, "creators": cmd_creators, "config": cmd_config, "confirm": cmd_confirm,
    "skip": cmd_skip, "launchagent": cmd_launchagent, "verify": cmd_verify,
}


def main(argv: list[str] | None = None) -> int:
    # Keep progress lines and errors in order when stdout is a pipe.
    sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["status", *(argv or [])])
    try:
        return COMMANDS[args.command](args)
    except (cloud.SetupError, QueueApiError, RuntimeError, OSError) as error:
        message = str(error)
        if isinstance(error, QueueApiError):
            message = _inspect_error(error)
        print(f"✗ {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
