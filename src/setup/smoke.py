"""End-to-end local trial: download → subtitles/translation → burn-in → delivery, without the Queue."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.core.job import Job
from src.output.delivery import DeliveryOutcome, deliver_completed_videos
from src.output.paths import OutputWorkspace
from src.queue.worker import next_core_step
from src.setup.environment import PROJECT_ROOT

# A 67-second NASA video (a U.S. government work, free of copyright) with
# English speech and English subtitles. Avoid "Me at the zoo": YouTube labels it
# German, so the trial would need the German translation pack.
DEFAULT_SMOKE_URL = "https://www.youtube.com/watch?v=IwZVXmQdX1E"
SMOKE_FOLDER = "uci-setup 试跑"
MAX_STEPS = 6


def run_smoke(url: str, delivery_root: Path, *, log: Callable[[str], None] = print) -> DeliveryOutcome:
    workspace = Path(tempfile.mkdtemp(prefix="uci-smoke-"))
    job_id = "setup-smoke-" + datetime.now().strftime("%Y%m%d%H%M%S")
    job_file = OutputWorkspace.for_job(workspace, job_id).paths.job_dir / "job.json"
    command = [sys.executable, "-m", "src.cli", "video-run", "--url", url, "--workspace-root", str(workspace), "--job-id", job_id]
    for _ in range(MAX_STEPS):
        log(f"  → {command[3]}")
        result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3600, check=False)
        if not job_file.is_file():
            raise RuntimeError(f"{command[3]} 没有生成 Job（退出码 {result.returncode}）：{result.stderr.strip()[-500:]}")
        job = Job.from_json(job_file.read_text(encoding="utf-8"))
        if job.error is not None:
            raise RuntimeError(f"{command[3]} 失败：{job.error.code.value} {job.error.message}")
        if result.returncode == 3:
            stage3 = job.source_metadata.get("stage3") or {}
            language = (stage3.get("subtitle_discovery") or {}).get("primary_language_code") or "未知"
            raise RuntimeError(f"试跑在字幕阶段暂停：{stage3.get('status') or '未知原因'}（字幕语言 {language}）。"
                               "缺翻译语言包时，到 系统设置 → 通用 → 语言与地区 → 翻译语言 下载对应语言。")
        if result.returncode != 0:
            raise RuntimeError(f"{command[3]} 退出码 {result.returncode}：{result.stderr.strip()[-500:]}")
        step = next_core_step(job)
        if step is None:
            break
        command = [sys.executable, "-m", "src.cli", step, "--resume-job", str(job_file)]
    else:
        raise RuntimeError("试跑没有在预期步数内完成")
    outcomes = deliver_completed_videos(workspace, delivery_root / SMOKE_FOLDER)
    delivered = [outcome for outcome in outcomes if outcome.status in {"delivered", "already_delivered"}]
    if not delivered:
        reasons = "; ".join(f"{outcome.status}:{outcome.reason}" for outcome in outcomes) or "没有可交付的视频"
        raise RuntimeError(f"交付失败：{reasons}")
    return delivered[0]
