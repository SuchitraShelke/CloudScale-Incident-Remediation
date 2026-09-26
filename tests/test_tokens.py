import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloudscale.common.tokens import ApprovalClaim, StepClaim, TokenError, TokenIssuer, TokenVerifier


@pytest.fixture
def keys():
    k = Ed25519PrivateKey.generate()
    return TokenIssuer(k), TokenVerifier(k.public_key())


STEP = StepClaim(step_id="s1", tool="apply_hotfix", op_class="DESTRUCTIVE", args_hash="abc")
APPROVAL = ApprovalClaim(decision_id="d1", gate="APPROVAL", approver="sre1", role="sre")


def test_read_token_round_trip(keys):
    issuer, verifier = keys
    c = verifier.verify(issuer.mint_read("INC-1", "production", "aws"))
    assert (c.typ, c.namespace, c.cloud_provider, c.step) == ("read", "production", "aws", None)


def test_exec_token_carries_step_and_approval(keys):
    issuer, verifier = keys
    tok = issuer.mint_exec(incident_id="INC-1", namespace="production", cloud_provider="aws",
                           plan_hash="ph", step=STEP, approval=APPROVAL)
    c = verifier.verify(tok)
    assert c.step == STEP and c.approval == APPROVAL and c.plan_hash == "ph" and c.jti


def test_each_token_has_unique_jti(keys):
    issuer, verifier = keys
    a, b = (verifier.verify(issuer.mint_read("INC-1", "p", "aws")).jti for _ in range(2))
    assert a != b


def test_forged_token_signed_by_other_key_rejected(keys):
    _, verifier = keys
    forged = TokenIssuer(Ed25519PrivateKey.generate()).mint_read("INC-1", "production", "aws")
    with pytest.raises(TokenError, match="InvalidSignature"):
        verifier.verify(forged)


def test_expired_and_wrong_audience_rejected(keys):
    issuer, verifier = keys
    now = int(time.time())
    base = {"typ": "read", "incident_id": "INC-1", "namespace": "p", "cloud_provider": "aws",
            "iss": "cloudscale-orchestrator", "jti": "j"}
    expired = jwt.encode(base | {"aud": "mcp-server", "exp": now - 10}, issuer.key, algorithm="EdDSA")
    wrong_aud = jwt.encode(base | {"aud": "other", "exp": now + 60}, issuer.key, algorithm="EdDSA")
    for tok in (expired, wrong_aud):
        with pytest.raises(TokenError):
            verifier.verify(tok)


def test_alg_none_rejected(keys):
    _, verifier = keys
    unsigned = jwt.encode({"typ": "exec", "aud": "mcp-server"}, key=None, algorithm="none")
    with pytest.raises(TokenError):
        verifier.verify(unsigned)
