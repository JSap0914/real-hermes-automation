from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ledger import ActionLedger, LedgerError
from .memory import redact_secrets
from .models import ActionType, ApprovalState, Platform, Sink, SourceProvenance, SourceType, Target, TrustLevel, make_action, new_id, utc_now


class ImprovementError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutcomeEvent:
    outcome_id: str
    action_id: str
    platform: str
    metrics: dict[str, Any]
    notes: str
    collected_at: str


@dataclass(frozen=True)
class ImprovementProposal:
    proposal_id: str
    status: str
    evidence: dict[str, Any]
    suggested_update: str
    risk: str
    rollback_path: str
    target_path: str
    ledger_action_id: str | None


class OutcomeCollector:
    """Ledger-linked outcome capture for supervised self-improvement.

    This stores metrics/feedback as local evidence only. It never updates
    persona or memory directly; proposed learnings go through
    ImprovementProposalStore and require ledgered promotion.
    """

    def __init__(self, ledger: ActionLedger) -> None:
        self.ledger = ledger

    def record(self, *, action_id: str, metrics: dict[str, Any], notes: str = "") -> OutcomeEvent:
        action = self.ledger.get_action(action_id)
        safe_notes, _ = redact_secrets(notes)
        safe_metrics = _safe_metrics(metrics)
        outcome = OutcomeEvent(new_id("outcome"), action_id, action.platform, safe_metrics, safe_notes, utc_now())
        with self.ledger.connect() as conn:
            conn.execute(
                "INSERT INTO outcome_events(outcome_id, action_id, platform, metrics_json, notes, collected_at) VALUES (?, ?, ?, ?, ?, ?)",
                (outcome.outcome_id, outcome.action_id, outcome.platform, json.dumps(outcome.metrics, sort_keys=True, ensure_ascii=False), outcome.notes, outcome.collected_at),
            )
        return outcome

    def list_recent(self, *, limit: int = 50) -> list[OutcomeEvent]:
        with self.ledger.connect() as conn:
            rows = conn.execute("SELECT * FROM outcome_events ORDER BY collected_at DESC LIMIT ?", (limit,)).fetchall()
        return [OutcomeEvent(row["outcome_id"], row["action_id"], row["platform"], json.loads(row["metrics_json"]), row["notes"] or "", row["collected_at"]) for row in rows]


