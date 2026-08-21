from fastapi import APIRouter

from app.api.routes import (
    api_keys,
    audit,
    auth,
    billing,
    health,
    invitations,
    members,
    metrics,
    organizations,
    projects,
    usage,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(organizations.router)
api_router.include_router(members.router)
api_router.include_router(invitations.router)
api_router.include_router(api_keys.router)
api_router.include_router(projects.router)
api_router.include_router(usage.router)
api_router.include_router(billing.router)
api_router.include_router(webhooks.router)
api_router.include_router(audit.router)
api_router.include_router(health.router)
api_router.include_router(metrics.router)

