"""Scoped Ed25519 JWTs. The orchestrator signs; the MCP server holds only the public key.

READ token: multi-use, read tools only, minted at intake.
EXEC token: single-use (`jti`), bound to one step's tool + op class + args hash + approval,
minted only after a recorded gate decision, immediately before execution.
"""

import time
import uuid
from pathlib import Path
from typing import Any, Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel

AUDIENCE = "mcp-server"
ISSUER = "cloudscale-orchestrator"
READ_TTL_S = 600
EXEC_TTL_S = 600


class StepClaim(BaseModel):
    step_id: str
    tool: str
    op_class: str
    args_hash: str


class ApprovalClaim(BaseModel):
    decision_id: str
    gate: str
    approver: str          # "system" for AUTO
    role: str | None = None


class TokenClaims(BaseModel):
    typ: Literal["read", "exec"]
    incident_id: str
    namespace: str
    cloud_provider: str
    jti: str
    exp: int
    step: StepClaim | None = None
    approval: ApprovalClaim | None = None
    plan_hash: str | None = None


class TokenError(Exception):
    pass


def generate_keypair(private_path: Path, public_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def load_private_key(path: Path) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def load_public_key(path: Path) -> Ed25519PublicKey:
    return serialization.load_pem_public_key(path.read_bytes())


class TokenIssuer:
    def __init__(self, private_key: Ed25519PrivateKey):
        self.key = private_key

    def _sign(self, claims: dict[str, Any], ttl_s: int) -> str:
        now = int(time.time())
        payload = claims | {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + ttl_s,
                            "jti": uuid.uuid4().hex}
        return jwt.encode(payload, self.key, algorithm="EdDSA")

    def mint_read(self, incident_id: str, namespace: str, cloud_provider: str) -> str:
        return self._sign({"typ": "read", "incident_id": incident_id, "namespace": namespace,
                           "cloud_provider": cloud_provider}, READ_TTL_S)

    def mint_exec(self, *, incident_id: str, namespace: str, cloud_provider: str, plan_hash: str,
                  step: StepClaim, approval: ApprovalClaim) -> str:
        return self._sign({"typ": "exec", "incident_id": incident_id, "namespace": namespace,
                           "cloud_provider": cloud_provider, "plan_hash": plan_hash,
                           "step": step.model_dump(), "approval": approval.model_dump()}, EXEC_TTL_S)


class TokenVerifier:
    def __init__(self, public_key: Ed25519PublicKey):
        self.key = public_key

    def verify(self, token: str) -> TokenClaims:
        try:
            payload = jwt.decode(token, self.key, algorithms=["EdDSA"], audience=AUDIENCE, issuer=ISSUER,
                                 options={"require": ["exp", "aud", "iss", "jti"]})
        except jwt.PyJWTError as e:
            raise TokenError(f"invalid_token: {type(e).__name__}") from e
        claims = TokenClaims.model_validate(payload, strict=False)
        if claims.typ == "exec" and (claims.step is None or claims.approval is None):
            raise TokenError("invalid_token: exec token without step/approval")
        return claims
