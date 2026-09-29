from __future__ import annotations

import logging
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.exceptions import OrchestratorDomainException

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    """Registers RFC 7807 Problem Details exception handlers."""

    @app.exception_handler(OrchestratorDomainException)
    async def domain_exception_handler(
        request: Request, exc: OrchestratorDomainException
    ) -> JSONResponse:
        problem_doc = exc.to_problem_detail(instance=str(request.url.path))
        logger.warning(
            f"Domain error {exc.status_code} on {request.method} {request.url.path}: {exc.detail}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=problem_doc,
            headers={"Content-Type": "application/problem+json"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problem_doc = {
            "type": "https://api.platform.internal/errors/validation-error",
            "title": "Request Validation Failed",
            "status": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "detail": "One or more request parameters or body fields failed validation.",
            "instance": str(request.url.path),
            "invalid_params": [
                {
                    "name": ".".join(str(loc) for loc in err["loc"]),
                    "reason": err["msg"],
                    "type": err["type"],
                }
                for err in exc.errors()
            ],
        }
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=problem_doc,
            headers={"Content-Type": "application/problem+json"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        problem_doc = {
            "type": f"https://api.platform.internal/errors/http-{exc.status_code}",
            "title": exc.detail if isinstance(exc.detail, str) else "HTTP Error",
            "status": exc.status_code,
            "detail": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            "instance": str(request.url.path),
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=problem_doc,
            headers={"Content-Type": "application/problem+json"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            f"Unhandled server error processing {request.method} {request.url.path}: {exc}"
        )
        problem_doc = {
            "type": "https://api.platform.internal/errors/internal-server-error",
            "title": "Internal Server Error",
            "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "detail": "An unexpected server error occurred while processing the request.",
            "instance": str(request.url.path),
        }
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=problem_doc,
            headers={"Content-Type": "application/problem+json"},
        )
