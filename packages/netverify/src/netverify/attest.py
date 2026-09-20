"""Compatibility shim: attestation helpers live in nettwin_core so twinlab can verify them."""

from nettwin_core.attest import issue, load_or_create_key, sign, verify

__all__ = ["issue", "load_or_create_key", "sign", "verify"]
