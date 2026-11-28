"""Run the worker: ``python -m vball_worker`` polls forever (local Docker, any VM);
``python -m vball_worker --until-idle`` processes every runnable job, then exits (GitHub
Actions and other on-demand runners)."""

from __future__ import annotations

import sys

from vball_core.logging import configure_logging, get_logger
from vball_core.settings import get_settings
from vball_worker.runner import Worker, main_loop


def main() -> None:
    settings = get_settings()
    if "--until-idle" in sys.argv[1:]:
        configure_logging(settings.log_level, settings.log_json)
        done = Worker(settings).run_until_idle()
        get_logger("vball_worker").info("worker_idle", jobs_processed=done)
        return
    main_loop(settings)


if __name__ == "__main__":
    main()
