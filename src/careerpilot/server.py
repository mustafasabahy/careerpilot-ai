"""Explicit single-host startup, migration and credential administration."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from pydantic import ValidationError

from careerpilot.api.app import create_app
from careerpilot.config import configured_execution
from careerpilot.deployment import ServerSettings
from careerpilot.portal import Portal
from careerpilot.telemetry.langsmith import LangSmithObserver, TraceSettings


def write_access(portal: Portal, owner: UUID, path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump({"owner_id": str(owner), "token": portal.security.issue(owner)}, target)
    print(f"Access token written to {path}; keep it private.")


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(
        description="CareerPilot single-host server and storage administration"
    )
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--init", action="store_true", help="Initialize development storage and issue a demo token"
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Explicitly initialize/migrate storage without issuing credentials",
    )
    parser.add_argument(
        "--issue-token",
        type=UUID,
        help="Issue an expiring token for a trusted operator-selected owner",
    )
    parser.add_argument("--revoke-owner", type=UUID, help="Revoke all credentials for an owner")
    args = parser.parse_args()
    try:
        overrides = {
            key: value
            for key, value in {"directory": args.directory, "port": args.port}.items()
            if value is not None
        }
        settings = ServerSettings(**overrides)
        if args.init and settings.environment != "development":
            parser.error(
                "--init demo credentials are development-only; use --migrate and --issue-token"
            )
        if args.init or args.migrate:
            Portal.initialize(settings.directory)
        settings.validate_storage()
        portal = Portal(settings.directory)
        if args.init:
            write_access(portal, uuid4(), settings.directory / "dev-access.json")
            return
        if args.migrate:
            print("Storage initialized/migrated; no credentials issued.")
            return
        if args.revoke_owner:
            portal.security.revoke_tokens(args.revoke_owner)
            print("Owner credentials revoked.")
            return
        if args.issue_token:
            write_access(portal, args.issue_token, settings.directory / f"access-{uuid4()}.json")
            return
        hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
        if settings.public_origin and settings.public_origin.host:
            hosts += (settings.public_origin.host,)
        observer = LangSmithObserver(TraceSettings())
        uvicorn.run(
            create_app(
                portal,
                enable_demo=settings.environment == "development",
                allowed_hosts=hosts,
                observer=observer,
            ),
            host="127.0.0.1",
            port=settings.port,
            access_log=False,
            workers=1,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            timeout_keep_alive=5,
            limit_concurrency=32,
        )
    except (ValidationError, ValueError, FileNotFoundError, FileExistsError):
        parser.exit(
            2,
            "Startup configuration/storage invalid; "
            "check private directory, migration and HTTPS origin.\n",
        )
