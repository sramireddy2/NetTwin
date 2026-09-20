"""Signed attestations: netverify's statement that a snapshot satisfied a policy.

The key lives in the shared state directory. twinlab verifies the MAC before it will export
a change, so an agent cannot forge a passing verification.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from pathlib import Path

from nettwin_core.models import Attestation


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.hex() + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return key


def _message(snapshot_id: str, policy_sha256: str, issued_at: datetime) -> bytes:
    return f"{snapshot_id}|{policy_sha256}|{issued_at.isoformat()}".encode()


def sign(key: bytes, snapshot_id: str, policy_sha256: str, issued_at: datetime) -> str:
    return hmac.new(
        key, _message(snapshot_id, policy_sha256, issued_at), hashlib.sha256
    ).hexdigest()


def issue(key: bytes, snapshot_id: str, policy_sha256: str) -> Attestation:
    issued_at = datetime.now(UTC)
    return Attestation(
        snapshot_id=snapshot_id,
        policy_sha256=policy_sha256,
        issued_at=issued_at,
        mac=sign(key, snapshot_id, policy_sha256, issued_at),
    )


def verify(key: bytes, attestation: Attestation) -> bool:
    expected = sign(key, attestation.snapshot_id, attestation.policy_sha256, attestation.issued_at)
    return hmac.compare_digest(expected, attestation.mac)
