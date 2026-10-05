"""Explicit local Worker entrypoint used by a rendered LaunchAgent."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from src.queue.client import QueueApiError, QueueClient
from src.queue.worker import QueueWorker, SubprocessCoreRunner, WorkerConfig, WorkerConfigError, WorkerStateStore, macos_notifier


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uci-queue-worker", description="Run the local Universal Content Intake Queue Worker")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("uci.queue.worker")
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)

    try:
        config = WorkerConfig.from_defaults(args.config)
        client = QueueClient.from_defaults(args.config)
    except WorkerConfigError:
        logger.error("queue_worker operation=start error_code=CONFIG_INVALID")
        return 2
    except QueueApiError as error:
        logger.error("queue_worker operation=start error_code=%s", error.code)
        return 2

    runner = SubprocessCoreRunner(
        project_root=PROJECT_ROOT,
        workspace_root=config.workspace_root,
        python_executable=Path(sys.executable),
    )
    worker = QueueWorker(
        client=client,
        core_runner=runner,
        config=config,
        state_store=WorkerStateStore(config.state_dir),
        logger=logger,
        notifier=macos_notifier,
    )
    worker.run_forever(stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