class ImprovementProposalStore:
    def __init__(self, ledger: ActionLedger, *, proposal_dir: str | Path = "memory/wiki/proposed-learnings", voice_guide_path: str | Path = "memory/wiki/voice-guide.md") -> None:
        self.ledger = ledger
        self.proposal_dir = Path(proposal_dir)
        self.voice_guide_path = Path(voice_guide_path)

    def propose_from_outcomes(self, *, title: str = "engagement-learning", limit: int = 20) -> ImprovementProposal:
        outcomes = OutcomeCollector(self.ledger).list_recent(limit=limit)
        if not outcomes:
            raise ImprovementError("cannot propose learning without outcome evidence")
        evidence = {
            "outcome_ids": [outcome.outcome_id for outcome in outcomes],
            "action_ids": [outcome.action_id for outcome in outcomes],
            "platforms": sorted({outcome.platform for outcome in outcomes}),
            "metrics": [outcome.metrics for outcome in outcomes],
        }
        best = _summarize_evidence(outcomes)
        suggested_update = f"Proposed learning: {best}"
        return self.create_proposal(
            evidence=evidence,
            suggested_update=suggested_update,
            risk="May overfit to a small or biased engagement sample; keep as staged learning until reviewed.",
            rollback_path="Revert the appended voice-guide section or tombstone this proposal.",
            target_path=str(self.voice_guide_path),
            title=title,
        )

    def create_proposal(
        self,
        *,
        evidence: dict[str, Any],
        suggested_update: str,
        risk: str,
        rollback_path: str,
        target_path: str,
        title: str = "learning",
    ) -> ImprovementProposal:
        safe_update, _ = redact_secrets(suggested_update)
        safe_risk, _ = redact_secrets(risk)
        safe_rollback, _ = redact_secrets(rollback_path)
        proposal = ImprovementProposal(new_id("learn"), "proposed", dict(evidence), safe_update, safe_risk, safe_rollback, target_path, None)
        self.proposal_dir.mkdir(parents=True, exist_ok=True)
        path = self.proposal_dir / f"{title}-{proposal.proposal_id}.md"
        path.write_text(_render_proposal(proposal), encoding="utf-8")
        now = utc_now()
        with self.ledger.connect() as conn:
            conn.execute(
                """
                INSERT INTO improvement_proposals(
                  proposal_id, status, evidence_json, suggested_update, risk, rollback_path,
                  target_path, ledger_action_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.status,
                    json.dumps(proposal.evidence, sort_keys=True, ensure_ascii=False),
                    proposal.suggested_update,
                    proposal.risk,
                    proposal.rollback_path,
                    proposal.target_path,
                    proposal.ledger_action_id,
                    now,
                    now,
                ),
            )
        return proposal

    def promote(self, proposal_id: str, *, ledger_action_id: str, actor: str = "owner") -> ImprovementProposal:
        proposal = self.get(proposal_id)
        try:
            action = self.ledger.get_action(ledger_action_id)
        except LedgerError as exc:
            raise ImprovementError("promotion requires an existing ledger action") from exc
        if action.action_type != ActionType.AGENT_MEMORY_WRITE:
            raise ImprovementError("promotion ledger action must be agent_memory_write")
        if action.metadata.get("improvement_proposal_id") != proposal_id:
            raise ImprovementError("promotion ledger action is not bound to this proposal")
        if str(action.metadata.get("target_path")) != str(proposal.target_path):
            raise ImprovementError("promotion ledger action target does not match proposal target")
        if proposal_id not in action.source_ids:
            raise ImprovementError("promotion ledger action must cite the proposal as a source")
        state = self.ledger.state(ledger_action_id)
        if state not in {ApprovalState.DRY_RUN_COMPLETED.value, ApprovalState.APPROVED_FOR_LIVE.value, ApprovalState.COMPLETED.value}:
            raise ImprovementError(f"promotion action is not approved/completed: {state}")
        target = Path(proposal.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        addition = f"\n\n## Approved learning {proposal.proposal_id}\n\n{proposal.suggested_update}\n\nRisk: {proposal.risk}\nRollback: {proposal.rollback_path}\n"
        target.write_text(existing.rstrip() + addition, encoding="utf-8")
        now = utc_now()
        with self.ledger.connect() as conn:
            conn.execute("UPDATE improvement_proposals SET status='promoted', ledger_action_id=?, updated_at=? WHERE proposal_id=?", (ledger_action_id, now, proposal_id))
        self.ledger.transition(ledger_action_id, ApprovalState.DRY_RUN_COMPLETED, actor=actor, result={"promoted_proposal_id": proposal_id, "target_path": str(target)}, event_type="improvement_promoted")
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> ImprovementProposal:
        with self.ledger.connect() as conn:
            row = conn.execute("SELECT * FROM improvement_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            raise ImprovementError(f"unknown proposal: {proposal_id}")
        return ImprovementProposal(
            row["proposal_id"],
            row["status"],
            json.loads(row["evidence_json"]),
            row["suggested_update"],
            row["risk"],
            row["rollback_path"],
            row["target_path"],
            row["ledger_action_id"],
        )

    def create_promotion_action(self, proposal_id: str, *, actor: str = "owner") -> str:
        proposal = self.get(proposal_id)
        provenance = SourceProvenance(SourceType.LOCAL_NOTE, TrustLevel.UNKNOWN, (Sink.MEMORY,))
        env = make_action(
            action_type=ActionType.AGENT_MEMORY_WRITE,
            target=Target(Platform.LOCAL, "voice-guide", agent_name="self_improvement"),
            text=proposal.suggested_update,
            source_ids=[proposal_id],
            provenance=provenance,
            created_by=actor,
            mode="dry_run",
            metadata={"improvement_proposal_id": proposal_id, "target_path": proposal.target_path},
        )
        return self.ledger.create_action(env, actor=actor)


def _safe_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metrics.items():
        clean_key, _ = redact_secrets(str(key))
        if isinstance(value, (int, float, bool)) or value is None:
            safe[clean_key] = value
        else:
            clean_value, _ = redact_secrets(str(value))
            safe[clean_key] = clean_value[:500]
    return safe


def _summarize_evidence(outcomes: list[OutcomeEvent]) -> str:
    platforms = ", ".join(sorted({outcome.platform for outcome in outcomes}))
    positive = sum(1 for outcome in outcomes if float(outcome.metrics.get("score", outcome.metrics.get("engagement_score", 0)) or 0) > 0)
    return f"{positive}/{len(outcomes)} recent outcomes on {platforms} showed positive signal; prefer the styles evidenced by those source-linked actions, but keep future updates staged before promotion."


def _render_proposal(proposal: ImprovementProposal) -> str:
    return (
        f"# Proposed learning {proposal.proposal_id}\n\n"
        f"Status: {proposal.status}\n\n"
        f"## Evidence\n\n```json\n{json.dumps(proposal.evidence, indent=2, ensure_ascii=False, sort_keys=True)}\n```\n\n"
        f"## Suggested update\n\n{proposal.suggested_update}\n\n"
        f"## Risk\n\n{proposal.risk}\n\n"
        f"## Rollback path\n\n{proposal.rollback_path}\n"
    )
