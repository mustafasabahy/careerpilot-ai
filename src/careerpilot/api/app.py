"""Validated local HTTP transport; all ownership decisions precede resource use."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from careerpilot.application.action_contracts import ActionConflict, ActionDenied
from careerpilot.application.analysis import AnalysisRequest, AnalysisResult
from careerpilot.application.chat_agent_contracts import ChatModel
from careerpilot.application.discovery_contracts import JobRecord
from careerpilot.application.execution import RunControl, RunObserver, current_run, execution_scope
from careerpilot.application.failures import failure_report
from careerpilot.application.preparation_contracts import PreparationRequest
from careerpilot.application.security import RetentionConflict, SecurityDenied, UserIdentity
from careerpilot.application.storage import IntegrityViolation, NotFound
from careerpilot.domain.actions import ActionContent, ReviewDecision
from careerpilot.domain.applications import ApplicationDocument, DocumentVersion, JobApplication
from careerpilot.domain.common import Model
from careerpilot.domain.jobs import JobFitAssessment, JobPosting
from careerpilot.domain.memory import MemoryItem, MemoryWrite
from careerpilot.domain.profile import CandidateProfile
from careerpilot.persistence.privacy import purge_candidate, purge_document
from careerpilot.portal import Portal
from careerpilot.workflows.action_approval import ApprovalThread
from careerpilot.workflows.preparation import PreparationResult


class Envelope[T](Model):
    data: T


class Page[T](Model):
    items: tuple[T, ...]
    offset: int
    next_offset: int | None


class ProfileInput(Model):
    profile: CandidateProfile
    explicitly_confirmed: Literal[True]


class ApplicationInput(Model):
    candidate_id: UUID
    job_id: UUID
    document_version_id: UUID | None = None


class ProposalInput(Model):
    content: ActionContent
    previous_proposal_id: UUID | None = None


class MemoryInput(Model):
    item: MemoryWrite
    explicitly_confirmed: Literal[True]


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        content = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            content.extend(message.get("body", b""))
            if len(content) > 128 * 1024:
                await JSONResponse(
                    {"error": {"code": "payload_limit", "message": "Request too large"}},
                    status_code=413,
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        consumed = False

        async def buffered() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(content), "more_body": False}
            return await receive()

        await self.app(scope, buffered, send)


def create_app(
    portal: Portal,
    *,
    enable_demo: bool = False,
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver"),
    observer: RunObserver | None = None,
    chat_model: ChatModel | None = None,
) -> FastAPI:
    from careerpilot.application.conversations import (
        ChatConflict,
        Conversation,
        MessageInput,
        Operation,
        Transcript,
    )
    from careerpilot.chat_runtime import configure_chat

    chat, chat_mode = configure_chat(portal, chat_model)
    app = FastAPI(
        title="CareerPilot AI local API",
        version="0.1.0",
        debug=False,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def safe_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation = uuid4()
        request.state.correlation_id = correlation
        try:
            with execution_scope(RunControl(correlation_id=correlation, observer=observer)):
                response = await call_next(request)
        except Exception as error:
            report = failure_report(error)
            response = JSONResponse(
                {
                    "error": {
                        "code": report.code,
                        "message": report.message,
                        "correlation_id": str(correlation),
                    }
                },
                status_code={
                    "limit_exceeded": 429,
                    "rate_limited": 429,
                    "timeout": 504,
                    "storage_busy": 503,
                    "cancelled": 409,
                    "unauthorized": 403,
                }.get(report.code, 500),
            )
        response.headers["X-Correlation-ID"] = str(correlation)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Exception)
    async def safe_error(request: Request, error: Exception) -> JSONResponse:
        code = (
            404
            if isinstance(error, (SecurityDenied, ActionDenied, NotFound))
            else 409
            if isinstance(
                error, (ActionConflict, IntegrityViolation, RetentionConflict, ChatConflict)
            )
            else 422
            if isinstance(error, (RequestValidationError, ValidationError, ValueError))
            else 500
        )
        report = failure_report(error)
        return JSONResponse(
            {
                "error": {
                    "code": report.code,
                    "message": "Resource unavailable"
                    if code == 404
                    else "Request could not be completed",
                    "correlation_id": str(request.state.correlation_id),
                }
            },
            status_code=code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "unauthorized" if error.status_code == 401 else "not_found",
                    "message": "Authentication required"
                    if error.status_code == 401
                    else "Resource unavailable",
                    "correlation_id": str(request.state.correlation_id),
                }
            },
            status_code=error.status_code,
        )

    app.add_exception_handler(RequestValidationError, safe_error)
    app.add_exception_handler(ChatConflict, safe_error)
    for error_type in (
        SecurityDenied,
        ActionDenied,
        NotFound,
        ActionConflict,
        IntegrityViolation,
        RetentionConflict,
        ValueError,
    ):
        app.add_exception_handler(error_type, safe_error)

    def identity(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
        ] = None,
    ) -> UserIdentity:
        from fastapi import HTTPException

        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            return portal.security.authenticate(credentials.credentials)
        except SecurityDenied:
            raise HTTPException(status_code=401, detail="Authentication required") from None

    User = Annotated[UserIdentity, Depends(identity)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        try:
            with portal.database.transaction():
                pass
            with portal.security.connection() as connection:
                connection.execute("SELECT 1 FROM access_tokens LIMIT 1")
            with chat.store.transaction():
                pass
            return JSONResponse({"status": "ready"})
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/api/targets")
    def targets(user: User) -> object:
        return {"data": {"recruiter": portal.target, "destination": portal.destination}}

    @app.get("/api/me", response_model=Envelope[UserIdentity])
    def me(user: User) -> object:
        return {"data": user}

    @app.post("/api/seed", status_code=201)
    def seed(user: User) -> object:
        if not enable_demo:
            raise SecurityDenied("Synthetic seeding disabled")
        return {"data": portal.seed(user.id)}

    @app.get("/api/profiles", response_model=Envelope[Page[CandidateProfile]])
    def profiles(
        user: User, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)
    ) -> object:
        ids = portal.security.list(user.id, "candidate", offset=offset, limit=limit)
        return {
            "data": {
                "items": portal.get_many(user.id, "candidate", CandidateProfile, ids),
                "offset": offset,
                "next_offset": offset + limit if len(ids) == limit else None,
            }
        }

    @app.post("/api/profiles", response_model=Envelope[CandidateProfile], status_code=201)
    def profile(data: ProfileInput, user: User) -> object:
        return {"data": portal.profile(user.id, data.profile)}

    @app.get("/api/profiles/{identifier}", response_model=Envelope[CandidateProfile])
    def get_profile(identifier: UUID, user: User) -> object:
        return {"data": portal.get(user.id, "candidate", CandidateProfile, identifier)}

    @app.post(
        "/api/profiles/{identifier}/revisions",
        response_model=Envelope[CandidateProfile],
        status_code=201,
    )
    def revise_profile(identifier: UUID, data: ProfileInput, user: User) -> object:
        portal.security.require(user.id, "candidate", identifier)
        return {"data": portal.profile(user.id, data.profile)}

    @app.delete("/api/profiles/{identifier}")
    def delete_profile(identifier: UUID, user: User) -> object:
        purge_candidate(portal.database, portal.security, user.id, identifier)
        return {"data": {"status": "deleted"}}

    @app.get("/api/jobs", response_model=Envelope[Page[JobPosting]])
    def jobs(
        user: User,
        query: str = Query("", max_length=160),
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=50),
    ) -> object:
        ids = portal.security.list(user.id, "job", offset=offset, limit=limit)
        values = portal.get_many(user.id, "job", JobPosting, ids)
        return {
            "data": {
                "items": [
                    job
                    for job in values
                    if query.casefold() in f"{job.title} {job.employer}".casefold()
                ],
                "offset": offset,
                "next_offset": offset + limit if len(ids) == limit else None,
            }
        }

    @app.post("/api/jobs/import", response_model=Envelope[JobPosting], status_code=201)
    def import_job(data: JobRecord, user: User) -> object:
        return {"data": portal.import_job(user.id, data)}

    @app.get("/api/jobs/{identifier}", response_model=Envelope[JobPosting])
    def job(identifier: UUID, user: User) -> object:
        return {"data": portal.get(user.id, "job", JobPosting, identifier)}

    @app.post("/api/analysis", response_model=Envelope[AnalysisResult])
    def analysis(data: AnalysisRequest, user: User) -> object:
        return {"data": portal.analyze(user.id, data)}

    @app.get("/api/assessments/{identifier}", response_model=Envelope[JobFitAssessment])
    def assessment(identifier: UUID, user: User) -> object:
        return {"data": portal.get(user.id, "assessment", JobFitAssessment, identifier)}

    @app.post("/api/preparations", response_model=Envelope[PreparationResult], status_code=201)
    def preparation(data: PreparationRequest, user: User) -> object:
        return {"data": portal.prepare(user.id, data)}

    @app.get("/api/documents", response_model=Envelope[Page[ApplicationDocument]])
    def documents(
        user: User, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)
    ) -> object:
        ids = portal.security.list(user.id, "document", offset=offset, limit=limit)
        return {
            "data": {
                "items": portal.get_many(user.id, "document", ApplicationDocument, ids),
                "offset": offset,
                "next_offset": offset + limit if len(ids) == limit else None,
            }
        }

    @app.get("/api/documents/{identifier}/latest", response_model=Envelope[DocumentVersion | None])
    def document(identifier: UUID, user: User) -> object:
        portal.security.require(user.id, "document", identifier)
        with portal.database.transaction() as repository:
            result = repository.latest_document_version(identifier)
        if result:
            portal.security.require(user.id, "version", result.id)
        return {"data": result}

    @app.get("/api/versions/{identifier}", response_model=Envelope[DocumentVersion])
    def version(identifier: UUID, user: User) -> object:
        return {"data": portal.get(user.id, "version", DocumentVersion, identifier)}

    @app.delete("/api/documents/{identifier}")
    def delete_document(identifier: UUID, user: User) -> object:
        purge_document(portal.database, portal.security, user.id, identifier)
        return {"data": {"status": "deleted"}}

    @app.post("/api/applications", response_model=Envelope[JobApplication], status_code=201)
    def application(data: ApplicationInput, user: User) -> object:
        return {
            "data": portal.application(
                user.id, data.candidate_id, data.job_id, data.document_version_id
            )
        }

    @app.get("/api/applications", response_model=Envelope[Page[JobApplication]])
    def applications(
        user: User, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)
    ) -> object:
        ids = portal.security.list(user.id, "application", offset=offset, limit=limit)
        return {
            "data": {
                "items": portal.get_many(user.id, "application", JobApplication, ids),
                "offset": offset,
                "next_offset": offset + limit if len(ids) == limit else None,
            }
        }

    @app.post(
        "/api/applications/{identifier}/proposals",
        response_model=Envelope[ApprovalThread],
        status_code=202,
    )
    def propose(identifier: UUID, data: ProposalInput, user: User) -> object:
        return {
            "data": portal.propose(user.id, identifier, data.content, data.previous_proposal_id)
        }

    @app.get("/api/threads", response_model=Envelope[Page[ApprovalThread]])
    def threads(
        user: User, offset: int = Query(0, ge=0), limit: int = Query(5, ge=1, le=20)
    ) -> object:
        identifiers = portal.security.list(user.id, "thread", offset=offset, limit=limit)
        values = []
        for identifier in identifiers:
            try:
                with portal.approval(portal.thread_service(user.id, identifier)) as workflow:
                    values.append(workflow.current(identifier))
            except (ActionDenied, NotFound):
                continue
        return {
            "data": {
                "items": values,
                "offset": offset,
                "next_offset": offset + limit if len(identifiers) == limit else None,
            }
        }

    @app.get("/api/threads/{identifier}", response_model=Envelope[ApprovalThread])
    def thread(identifier: UUID, user: User) -> object:
        service = portal.thread_service(user.id, identifier)
        with portal.approval(service) as workflow:
            return {"data": workflow.current(identifier)}

    @app.get("/api/threads/{identifier}/history")
    def history(identifier: UUID, user: User) -> object:
        return {"data": portal.thread_service(user.id, identifier).audit(identifier)}

    @app.post("/api/threads/{identifier}/decision", response_model=Envelope[ApprovalThread])
    def decision(identifier: UUID, data: ReviewDecision, user: User) -> object:
        if identifier != data.thread_id:
            raise ActionConflict("Wrong approval thread")
        service = portal.thread_service(user.id, identifier)
        with portal.approval(service) as workflow:
            return {"data": workflow.resume(data)}

    @app.post("/api/threads/{identifier}/recover", response_model=Envelope[ApprovalThread])
    def recover(identifier: UUID, user: User) -> object:
        service = portal.thread_service(user.id, identifier)
        with portal.approval(service) as workflow:
            return {"data": workflow.recover(identifier)}

    @app.post("/api/threads/{identifier}/cancel")
    def cancel(identifier: UUID, user: User) -> object:
        return {"data": portal.thread_service(user.id, identifier).cancel(identifier)}

    @app.get("/api/preferences", response_model=Envelope[tuple[MemoryItem, ...]])
    def preferences(user: User) -> object:
        return {"data": portal.memory(user.id).list()}

    @app.put("/api/preferences", response_model=Envelope[MemoryItem])
    def preference(data: MemoryInput, user: User) -> object:
        return {"data": portal.memory(user.id).confirm(data.item, explicitly_confirmed=True)}

    @app.delete("/api/preferences/{identifier}")
    def forget_preference(identifier: UUID, user: User, version: int = Query(ge=1)) -> object:
        portal.memory(user.id).delete(identifier, expected_version=version)
        return {"data": {"status": "deleted"}}

    @app.get("/api/chat/config")
    def chat_configuration(user: User) -> object:
        return {"data": chat_mode}

    @app.post("/api/conversations", response_model=Envelope[Conversation], status_code=201)
    def create_conversation(user: User) -> object:
        return {"data": chat.create(user.id)}

    @app.get("/api/conversations", response_model=Envelope[Page[Conversation]])
    def conversations(
        user: User, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)
    ) -> object:
        values = chat.list(user.id, offset, limit)
        return {
            "data": {
                "items": values,
                "offset": offset,
                "next_offset": offset + limit if len(values) == limit else None,
            }
        }

    @app.get("/api/conversations/{identifier}", response_model=Envelope[Transcript])
    def conversation(
        identifier: UUID,
        user: User,
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=50),
    ) -> object:
        return {"data": chat.read(user.id, identifier, offset, limit)}

    @app.post("/api/conversations/{identifier}/messages", response_model=Envelope[Operation])
    def message(identifier: UUID, data: MessageInput, user: User) -> object:
        result = chat.submit(user.id, identifier, data)
        if control := current_run():
            control.note_result(result)
        return {"data": result}

    @app.post(
        "/api/conversations/{identifier}/operations/{operation_id}/refresh",
        response_model=Envelope[Operation],
    )
    def refresh_operation(identifier: UUID, operation_id: UUID, user: User) -> object:
        result = chat.refresh(user.id, identifier, operation_id)
        if control := current_run():
            control.note_result(result)
        return {"data": result}

    @app.delete("/api/conversations/{identifier}")
    def delete_conversation(identifier: UUID, user: User) -> object:
        chat.delete(user.id, identifier)
        return {"data": {"status": "deleted"}}

    static = Path(__file__).resolve().parents[1] / "web"
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/docs", include_in_schema=False)
    def local_docs() -> FileResponse:
        return FileResponse(static / "api.html")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
