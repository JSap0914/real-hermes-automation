from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import reject_secret_value
from .ledger import ActionLedger, LedgerError
from .memory import sanitize_text
from .models import (
    ActionType,
    ApprovalState,
    Platform,
    PolicyDecision,
    Sink,
    SourceProvenance,
    SourceType,
    Target,
    TrustLevel,
    make_action,
    new_id,
    stable_hash,
    utc_now,
)


DIRECT_WRITE_TOOL_PREFIXES = ("x.", "threads.", "instagram.", "discord.", "telegram.")
DIRECT_WRITE_WORDS = ("post", "publish", "send", "reply", "dm", "message", "write", "create")


@dataclass(frozen=True)
class ProviderMetadata:
    provider_name: str
    provider_version: str = "unknown"
    provider_profile_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider_profile_id:
            reject_secret_value("provider_profile_id", self.provider_profile_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "provider_profile_id": self.provider_profile_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskArtifact:
    path: str
    digest: str
    artifact_type: str = "result"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "digest": self.digest, "artifact_type": self.artifact_type, "metadata": dict(self.metadata)}


@dataclass(frozen=True)
class OrchestratorTaskResult:
    task_id: str
    action_id: str
    status: str
    prompt_digest: str
    result_digest: str | None
    artifact_paths: tuple[str, ...]
    correlation_id: str
    provider: ProviderMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action_id": self.action_id,
            "status": self.status,
            "prompt_digest": self.prompt_digest,
            "result_digest": self.result_digest,
            "artifact_paths": list(self.artifact_paths),
            "correlation_id": self.correlation_id,
            "provider": self.provider.to_dict(),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@runtime_checkable
class BrainProvider(Protocol):
    def provider_metadata(self) -> ProviderMetadata:
        ...

    def create_task(self, prompt: str, allowed_tools: list[str], provenance: dict[str, Any] | SourceProvenance, requester: str) -> str:
        ...

    def run_task(self, task_id: str) -> OrchestratorTaskResult:
        ...

    def resume_task(self, task_id: str) -> OrchestratorTaskResult:
        ...

    def cancel_task(self, task_id: str) -> OrchestratorTaskResult:
        ...

    def get_result(self, task_id: str) -> OrchestratorTaskResult:
        ...

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        ...


@runtime_checkable
class OrchestratorGateway(BrainProvider, Protocol):
    """Ledger-gated contract used by Hermes or compatible brain providers."""


