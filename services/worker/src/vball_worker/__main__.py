"""Run the worker as a long-lived poller: ``python -m vball_worker``."""

from __future__ import annotations

from vball_core.settings import get_settings
from vball_worker.runner import main_loop


def main() -> None:
    main_loop(get_settings())


if __name__ == "__main__":
    main()
