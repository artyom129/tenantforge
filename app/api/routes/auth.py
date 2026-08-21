from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, Session
from app.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest, RegisterRequest, TokenPair
from app.schemas.users import UserRead
from app.services.auth import (
    authenticate_user,
    issue_tokens,
    register_user,
    revoke_refresh_token,
    rotate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: Session) -> UserRead:
    user = await register_user(session, payload.email, payload.password)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, session: Session) -> TokenPair:
    user = await authenticate_user(session, payload.email, payload.password)
    tokens = await issue_tokens(session, user)
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, session: Session) -> TokenPair:
    tokens = await rotate_refresh_token(session, payload.refresh_token)
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, session: Session) -> Response:
    await revoke_refresh_token(session, payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
async def me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)
