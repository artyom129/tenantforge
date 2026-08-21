from app.core.errors import ForbiddenError
from app.models.enums import Role


def ensure_role(role: Role, *allowed: Role) -> None:
    if role not in allowed:
        raise ForbiddenError()


def ensure_invitation_role(actor_role: Role, invited_role: Role) -> None:
    if actor_role == Role.OWNER:
        return
    if actor_role == Role.ADMIN and invited_role == Role.MEMBER:
        return
    raise ForbiddenError(
        "invitation_role_forbidden",
        "You cannot invite a member with that role",
    )
