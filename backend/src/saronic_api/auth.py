"""Shared-key API auth.

DECISION(D4): one shared key in X-API-Key, required in production (enforced at startup in
main.py). Replace when the team picks real auth.
"""

import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(request: Request, provided: str | None = Security(_api_key_header)) -> None:
    expected = request.app.state.settings.api_key
    if expected is None:
        # Only reachable outside production; startup refuses production without a key.
        return
    if provided is None or not secrets.compare_digest(
        provided.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
