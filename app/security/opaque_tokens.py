import hashlib
import secrets


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    key = f"tf_live_{secrets.token_urlsafe(32)}"
    return key, key[:16]
