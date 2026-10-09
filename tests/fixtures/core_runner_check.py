"""Run the monitoring Worker's Core runner for real on one local video (used by CI on Windows).

  python tests/fixtures/core_runner_check.py URL WORKSPACE

1. One full run: download, subtitles, translation, output. It must succeed.
2. A second Job is started and cancelled while its Core runs. No Core process
   may be left behind afterwards.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core import compat  # noqa: E402
from src.queue.worker import SubprocessCoreRunner, WorkerState  # noqa: E402


def state(job_id: str, url: str) -> WorkerState:
    return WorkerState(worker_state="PROCESSING", claim_request_id="request-000000000001", queue_id="queue-ci",
                       claim_token="ci-claim-token", lease_until="2099-01-01T00:00:00Z", local_job_id=job_id, url=url)


def main(url: str, workspace: str) -> int:
    runner = SubprocessCoreRunner(project_root=Path(__file__).resolve().parents[2], workspace_root=workspace,
                                  python_executable=sys.executable)
    started: list[int] = []
    result = runner.run(state("ci-full-run", url), cancel_event=threading.Event(), on_started=started.append)
    print(f"full run: success={result.success} error={result.error.to_dict() if result.error else None} cores={started}")
    if not result.success:
        return 1

    cancel = threading.Event()
    pids: list[int] = []
    outcome: list[object] = []
    thread = threading.Thread(target=lambda: outcome.append(
        runner.run(state("ci-cancelled-run", url), cancel_event=cancel, on_started=pids.append)))
    thread.start()
    while not pids and thread.is_alive():
        time.sleep(0.1)
    time.sleep(1)
    cancel.set()
    thread.join(timeout=60)
    leftover = [pid for pid, command in compat.command_lines() if "ci-cancelled-run" in command]
    print(f"cancelled run: cancelled={getattr(outcome[0], 'cancelled', None) if outcome else None} "
          f"cores={pids} leftover={leftover}")
    return 0 if outcome and getattr(outcome[0], "cancelled", False) and not leftover else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
