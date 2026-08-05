import secrets

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

basic_auth = HTTPBasic()
OPEN_API_KEY_ENVS = {"development", "dev", "local", "test", "testing"}


def require_admin(credentials: HTTPBasicCredentials = Depends(basic_auth)) -> str:
    username_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected_key = settings.internal_api_key.strip()
    if not expected_key:
        if settings.app_env.lower().strip() in OPEN_API_KEY_ENVS:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API key is not configured",
        )
    if not secrets.compare_digest(x_api_key or "", expected_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
