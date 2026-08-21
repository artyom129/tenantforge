from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.config import get_settings
from app.core.errors import (
    AppError,
    app_error_handler,
    http_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from app.core.logging import RequestContextMiddleware, configure_logging
from app.infrastructure.redis import close_redis


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Multi-tenant SaaS backend reference implementation.",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "Auth"},
            {"name": "Organizations"},
            {"name": "Members"},
            {"name": "Invitations"},
            {"name": "API Keys"},
            {"name": "Projects"},
            {"name": "Usage"},
            {"name": "Billing"},
            {"name": "Audit"},
            {"name": "Health"},
        ],
    )
    application.add_middleware(RequestContextMiddleware)
    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(StarletteHTTPException, http_error_handler)
    application.add_exception_handler(Exception, unexpected_error_handler)
    application.include_router(api_router)
    return application


app = create_app()
