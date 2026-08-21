from typing import Any

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(404, code, message)


class ForbiddenError(AppError):
    def __init__(self, code: str = "forbidden", message: str = "Permission denied") -> None:
        super().__init__(403, code, message)


class ConflictError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class AuthenticationError(AppError):
    def __init__(self, code: str = "authentication_required", message: str = "Invalid credentials"):
        super().__init__(401, code, message, {"WWW-Authenticate": "Bearer"})


def _payload(request: Request, code: str, message: str, details: Any = None) -> dict:
    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }
    if details is not None:
        body["details"] = details
    return {"error": body}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_payload(request, exc.code, exc.message),
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_payload(request, "validation_error", "Request validation failed", details),
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content=_payload(request, "http_error", message),
        headers=exc.headers,
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger().exception("unhandled_error", error_type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content=_payload(request, "internal_error", "Internal server error"),
    )
