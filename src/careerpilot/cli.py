"""Composition root and local health command."""

import argparse

from careerpilot.application.health import check_health
from careerpilot.config import Settings
from careerpilot.observability import configure_logging, log_event


def main() -> None:
    parser = argparse.ArgumentParser(description="CareerPilot AI local diagnostics")
    parser.add_argument("command", choices=["health"])
    parser.parse_args()
    settings = Settings()
    logger = configure_logging(settings.log_level)
    log_event(logger, "application_started")
    status = check_health()
    log_event(logger, "health_checked")
    print(status.model_dump_json())
