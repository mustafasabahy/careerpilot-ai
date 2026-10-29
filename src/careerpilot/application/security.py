"""Small explicit authorization, import and outbound policies for local services."""

from pathlib import PurePath
from typing import Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from careerpilot.domain.common import Model

ResourceKind = Literal[
    "candidate", "job", "assessment", "document", "version", "application", "preparation", "thread"
]


class SecurityDenied(PermissionError):
    pass


class RetentionConflict(ValueError):
    pass


class UserIdentity(Model):
    id: UUID


class Authorizer(Protocol):
    def require(self, owner: UUID, kind: ResourceKind, identifier: UUID) -> None: ...


def validate_json_import(filename: str, media_type: str, content: bytes) -> None:
    if (
        len(content) > 256 * 1024
        or not content
        or media_type != "application/json"
        or PurePath(filename).name != filename
        or "\\" in filename
        or not filename.lower().endswith(".json")
        or "\x00" in filename
    ):
        raise ValueError("Only bounded plain JSON imports are supported")
    content.decode("utf-8")


def validate_outbound(url: str, *, purpose: Literal["model", "telemetry"], enabled: bool) -> None:
    parsed = urlsplit(url)
    hosts = {
        "model": {"api.openai.com"},
        "telemetry": {"api.smith.langchain.com", "eu.api.smith.langchain.com"},
    }
    if (
        not enabled
        or parsed.scheme != "https"
        or parsed.hostname not in hosts[purpose]
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise SecurityDenied("Outbound destination is not permitted")
