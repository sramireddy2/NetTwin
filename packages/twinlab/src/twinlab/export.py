"""Export bundles: the only thing that leaves the twin, and only after a human says yes.

An export ties together the applied changes, the verifier's signed report and the agent's
root-cause writeup. `prepare` validates everything the server can check on its own (the
attestation signature, that it covers the state after the last change, that the policy is
the one in force). The decision itself comes from MCP elicitation, the admin approve route,
or bench mode; it is never an argument the agent supplies.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nettwin_core.attest import verify
from nettwin_core.models import ChangeResult, RootCause, VerificationReport


class ExportError(ValueError):
    pass


class ExportBundle(BaseModel):
    export_id: str
    created_at: datetime
    status: Literal["pending", "approved", "declined"]
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str = ""
    changes: list[ChangeResult]
    verification: VerificationReport
    root_cause: RootCause
    summary: str = ""

    @property
    def nodes(self) -> list[str]:
        return sorted({c.node for c in self.changes})


class ExportResult(BaseModel):
    export_id: str
    status: Literal["pending", "approved", "declined"]
    path: str
    decided_by: str | None = None
    message: str = Field(description="What happened and, if pending, how to approve")


class Approval(BaseModel):
    """What the operator is asked during elicitation."""

    approve: bool = Field(description="Export this verified change for production?")
    note: str = Field(default="", description="Optional note recorded with the decision")


def validate(
    changes: list[ChangeResult], verification: VerificationReport, key: bytes, policy_sha256: str
) -> None:
    if not changes:
        raise ExportError("nothing to export: no change ids given")
    if not verification.passed:
        raise ExportError(f"verification did not pass; failed rules: {verification.failed_rules}")
    attestation = verification.attestation
    if attestation is None:
        raise ExportError("verification carries no attestation")
    if not verify(key, attestation):
        raise ExportError("attestation signature is invalid")
    if attestation.snapshot_id != verification.snapshot_id:
        raise ExportError("attestation does not match the verification's snapshot")
    if attestation.policy_sha256 != policy_sha256:
        raise ExportError("attestation was issued for a different intent policy")
    last = changes[-1]
    if attestation.snapshot_id != last.after_snapshot_id:
        raise ExportError(
            f"attestation covers snapshot {attestation.snapshot_id[:12]} but the state after the "
            f"last change {last.change_id} is {last.after_snapshot_id[:12]}; run intent_check "
            "after the last change and export again"
        )


def new_bundle(
    changes: list[ChangeResult],
    verification: VerificationReport,
    root_cause: RootCause,
    summary: str,
) -> ExportBundle:
    return ExportBundle(
        export_id=secrets.token_hex(6),
        created_at=datetime.now(UTC),
        status="pending",
        changes=changes,
        verification=verification,
        root_cause=root_cause,
        summary=summary,
    )


def render_patch(bundle: ExportBundle) -> str:
    parts = []
    for change in bundle.changes:
        parts.append(f"# change {change.change_id} on {change.node}: {change.rationale}".rstrip())
        parts.append(change.diff.rstrip())
        parts.append("")
    return "\n".join(parts)


def render_root_cause(bundle: ExportBundle) -> str:
    rc = bundle.root_cause
    lines = [
        f"# Root cause: {rc.component} on {rc.node}",
        "",
        f"Layer: {rc.layer}",
        "",
        rc.summary,
        "",
    ]
    if bundle.summary:
        lines += ["## Summary", "", bundle.summary, ""]
    lines += ["## Changes", ""]
    for change in bundle.changes:
        lines.append(
            f"- `{change.change_id}` on **{change.node}**: {change.rationale or '(no rationale)'}"
        )
    lines += ["", "## Verification", ""]
    v = bundle.verification
    lines.append(
        f"Snapshot `{v.snapshot_id[:12]}`, policy `{v.policy_sha256[:12]}`, passed: {v.passed}"
    )
    lines += ["", "| Rule | Kind | Result | Evidence |", "|---|---|---|---|"]
    for rule in v.rules:
        verdict = "pass" if rule.passed else "FAIL"
        lines.append(f"| {rule.rule_id} | {rule.kind} | {verdict} | {rule.detail} |")
    lines += [
        "",
        f"Decision: {bundle.status} by {bundle.decided_by or '-'} {bundle.note}".rstrip(),
        "",
    ]
    return "\n".join(lines)


def preview(bundle: ExportBundle) -> str:
    rc = bundle.root_cause
    nodes = ", ".join(bundle.nodes)
    header = (
        f"Export verified change {bundle.export_id} to production?\n"
        f"Root cause: {rc.component} on {rc.node} ({rc.layer}): {rc.summary}\n"
        f"Nodes touched: {nodes}; changes: {len(bundle.changes)}; "
        f"verification: {len(bundle.verification.rules)} rules passed, "
        f"snapshot {bundle.verification.snapshot_id[:12]}\n"
    )
    patch = render_patch(bundle)
    if len(patch) > 1500:
        patch = patch[:1500] + "\n... (truncated)"
    return header + "\n" + patch


class ExportStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, export_id: str) -> Path:
        return self.directory / export_id

    def save(self, bundle: ExportBundle) -> Path:
        folder = self.path(bundle.export_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bundle.json").write_text(bundle.model_dump_json(indent=1), encoding="utf-8")
        if bundle.status == "approved":
            (folder / "diff.patch").write_text(render_patch(bundle), encoding="utf-8")
            (folder / "root_cause.md").write_text(render_root_cause(bundle), encoding="utf-8")
            (folder / "verification.json").write_text(
                bundle.verification.model_dump_json(indent=1), encoding="utf-8"
            )
        return folder

    def load(self, export_id: str) -> ExportBundle:
        path = self.path(export_id) / "bundle.json"
        if not path.exists():
            raise KeyError(f"unknown export {export_id!r}")
        return ExportBundle.model_validate_json(path.read_text(encoding="utf-8"))

    def ids(self) -> list[str]:
        folders = [p for p in self.directory.iterdir() if (p / "bundle.json").exists()]
        return [p.name for p in sorted(folders, key=lambda p: p.stat().st_mtime)]
