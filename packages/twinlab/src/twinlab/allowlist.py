"""Compatibility shim: the allowlist lives in nettwin_core so netverify can share it."""

from nettwin_core.allowlist import (
    EXAMPLES,
    FRR_FAMILIES,
    FRR_TIMEOUT,
    KERNEL_TIMEOUT,
    PROBE_TIMEOUT,
    AllowedCommand,
    CommandNotAllowed,
    check,
)

__all__ = [
    "EXAMPLES",
    "FRR_FAMILIES",
    "FRR_TIMEOUT",
    "KERNEL_TIMEOUT",
    "PROBE_TIMEOUT",
    "AllowedCommand",
    "CommandNotAllowed",
    "check",
]
