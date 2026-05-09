from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .ledger import ActionLedger
from .memory import sanitize_text
from .models import ActionType, Platform, Sink, SourceProvenance, SourceType, Target, TrustLevel, make_action, new_id, now_iso
from .policy import PolicyGate


@dataclass(frozen=True)
class AgentDirectoryEntry:
    name: str
    channel_id: str
    repo: str
    timeout_seconds: int = 1800


class AgentManager:
    def __init__(self, ledger: ActionLedger, policy: PolicyGate, directory: list[AgentDirectoryEntry]) -> None:
        self.ledger = ledger
        self.policy = policy
        self.directory = {entry.name: entry for entry in directory}

    def delegate(self, *, agent_name: str, task_text: str, requested_by: str = "owner"):
        entry = self.directory.get(agent_name)
        if not entry:
            raise ValueError("agent not allowlisted")
        safe_text, _ = sanitize_text(task_text)
        provenance = SourceProvenance(SourceType.MANUAL_PROMPT, TrustLevel.UNKNOWN, (Sink.PRIVATE_REPLY, Sink.MEMORY))
        target = Target(Platform.DISCORD, entry.channel_id)
        decision = self.policy.decide(action_type=ActionType.DELEGATE_AGENT_TASK, target=target, text=safe_text, provenance=provenance)
        envelope = make_action(
            action_type=ActionType.DELEGATE_AGENT_TASK,
            target=target,
            text=safe_text,
            source_ids=[new_id("manual")],
            provenance=provenance,
            policy=decision,
            created_by=requested_by,
            mode="dry_run",
        )
        self.ledger.create_action(envelope)
        task_id = new_id("agenttask")
        with self.ledger.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_tasks(task_id, action_id, target_channel, repo, status, transcript, timeout_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, envelope.action_id, entry.channel_id, entry.repo, "queued", "", entry.timeout_seconds, now_iso(), now_iso()),
            )
        return envelope

    def record_result(self, task_id: str, transcript: str, result_summary: str) -> None:
        safe_transcript, _ = sanitize_text(transcript)
        safe_summary, _ = sanitize_text(result_summary)
        with self.ledger.connect() as conn:
            conn.execute(
                "UPDATE agent_tasks SET transcript=?, result_summary=?, status='completed', updated_at=? WHERE task_id=?",
                (safe_transcript, safe_summary, now_iso(), task_id),
            )


def write_agent_directory(path: str | Path, entries: list[AgentDirectoryEntry]) -> None:
    p = Path(path)
    p.write_text("# Agent Directory\n\n" + "\n".join(f"- {e.name}: channel={e.channel_id}, repo={e.repo}, timeout={e.timeout_seconds}s" for e in entries) + "\n", encoding="utf-8")