class FakeHermesGateway:
    """Deterministic Hermes-compatible gateway for tests and dry-run development.

    The fake provider never talks to external services. It proves the production
    gateway contract by creating a ledger action before a task can run and by
    persisting only provider profile IDs, digests, and artifact paths in SQLite.
    """

    def __init__(
        self,
        ledger: ActionLedger,
        *,
        artifact_dir: str | Path,
        provider_name: str = "fake_hermes",
        provider_version: str = "test",
        provider_profile_id: str = "local-fake-profile",
    ) -> None:
        self.ledger = ledger
        self.artifact_dir = Path(artifact_dir)
        self._provider = ProviderMetadata(provider_name, provider_version, provider_profile_id, {"kind": "deterministic_fake"})

    def provider_metadata(self) -> ProviderMetadata:
        return self._provider

    def create_task(
        self,
        prompt: str,
        allowed_tools: list[str] | tuple[str, ...],
        provenance: dict[str, Any] | SourceProvenance,
        requester: str,
        credential_profile_id: str | None = None,
    ) -> str:
        tools = [str(tool) for tool in allowed_tools]
        _validate_allowed_tools(tools)
        safe_prompt, redacted = sanitize_text(prompt)
        source_ids, source_provenance = _coerce_provenance(provenance)
        prompt_digest = stable_hash(safe_prompt)
        correlation_id = new_id("corr")
        provider = self._provider
        if credential_profile_id:
            reject_secret_value("credential_profile_id", credential_profile_id)
            provider = ProviderMetadata(self._provider.provider_name, self._provider.provider_version, credential_profile_id, self._provider.metadata)
        action = make_action(
            action_type=ActionType.AGENT_TOOL_CALL,
            target=Target(Platform.LOCAL, "hermes-gateway"),
            text=safe_prompt,
            source_ids=source_ids,
            provenance=source_provenance,
            policy=PolicyDecision("allowed", ("ledger_gated_orchestrator_task",)),
            created_by=requester,
            mode="dry_run",
            metadata={
                "provider": provider.to_dict(),
                "allowed_tools": tools,
                "prompt_digest": prompt_digest,
                "credential_profile_id": provider.provider_profile_id,
                "correlation_id": correlation_id,
                "redaction_applied": redacted,
            },
        )
        self.ledger.create_action(action, actor=requester)
        task_id = new_id("hermes")
        now = utc_now()
        with self.ledger.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_tasks(
                    task_id, action_id, agent_name, channel_id, target_channel, repo, status,
                    provider_name, provider_version, provider_profile_id, prompt_digest, result_digest,
                    allowed_tools_json, artifact_paths_json, provenance_json, requester, correlation_id,
                    metadata_json, transcript_json, transcript, result_summary, timeout_seconds, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    action.action_id,
                    self._provider.provider_name,
                    None,
                    None,
                    ".",
                    "queued",
                    self._provider.provider_name,
                    self._provider.provider_version,
                    provider.provider_profile_id,
                    prompt_digest,
                    None,
                    json.dumps(tools, sort_keys=True),
                    "[]",
                    json.dumps({"source_ids": source_ids, "source_provenance": source_provenance.to_dict()}, sort_keys=True, ensure_ascii=False),
                    requester,
                    correlation_id,
                    json.dumps({"redaction_applied": redacted}, sort_keys=True),
                    "[]",
                    "",
                    "",
                    1800,
                    now,
                    now,
                ),
            )
        return task_id

    def run_task(self, task_id: str) -> OrchestratorTaskResult:
        row = self._task_row(task_id)
        if row["status"] == "completed":
            return self.get_result(task_id)
        action = self.ledger.get_action(row["action_id"])
        if action.state != ApprovalState.EXECUTING.value:
            self.ledger.transition(action.action_id, ApprovalState.EXECUTING, actor=self._provider.provider_name, event_type="provider_run_started")
        safe_result, redacted = sanitize_text(f"Fake Hermes completed task {task_id} for prompt digest {row['prompt_digest']}.")
        artifact = self._write_result_artifact(task_id, action.action_id, safe_result, redacted=redacted)
        result_digest = artifact.digest
        now = utc_now()
        artifacts_json = json.dumps([artifact.path], sort_keys=True)
        with self.ledger.connect() as conn:
            conn.execute(
                """
                UPDATE agent_tasks
                SET status='completed', result_digest=?, artifact_paths_json=?, result_summary=?, updated_at=?
                WHERE task_id=?
                """,
                (result_digest, artifacts_json, "Fake Hermes completed task; see artifact path.", now, task_id),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_artifacts(artifact_id, task_id, action_id, path, digest, artifact_type, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("artifact"), task_id, action.action_id, artifact.path, artifact.digest, artifact.artifact_type, json.dumps(artifact.metadata, sort_keys=True), now),
            )
        self.ledger.transition(
            action.action_id,
            ApprovalState.DRY_RUN_COMPLETED,
            actor=self._provider.provider_name,
            event_type="provider_run_completed",
            result={"task_id": task_id, "artifact_paths": [artifact.path], "result_digest": result_digest, "correlation_id": row["correlation_id"]},
        )
        return self.get_result(task_id)

    def resume_task(self, task_id: str) -> OrchestratorTaskResult:
        return self.run_task(task_id)

    def cancel_task(self, task_id: str) -> OrchestratorTaskResult:
        row = self._task_row(task_id)
        action = self.ledger.get_action(row["action_id"])
        if action.state not in {state.value for state in (ApprovalState.CANCELLED, ApprovalState.DRY_RUN_COMPLETED, ApprovalState.COMPLETED, ApprovalState.BLOCKED, ApprovalState.FAILED)}:
            self.ledger.transition(action.action_id, ApprovalState.CANCELLED, actor=self._provider.provider_name, event_type="provider_cancelled")
        with self.ledger.connect() as conn:
            conn.execute("UPDATE agent_tasks SET status='cancelled', updated_at=? WHERE task_id=?", (utc_now(), task_id))
        return self.get_result(task_id)

    def get_result(self, task_id: str) -> OrchestratorTaskResult:
        row = self._task_row(task_id)
        return OrchestratorTaskResult(
            task_id=task_id,
            action_id=row["action_id"],
            status=row["status"],
            prompt_digest=row["prompt_digest"],
            result_digest=row["result_digest"],
            artifact_paths=tuple(json.loads(row["artifact_paths_json"] or "[]")),
            correlation_id=row["correlation_id"],
            provider=ProviderMetadata(row["provider_name"], row["provider_version"] or "unknown", row["provider_profile_id"] or ""),
        )

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        self._task_row(task_id)
        with self.ledger.connect() as conn:
            rows = conn.execute("SELECT path, digest, artifact_type, metadata_json FROM agent_artifacts WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        artifacts: list[TaskArtifact] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            artifacts.append(TaskArtifact(row["path"], row["digest"], row["artifact_type"], metadata))
        return artifacts

    def _write_result_artifact(self, task_id: str, action_id: str, result_text: str, *, redacted: bool) -> TaskArtifact:
        task_dir = self.artifact_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "action_id": action_id,
            "provider": self._provider.to_dict(),
            "result_text": result_text,
            "redaction_applied": redacted,
            "created_at": utc_now(),
        }
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        digest = stable_hash(text)
        path = task_dir / "result.json"
        path.write_text(text, encoding="utf-8")
        return TaskArtifact(str(path), digest, "result", {"redaction_applied": redacted})

    def _task_row(self, task_id: str):
        with self.ledger.connect() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise LedgerError(f"unknown task_id: {task_id}")
        return row


