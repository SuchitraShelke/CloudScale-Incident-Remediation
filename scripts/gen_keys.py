"""Generate the Ed25519 token-signing keypair into keys/ (gitignored): `uv run python scripts/gen_keys.py`.
The orchestrator mounts both files; the MCP server mounts only the public key."""

import sys
from pathlib import Path

from cloudscale.common.tokens import generate_keypair

KEYS = Path(__file__).resolve().parents[1] / "keys"

if __name__ == "__main__":
    KEYS.mkdir(exist_ok=True)
    priv, pub = KEYS / "signing_private.pem", KEYS / "signing_public.pem"
    if priv.exists() and "--force" not in sys.argv:
        print(f"{priv} exists (use --force to rotate)")
        sys.exit(0)
    generate_keypair(priv, pub)
    print(f"wrote {priv} and {pub}")
