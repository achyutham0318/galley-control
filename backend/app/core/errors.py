import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


class ErrorField(BaseModel):
    loc: list[str | int] = Field(description="Location of the invalid field")
    msg: str = Field(description="Validation error message")
    type: str = Field(description="Validation error type")


class ErrorResponse(BaseModel):
    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error summary")
    request_id: str = Field(description="Correlation UUID for the request")
    fields: list[ErrorField] | None = Field(
        default=None,
        description="Field-level validation errors when applicable",
    )


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        fields: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.fields = fields
        self.headers = headers
        super().__init__(message)


class BadRequestError(AppError):
    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(code=code, message=message, status_code=400)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Authentication required", code: str = "UNAUTHORIZED") -> None:
        super().__init__(code=code, message=message, status_code=401)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Operation not permitted", code: str = "FORBIDDEN") -> None:
        super().__init__(code=code, message=message, status_code=403)


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found", code: str = "NOT_FOUND") -> None:
        super().__init__(code=code, message=message, status_code=404)


class ConflictError(AppError):
    def __init__(self, message: str, code: str = "CONFLICT") -> None:
        super().__init__(code=code, message=message, status_code=409)


class ValidationError(AppError):
    def __init__(
        self,
        message: str = "Validation failed",
        code: str = "VALIDATION_ERROR",
        fields: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, status_code=422, fields=fields)


class RateLimitError(AppError):
    def __init__(
        self,
        message: str = "Too many requests",
        code: str = "RATE_LIMITED",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, status_code=429, headers=headers)


class LockedError(AppError):
    def __init__(
        self,
        message: str = "Account is temporarily locked",
        code: str = "ACCOUNT_LOCKED",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, status_code=423, headers=headers)


class ServiceUnavailableError(AppError):
    def __init__(
        self,
        message: str = "Service temporarily unavailable",
        code: str = "SERVICE_UNAVAILABLE",
    ) -> None:
        super().__init__(code=code, message=message, status_code=503)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def _resolve_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


def _build_error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    fields: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    if fields is not None:
        payload["fields"] = fields
    response_headers = {"X-Request-ID": request_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=response_headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _build_error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request_id=_resolve_request_id(request),
            fields=exc.fields,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "loc": list(err.get("loc", [])),
                "msg": str(err.get("msg", "Invalid value")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        return _build_error_response(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request validation failed",
            request_id=_resolve_request_id(request),
            fields=fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _build_error_response(
            status_code=exc.status_code,
            code=f"HTTP_{exc.status_code}",
            message=str(exc.detail) if exc.detail else "HTTP error",
            request_id=_resolve_request_id(request),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return _build_error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred",
            request_id=_resolve_request_id(request),
        )
