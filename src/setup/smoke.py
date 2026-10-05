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

# "Me at the zoo": 19 seconds of English speech, the first YouTube upload.
DEFAULT_SMOKE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
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
        if result.returncode not in (0,):
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