def _coerce_provenance(provenance: dict[str, Any] | SourceProvenance) -> tuple[list[str], SourceProvenance]:
    if isinstance(provenance, SourceProvenance):
        return [new_id("src")], provenance
    source_ids = [str(source_id) for source_id in provenance.get("source_ids", [])] or [new_id("src")]
    source_payload = provenance.get("source_provenance", provenance)
    if isinstance(source_payload, SourceProvenance):
        source_provenance = source_payload
    elif isinstance(source_payload, dict) and "source_type" in source_payload:
        source_provenance = SourceProvenance.from_dict(source_payload)
    else:
        source_provenance = SourceProvenance(SourceType.MANUAL_PROMPT, TrustLevel.UNKNOWN, (Sink.MEMORY, Sink.DRAFT, Sink.PRIVATE_REPLY))
    return source_ids, source_provenance


def _validate_allowed_tools(allowed_tools: list[str]) -> None:
    for tool in allowed_tools:
        lowered = tool.lower()
        if lowered.startswith("ledger_gateway."):
            continue
        if lowered.startswith(DIRECT_WRITE_TOOL_PREFIXES) and any(word in lowered for word in DIRECT_WRITE_WORDS):
            raise LedgerError(f"direct external write tool is not allowed without ledger gateway: {tool}")


__all__ = [
    "BrainProvider",
    "FakeHermesGateway",
    "OrchestratorGateway",
    "OrchestratorTaskResult",
    "ProviderMetadata",
    "TaskArtifact",
]


class ProviderSecurityError(ValueError):
    pass


class _FakeLocalProvider:
    provider_name = "local_cli"

    def __init__(self, ledger: ActionLedger, *, profile_id: str = "local-profile") -> None:
        try:
            reject_secret_value("profile_id", profile_id)
        except ValueError as exc:
            raise ProviderSecurityError(str(exc)) from exc
        self.ledger = ledger
        self.profile_id = profile_id

    def provider_metadata(self) -> dict[str, Any]:
        return {"provider_name": self.provider_name, "profile_id": self.profile_id, "stores_raw_secret": False}


class FakeCodexCliProvider(_FakeLocalProvider):
    provider_name = "codex_cli"


class FakeClaudeCliProvider(_FakeLocalProvider):
    provider_name = "claude_cli"
