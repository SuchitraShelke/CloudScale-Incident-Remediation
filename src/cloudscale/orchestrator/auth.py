"""Demo identity: users seeded from DEMO_USERS, scrypt-hashed at startup, HS256 session tokens.
Production path (ADR): OIDC with group -> role mapping. The approver recorded in the audit log
always comes from the verified session, never from a request body."""

import hashlib
import hmac
import os
import time
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

Role = Literal["viewer", "sre", "senior_sre"]
SESSION_TTL_S = 8 * 3600


class User(BaseModel):
    username: str
    role: Role


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


class Auth:
    def __init__(self, secret: str, users_spec: str):
        """users_spec: 'name:password:role,...'"""
        if len(secret.encode()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 bytes (HS256)")
        self.secret = secret
        self.users: dict[str, tuple[bytes, bytes, Role]] = {}
        for entry in filter(None, (u.strip() for u in users_spec.split(","))):
            name, password, role = entry.split(":")
            salt = os.urandom(16)
            self.users[name] = (salt, _hash(password, salt), role)  # plaintext is not kept

    def login(self, username: str, password: str) -> tuple[str, User] | None:
        rec = self.users.get(username)
        if rec is None or not hmac.compare_digest(_hash(password, rec[0]), rec[1]):
            return None
        user = User(username=username, role=rec[2])
        token = jwt.encode({"sub": username, "role": user.role, "exp": int(time.time()) + SESSION_TTL_S},
                           self.secret, algorithm="HS256")
        return token, user

    def verify(self, token: str) -> User:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError as e:
            raise HTTPException(401, "Session expired or invalid. Log in again.") from e
        if claims["sub"] not in self.users:
            raise HTTPException(401, "Unknown user")
        return User(username=claims["sub"], role=claims["role"])


def current_user(request: Request) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Log in first (POST /auth/login), then send 'Authorization: Bearer <token>'.")
    return request.app.state.auth.verify(header[7:])


def require(*roles: Role):
    def check(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, f"Role '{user.role}' can't do this; needs one of {list(roles)}.")
        return user
    return check
