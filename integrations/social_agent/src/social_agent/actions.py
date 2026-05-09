from __future__ import annotations

from typing import Any

from .ledger import ActionLedger, LedgerError
from .models import (
    ActionEnvelope,
    ActionType,
    ApprovalState,
    PolicyDecision,
    SourceProvenance,
    Target,
    Content,
    ValidationError,
)


class InvalidActionError(ValidationError):
    pass


def create_action_envelope(
    *,
    created_by: str,
    source_ids: list[str] | tuple[str, ...],
    source_provenance: SourceProvenance | dict[str, Any],
    action_type: str | ActionType,
    target: Target | dict[str, Any],
    content: Content | dict[str, Any],
    policy: PolicyDecision | dict[str, Any],
    approval: dict[str, Any],
    execution: dict[str, Any],
    audit: dict[str, Any],
    action_id: str | None = None,
    schema_version: int = 1,
    created_at: str | None = None,
) -> ActionEnvelope:
    try:
        if not source_ids:
            raise InvalidActionError("source_ids are required")
        if isinstance(content, dict) and "content_hash" not in content:
            raise InvalidActionError("content.content_hash is required")
        if "idempotency_key" not in execution:
            raise InvalidActionError("execution.idempotency_key is required")
        data = {
            "created_by": created_by,
            "source_ids": list(source_ids),
            "source_provenance": source_provenance,
            "action_type": action_type,
            "target": target,
            "content": content,
            "policy": policy,
            "approval": approval,
            "execution": execution,
            "audit": audit,
            "schema_version": schema_version,
        }
        if action_id is not None:
            data["action_id"] = action_id
        if created_at is not None:
            data["created_at"] = created_at
        return ActionEnvelope(**data)
    except Exception as exc:
        if isinstance(exc, InvalidActionError):
            raise
        raise InvalidActionError(str(exc)) from exc


__all__ = ["ActionEnvelope", "ActionLedger", "ApprovalState", "InvalidActionError", "SourceProvenance", "create_action_envelope"]
