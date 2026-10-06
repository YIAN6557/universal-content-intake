"""Chinese health report for one Daily Batch: cloud status API plus local Worker.

Read-only. Uses the signed Queue API ``status`` action, so it never needs a
browser Google session. Exit code: 0 healthy, 1 problems found, 2 cloud
unreachable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.output.delivery import delivered_artifacts
from src.queue.client import QueueApiError, QueueClient
from src.queue.worker import WorkerConfig, WorkerConfigError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"
DEFAULT_WORKER_LOG = Path.home() / "Library" / "Logs" / "Universal Content Intake" / "worker.stderr.log"
WORKER_LABEL = "local.universal-content-intake.worker"

# Fallbacks for the Cloud schedule and Cold thresholds; the status API returns
# the live Config values, which take precedence. Slack covers the 10-minute
# trigger cadence.
DEFAULT_SCHEDULE = {"discovery_window_end": "08:00", "final_sweep_time": "08:10", "daily_selection_time": "10:00"}
SCHEDULE_SLACK_MINUTES = 20
CHECKPOINTS = (30, 60, 120)
DEFAULT_COLD_RATIOS = {30: 0.015, 60: 0.025, 120: 0.04}


def _clock_minutes(value: Any, fallback: str) -> tuple[int, str]:
    text = str(value or "").strip().lstrip("'")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        text = fallback
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    return int(match.group(1)) * 60 + int(match.group(2)), text


def _cold_ratios(config: Mapping[str, Any]) -> dict[int, float]:
    ratios = {}
    for checkpoint, fallback in DEFAULT_COLD_RATIOS.items():
        try:
            ratios[checkpoint] = float(config.get(f"cold_start_checkpoint_{checkpoint}_ratio"))
        except (TypeError, ValueError):
            ratios[checkpoint] = fallback
    return ratios
SNAPSHOT_GRACE_MINUTES = 30
WORKER_SILENCE_MINUTES = 15
RETRY_STREAK_PROBLEM = 3
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2},\d+ \w+ .*operation=(\w+) state=(\w+).*?error_code=(\S+)(?: detail=(.*))?$")

STATE_LABELS = {
    "WATCH": "观察中", "NORMAL": "普通", "DATA_HOT": "热门", "CANDIDATE": "候选", "LIVE_REJECTED": "直播已放弃", "CONTENT_REJECTED": "内容不符已过滤",
    "PREMIERE_PENDING": "首映待开播", "PREMIERE_OUT_OF_WINDOW": "首映超出窗口", "SNAPSHOT_MISSED": "快照错过",
    "BASELINE_UNAVAILABLE": "无基线",
}


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def add(self, text: str = "") -> None:
        self.lines.append(text)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _clock(value: Any, tz: ZoneInfo) -> str:
    parsed = _parse_time(value)
    return parsed.astimezone(tz).strftime("%H:%M") if parsed else "-"


def _installed_at(value: Any, tz: ZoneInfo) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(tz) if moment.tzinfo else None


def analyze_cloud(status: Mapping[str, Any], now: datetime, report: Report) -> None:
    config = status.get("config") or {}
    tz = ZoneInfo(str(config.get("production_timezone") or "Asia/Shanghai"))
    local_now = now.astimezone(tz)
    day = str(status.get("day") or local_now.strftime("%Y-%m-%d"))
    is_today = day == local_now.strftime("%Y-%m-%d")
    minute_of_day = local_now.hour * 60 + local_now.minute if is_today else 24 * 60

    report.add("【云端】")
    checks = status.get("checks") or []
    report.add("  自检：" + "  ".join(f"{c.get('name')} {'✓' if c.get('ok') else '✗'}" for c in checks))
    for check in checks:
        if not check.get("ok"):
            report.problems.append(f"云端读取 {check.get('name')} 失败：{check.get('error')}")
    slot = str(config.get("last_discovery_slot") or "")
    sweep = str(config.get("last_final_sweep_day") or "")
    selection = str(config.get("last_daily_selection_day") or "")
    report.add(f"  最近发现时段：{slot or '-'}；补扫日：{sweep or '-'}；选片日：{selection or '-'}")
    monitor_status = str(config.get("monitor_status") or "")
    if monitor_status and monitor_status != "ACTIVE":
        report.add(f"  ⚠ 监控已暂停：{monitor_status}（{config.get('last_api_error_class') or '未知原因'}，"
                   f"{config.get('last_api_error_at') or '时间未知'}）")
        report.problems.append(
            f"云端监控已暂停（monitor_status={monitor_status}，原因 {config.get('last_api_error_class') or '未知'}），"
            "发现、快照和选片都不会运行；排查后需在 Config 表把 monitor_status 改回 ACTIVE"
        )
    if str(config.get("daily_selection_enabled")).lower() not in {"true", "1"}:
        report.problems.append(f"daily_selection_enabled 不是 true（当前 {config.get('daily_selection_enabled')!r}）")
    window_end, _ = _clock_minutes(config.get("discovery_window_end"), DEFAULT_SCHEDULE["discovery_window_end"])
    sweep_at, sweep_label = _clock_minutes(config.get("final_sweep_time"), DEFAULT_SCHEDULE["final_sweep_time"])
    selection_at, selection_label = _clock_minutes(config.get("daily_selection_time"), DEFAULT_SCHEDULE["daily_selection_time"])
    if is_today and 20 <= minute_of_day < window_end + 20:
        try:
            last_slot = datetime.strptime(slot, "%Y-%m-%d %H:%M").replace(tzinfo=tz) if slot else None
        except ValueError:
            last_slot = None
        if last_slot is None or local_now - last_slot > timedelta(minutes=30):
            report.problems.append(f"发现时段未按时推进（最近：{slot or '无'}）")
    # A run scheduled before the system was installed was never due.
    started = _installed_at(config.get("monitor_started_at"), tz)

    def due(at_minute: int) -> bool:
        if started is None:
            return True
        start_day = started.strftime("%Y-%m-%d")
        return start_day < day or (start_day == day and started.hour * 60 + started.minute <= at_minute)

    if started is not None and started.strftime("%Y-%m-%d") == day:
        report.add(f"  系统在 {started.strftime('%H:%M')} 安装；今天在这之前的环节不会补跑")
    # The markers keep only the latest day, so a later marker means this day ran.
    if minute_of_day >= sweep_at + SCHEDULE_SLACK_MINUTES and sweep < day and due(sweep_at):
        report.problems.append(f"{day} 的 {sweep_label} 补扫没有执行（补扫日：{sweep or '无'}）")
    if minute_of_day >= selection_at + SCHEDULE_SLACK_MINUTES and selection < day and due(selection_at):
        report.problems.append(f"{day} 的 {selection_label} 选片没有执行（选片日：{selection or '无'}）")

    videos = status.get("videos") or []
    counts: dict[str, int] = {}
    for video in videos:
        state = str(video.get("lifecycle_state") or "")
        counts[state] = counts.get(state, 0) + 1
    summary = "，".join(f"{STATE_LABELS.get(state, state)} {count}" for state, count in sorted(counts.items()))
    report.add()
    report.add(f"【{day} 批次视频】共 {len(videos)} 条" + (f"（{summary}）" if summary else ""))
    for video in videos:
        state = str(video.get("lifecycle_state") or "")
        title = str(video.get("title") or "")[:48]
        head = f"  - {video.get('creator_name')}｜{title}｜发布 {_clock(video.get('published_at'), tz)}｜{STATE_LABELS.get(state, state)}"
        if video.get("selection_result"):
            head += f"｜选片：{video.get('selection_result')} {video.get('selection_reason') or ''}".rstrip()
        report.add(head)
        parts = []
        cold_ratios = _cold_ratios(status.get("config") or {})
        for snap in sorted(video.get("snapshots") or [], key=lambda s: _number(s.get("snapshot_stage_minutes")) or 0):
            stage = int(_number(snap.get("snapshot_stage_minutes")) or 0)
            views = _number(snap.get("view_count"))
            median = _number(snap.get("baseline_final_views_median"))
            need = f" / 需 {median * cold_ratios[stage]:,.0f}" if median and stage in cold_ratios else ""
            parts.append(f"T+{stage} {views:,.0f}{need}" if views is not None else f"T+{stage} -")
        if parts:
            report.add("      " + " · ".join(parts))
        published = _parse_time(video.get("published_at"))
        if state == "WATCH" and published and now > published + timedelta(minutes=CHECKPOINTS[-1] + SNAPSHOT_GRACE_MINUTES):
            report.problems.append(f"「{title}」发布已超过 {CHECKPOINTS[-1] + SNAPSHOT_GRACE_MINUTES} 分钟仍在观察中，快照可能没有按时采集")

    queue = status.get("queue") or []
    report.add()
    report.add(f"【队列】本批次/活动任务 {len(queue)} 条（队列共 {status.get('queue_total', '-')} 条）")
    for task in queue:
        report.add(f"  - {task.get('video_id')}｜{task.get('status')}｜Rank {task.get('selection_rank') or '-'}｜尝试 {task.get('attempts')}"
                   + (f"｜错误 {task.get('last_error_code')}" if task.get("last_error_code") else ""))
        if task.get("status") in {"PAUSED", "FAILED"}:
            report.problems.append(f"队列任务 {task.get('video_id')} 状态为 {task.get('status')}（{task.get('last_error_code') or '无错误码'}）")


def analyze_worker(log_path: Path, now: datetime, day: str, report: Report) -> None:
    report.add()
    report.add("【本机 Worker】")
    try:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ""
    row = next((line.split("\t") for line in listing.splitlines() if line.endswith(WORKER_LABEL)), None)
    running = bool(row and row[0].strip().isdigit())
    report.add(f"  进程：{'运行中' if running else '未运行'}")
    if not running:
        report.problems.append("Worker LaunchAgent 没有在运行")

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        report.problems.append(f"读不到 Worker 日志 {log_path}")
        return
    states: dict[str, int] = {}
    errors: dict[str, int] = {}
    streak = 0
    last_details: list[str] = []
    last_at: datetime | None = None
    for line in lines:
        match = LOG_LINE.match(line)
        if not match:
            continue
        stamp = datetime.strptime(f"{match.group(1)} {match.group(2)}:{match.group(3)}", "%Y-%m-%d %H:%M").astimezone()
        last_at = stamp
        state, code, detail = match.group(5), match.group(6), match.group(7)
        streak = streak + 1 if state == "retry" else 0
        if state == "retry" and detail:
            last_details = (last_details + [detail])[-1:]
        if match.group(1) != day:
            continue
        states[state] = states.get(state, 0) + 1
        if code not in {"-", "None"}:
            errors[code] = errors.get(code, 0) + 1
    report.add(f"  {day} 领取/处理记录：" + ("，".join(f"{k} {v}" for k, v in sorted(states.items())) or "无"))
    if errors:
        report.add("  错误码：" + "，".join(f"{k}×{v}" for k, v in sorted(errors.items())))
    if last_at:
        report.add(f"  最近一条日志：{last_at.strftime('%m-%d %H:%M')}")
        if running and now - last_at > timedelta(minutes=WORKER_SILENCE_MINUTES):
            report.problems.append(f"Worker 日志已 {int((now - last_at).total_seconds() // 60)} 分钟没有更新")
    if streak >= RETRY_STREAK_PROBLEM:
        report.problems.append(f"Worker 当前已连续 {streak} 次重试失败" + (f"：{last_details[-1]}" if last_details else ""))


def analyze_jobs(workspace_root: Path, day: str, tz: ZoneInfo, report: Report) -> None:
    report.add()
    report.add("【成片】")
    found = 0
    for job_dir in sorted(workspace_root.glob("job-*"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = job_dir.stat()
        created = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_ctime), tz)
        if created.strftime("%Y-%m-%d") < day:
            continue
        found += 1
        try:
            job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job = {}
        final = job_dir / "output" / "final.mp4"
        delivered = delivered_artifacts(job_dir).get("final.mp4")
        if final.exists():
            size = f"{final.stat().st_size / 1e6:,.0f} MB"
        elif delivered:
            size = f"已移到 {delivered.get('destination')}（{int(delivered.get('size', 0)) / 1e6:,.0f} MB）"
        else:
            size = "无 final.mp4"
        title = ""
        info = job_dir / "output" / "info.md"
        delivered_info = delivered_artifacts(job_dir).get("info.md")
        if not info.exists() and delivered_info:
            info = Path(str(delivered_info.get("destination")))
        if info.exists():
            match = re.search(r"^- Publish Title: (.*)$", info.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
            title = f"｜发布标题：{match.group(1).strip()}" if match and match.group(1).strip() else ""
        state = job.get("current_state", "?")
        report.add(f"  - {job_dir.name}｜{state}｜{size}{title}")
        if state not in {"COMPLETED"} and not final.exists() and not delivered:
            report.problems.append(f"{job_dir.name} 状态 {state}，还没有成片")
    if not found:
        report.add(f"  {day} 没有新的 Job")


def build_report(status: Mapping[str, Any] | None, cloud_error: QueueApiError | None, *, now: datetime,
                 day: str | None, log_path: Path, workspace_root: Path | None) -> Report:
    report = Report()
    config = (status or {}).get("config") or {}
    tz = ZoneInfo(str(config.get("production_timezone") or "Asia/Shanghai"))
    batch_day = str((status or {}).get("day") or day or now.astimezone(tz).strftime("%Y-%m-%d"))
    report.add(f"UCI 运行状态 · 批次 {batch_day} · 查询时间 {now.astimezone(tz).strftime('%m-%d %H:%M')}")
    report.add()
    if status is not None:
        analyze_cloud(status, now, report)
    else:
        report.add("【云端】无法访问")
        detail = f"（{cloud_error.detail}）" if cloud_error is not None and cloud_error.detail else ""
        report.problems.append(f"云端状态接口失败：{cloud_error.code if cloud_error else '未知'}{detail}")
    analyze_worker(log_path, now, batch_day, report)
    if workspace_root is not None:
        analyze_jobs(workspace_root, batch_day, tz, report)
    header = "结论：正常" if not report.problems else f"结论：有 {len(report.problems)} 个问题"
    report.lines.insert(1, header)
    if report.problems:
        report.add()
        report.add("【问题】")
        report.lines.extend(f"  - {problem}" for problem in report.problems)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uci status", description="Universal Content Intake 运行状态（只读）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--day", help="批次日期 YYYY-MM-DD，默认当天（生产时区）")
    parser.add_argument("--worker-log", type=Path, default=DEFAULT_WORKER_LOG)
    parser.add_argument("--json", action="store_true", help="输出云端原始状态 JSON")
    args = parser.parse_args(argv)

    status: Mapping[str, Any] | None = None
    cloud_error: QueueApiError | None = None
    try:
        status = QueueClient.from_defaults(args.config).status(args.day)
    except QueueApiError as error:
        cloud_error = error
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2) if status is not None else json.dumps({"error": cloud_error.code if cloud_error else None}))
        return 0 if status is not None else 2
    try:
        workspace_root: Path | None = WorkerConfig.from_defaults(args.config).workspace_root
    except WorkerConfigError:
        workspace_root = None
    report = build_report(status, cloud_error, now=datetime.now(timezone.utc), day=args.day,
                          log_path=args.worker_log, workspace_root=workspace_root)
    print("\n".join(report.lines))
    if status is None:
        return 2
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
