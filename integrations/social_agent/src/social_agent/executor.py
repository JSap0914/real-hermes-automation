from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapters import AdapterError, Executor as AdapterExecutor
from .config import AppConfig
from .ledger import ActionLedger, LedgerError
from .models import ActionEnvelope, ApprovalState


@dataclass
class Executor:
    ledger: ActionLedger
    config: AppConfig

    def __post_init__(self) -> None:
        self._adapter_executor = AdapterExecutor(self.ledger, self.config)

    def run_once(self) -> list[dict[str, Any]]:
        if self.config.runtime.paused:
            return [{"ok": True, "paused": True}]
        candidates = self.ledger.list_actions(states=[ApprovalState.DRY_RUN_READY, ApprovalState.APPROVED_FOR_LIVE], limit=25)
        results: list[dict[str, Any]] = []
        for envelope in candidates:  # type: ignore[assignment]
            try:
                results.append(self.execute(envelope))
            except (AdapterError, LedgerError) as exc:
                failed = self.ledger.transition(envelope.action_id, ApprovalState.BLOCKED, actor="executor", result={"error": str(exc)}, event_type="blocked")
                results.append({"ok": False, "action_id": failed.action_id, "error": str(exc)})
        return results

    def execute(self, envelope_or_id: ActionEnvelope | str) -> dict[str, Any]:
        action_id = envelope_or_id.action_id if isinstance(envelope_or_id, ActionEnvelope) else envelope_or_id
        result = self._adapter_executor.execute(action_id)
        final_state = self.ledger.state(action_id)
        payload = result.to_dict() if hasattr(result, "to_dict") else result
        if final_state == "dry_run_completed" and isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
            payload = payload["payload"]
        if final_state == "dry_run_completed" and isinstance(payload, dict) and "preview" not in payload:
            payload = {"preview": payload}
        return {"ok": True, "action_id": action_id, "state": final_state, "result": payload}

    def recover_stale_executing(self) -> list[str]:
        recovered: list[str] = []
        for envelope in self.ledger.list_actions(states=[ApprovalState.EXECUTING], limit=100):  # type: ignore[assignment]
            self.ledger.transition(envelope.action_id, ApprovalState.BLOCKED, actor="executor", result={"reason": "stale executing lease requires review"}, event_type="recovery_blocked")
            recovered.append(envelope.action_id)
        return recovered
