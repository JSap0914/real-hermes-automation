from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from .ledger import ActionLedger, LedgerError
from .memory import redact_secrets
from .models import ActionType, ApprovalState, Platform, Sink, SourceProvenance, SourceType, Target, TrustLevel, make_action, new_id, sha256_text, utc_now
from .policy import PolicyGate


class BrainProviderError(RuntimeError):
    pass


SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|(?:oauth|bearer|api[_-]?key|token|secret|password)\s*[:=]\s*\S+|BEGIN PRIVATE KEY)",
    re.I,
)
SAFE_TOOL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProcessRunner(Protocol):
    def run(self, argv: Sequence[str], *, input_text: str, timeout_seconds: int, cwd: str | Path | None = None) -> ProcessResult: ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], *, input_text: str, timeout_seconds: int, cwd: str | Path | None = None) -> ProcessResult:
        completed = subprocess.run(
            list(argv),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
        )
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass
class FakeProcessRunner:
    responses: list[ProcessResult] = field(default_factory=lambda: [ProcessResult(0, "ok", "")])
    invocations: list[dict[str, Any]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, input_text: str, timeout_seconds: int, cwd: str | Path | None = None) -> ProcessResult:
        self.invocations.append({"argv": list(argv), "input_text": input_text, "timeout_seconds": timeout_seconds, "cwd": str(cwd) if cwd is not None else None})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


@dataclass(frozen=True)
class BrainTask:
    task_id: str
    action_id: str
    provider_name: str
    provider_version: str
    status: str
    prompt_digest: str
    result_digest: str | None
    artifact_paths: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    credential_profile_id: str | None
    correlation_id: str


@dataclass(frozen=True)
class BrainResult:
    task_id: str
    status: str
    artifact_paths: tuple[str, ...]
    result_digest: str | None
    metadata: dict[str, Any]


class BrainProvider(Protocol):
    def create_task(self, prompt: str, allowed_tools: Sequence[str], provenance: dict[str, Any] | SourceProvenance | None = None, requester: str = "owner") -> BrainTask: ...
    def run_task(self, task_id: str) -> BrainResult: ...
    def resume_task(self, task_id: str) -> BrainResult: ...
    def cancel_task(self, task_id: str) -> BrainResult: ...
    def get_result(self, task_id: str) -> BrainResult: ...
    def list_artifacts(self, task_id: str) -> list[str]: ...
    def provider_metadata(self) -> dict[str, Any]: ...


def _assert_no_secret(label: str, value: str | None) -> None:
    if value and SECRET_VALUE_RE.search(value):
        raise BrainProviderError(f"{label} must be a profile id or artifact path, not an OAuth/API secret")


def _safe_tools(allowed_tools: Sequence[str]) -> tuple[str, ...]:
    tools: list[str] = []
    for tool in allowed_tools:
        if not SAFE_TOOL_RE.fullmatch(str(tool)):
            raise BrainProviderError(f"unsafe tool name: {tool!r}")
        tools.append(str(tool))
    return tuple(dict.fromkeys(tools))


def _profile(profile_id: str | None) -> str | None:
    if profile_id is None:
        return None
    _assert_no_secret("credential_profile_id", profile_id)
    if not SAFE_PROFILE_RE.fullmatch(profile_id):
        raise BrainProviderError("credential_profile_id contains unsupported characters")
    return profile_id


class LocalCliProvider:
    """Ledger-gated wrapper for local account-backed CLIs such as Codex or Claude.

    Prompts are passed via stdin and results are persisted as redacted artifact files. SQLite receives
    provider/profile identifiers, digests, and artifact paths only; it never stores raw OAuth/API tokens.
    """

    provider_name = "local_cli"
    provider_version = "local-cli-v1"

    def __init__(
        self,
        ledger: ActionLedger,
        *,
        command: Sequence[str],
        profile_id: str | None = None,
        runner: ProcessRunner | None = None,
        artifact_dir: str | Path = ".local/social_agent/artifacts/brain",
        timeout_seconds: int = 1800,
        policy: PolicyGate | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        if not command:
            raise BrainProviderError("command is required")
        self.ledger = ledger
        self.command = tuple(str(part) for part in command)
        self.profile_id = _profile(profile_id)
        self.runner = runner or SubprocessRunner()
        self.artifact_dir = Path(artifact_dir)
        self.timeout_seconds = timeout_seconds
        self.policy = policy or PolicyGate()
        self.cwd = cwd

    def create_task(self, prompt: str, allowed_tools: Sequence[str], provenance: dict[str, Any] | SourceProvenance | None = None, requester: str = "owner") -> BrainTask:
        tools = _safe_tools(allowed_tools)
        safe_prompt, redacted = redact_secrets(prompt)
        prompt_digest = sha256_text(safe_prompt)
        source_provenance = self._coerce_provenance(provenance)
        task_id = new_id("braintask")
        correlation_id = task_id
        metadata = {
            "provider": self.provider_metadata(),
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "credential_profile_id": self.profile_id,
            "allowed_tools": list(tools),
            "prompt_digest": prompt_digest,
            "result_digest": None,
            "artifact_paths": [],
            "correlation_id": correlation_id,
            "redaction_applied": redacted,
        }
        action_type = ActionType.AGENT_TOOL_CALL if tools else ActionType.AGENT_DRAFT
        target = Target(Platform.LOCAL, self.profile_id or self.provider_name, agent_name=self.provider_name)
        action = make_action(
            action_type=action_type,
            target=target,
            text=safe_prompt,
            source_ids=[new_id("prompt")],
            provenance=source_provenance,
            policy=self.policy.decide(action_type=action_type, target=target, text=safe_prompt, provenance=source_provenance),
            created_by=requester,
            mode="dry_run",
            metadata=metadata,
        )
        action_id = self.ledger.create_action(action, actor=requester)
        self._insert_task(
            BrainTask(task_id, action_id, self.provider_name, self.provider_version, "queued", prompt_digest, None, (), tools, self.profile_id, correlation_id)
        )
        return self._load_task(task_id)

    def run_task(self, task_id: str) -> BrainResult:
        task = self._load_task(task_id)
        try:
            action = self.ledger.get_action(task.action_id)
        except LedgerError as exc:
            raise BrainProviderError("brain task has no ledger action") from exc
        self._update_task(task_id, status="running")
        if self.ledger.state(task.action_id) != ApprovalState.EXECUTING.value:
            self.ledger.transition(task.action_id, ApprovalState.EXECUTING, actor=self.provider_name, event_type="brain_started")
        argv = self._build_argv(task, action.content.text)
        try:
            result = self.runner.run(argv, input_text=action.content.text, timeout_seconds=self.timeout_seconds, cwd=self.cwd)
        except subprocess.TimeoutExpired as exc:
            return self._block_task(task, f"{self.provider_name} timed out after {self.timeout_seconds}s", {"timeout": str(exc)})
        stdout, out_redacted = redact_secrets(result.stdout)
        stderr, err_redacted = redact_secrets(result.stderr)
        artifact_path = self._write_artifact(task_id, {"stdout": stdout, "stderr": stderr, "returncode": result.returncode})
        digest = sha256_text(json.dumps({"stdout": stdout, "stderr": stderr, "returncode": result.returncode}, sort_keys=True, ensure_ascii=False))
        if result.returncode != 0:
            return self._block_task(task, f"{self.provider_name} exited {result.returncode}", {"artifact_path": artifact_path, "result_digest": digest})
        metadata = {"artifact_paths": [artifact_path], "result_digest": digest, "returncode": result.returncode, "redaction_applied": out_redacted or err_redacted}
        self._update_task(task_id, status="completed", result_digest=digest, artifact_paths=[artifact_path], error=None)
        self._patch_action_metadata(task.action_id, metadata)
        self.ledger.transition(task.action_id, ApprovalState.DRY_RUN_COMPLETED, actor=self.provider_name, result=metadata, event_type="brain_completed")
        return self.get_result(task_id)

    def resume_task(self, task_id: str) -> BrainResult:
        task = self._load_task(task_id)
        if task.status == "completed":
            return self.get_result(task_id)
        return self.run_task(task_id)

    def cancel_task(self, task_id: str) -> BrainResult:
        task = self._load_task(task_id)
        self._update_task(task_id, status="cancelled")
        self.ledger.transition(task.action_id, ApprovalState.CANCELLED, actor=self.provider_name, result={"reason": "cancelled"}, event_type="brain_cancelled")
        return self.get_result(task_id)

    def get_result(self, task_id: str) -> BrainResult:
        task = self._load_task(task_id)
        return BrainResult(task.task_id, task.status, task.artifact_paths, task.result_digest, {"provider_name": task.provider_name, "correlation_id": task.correlation_id})

    def list_artifacts(self, task_id: str) -> list[str]:
        return list(self._load_task(task_id).artifact_paths)

    def provider_metadata(self) -> dict[str, Any]:
        return {
            "name": self.provider_name,
            "version": self.provider_version,
            "mode": "local_cli",
            "command": self.command[0],
            "credential_profile_id": self.profile_id,
        }

    def _build_argv(self, task: BrainTask, prompt: str) -> list[str]:
        argv = list(self.command)
        if self.profile_id:
            argv.extend(["--profile", self.profile_id])
        return argv

    def _coerce_provenance(self, provenance: dict[str, Any] | SourceProvenance | None) -> SourceProvenance:
        if isinstance(provenance, SourceProvenance):
            return provenance
        if isinstance(provenance, dict):
            return SourceProvenance.from_dict(provenance)
        return SourceProvenance(SourceType.MANUAL_PROMPT, TrustLevel.UNKNOWN, (Sink.PRIVATE_REPLY, Sink.DRAFT, Sink.MEMORY))

    def _write_artifact(self, task_id: str, payload: dict[str, Any]) -> str:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{task_id}-result.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _insert_task(self, task: BrainTask) -> None:
        with self.ledger.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_provider_tasks(task_id, action_id, provider_name, provider_version, status, prompt_digest, result_digest, artifact_paths_json, allowed_tools_json, credential_profile_id, correlation_id, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.action_id,
                    task.provider_name,
                    task.provider_version,
                    task.status,
                    task.prompt_digest,
                    task.result_digest,
                    json.dumps(list(task.artifact_paths), sort_keys=True),
                    json.dumps(list(task.allowed_tools), sort_keys=True),
                    task.credential_profile_id,
                    task.correlation_id,
                    None,
                    utc_now(),
                    utc_now(),
                ),
            )

    def _load_task(self, task_id: str) -> BrainTask:
        with self.ledger.connect() as conn:
            row = conn.execute("SELECT * FROM agent_provider_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise BrainProviderError(f"unknown brain task: {task_id}")
        return BrainTask(
            row["task_id"],
            row["action_id"],
            row["provider_name"],
            row["provider_version"],
            row["status"],
            row["prompt_digest"],
            row["result_digest"],
            tuple(json.loads(row["artifact_paths_json"] or "[]")),
            tuple(json.loads(row["allowed_tools_json"] or "[]")),
            row["credential_profile_id"],
            row["correlation_id"],
        )

    def _update_task(self, task_id: str, *, status: str, result_digest: str | None = None, artifact_paths: Sequence[str] | None = None, error: str | None = None) -> None:
        updates = ["status=?", "updated_at=?"]
        values: list[Any] = [status, utc_now()]
        if result_digest is not None:
            updates.append("result_digest=?")
            values.append(result_digest)
        if artifact_paths is not None:
            updates.append("artifact_paths_json=?")
            values.append(json.dumps(list(artifact_paths), sort_keys=True))
        if error is not None:
            updates.append("error=?")
            values.append(error)
        elif status in {"completed", "cancelled"}:
            updates.append("error=NULL")
        values.append(task_id)
        with self.ledger.connect() as conn:
            conn.execute(f"UPDATE agent_provider_tasks SET {', '.join(updates)} WHERE task_id=?", values)

    def _patch_action_metadata(self, action_id: str, updates: dict[str, Any]) -> None:
        with self.ledger.connect() as conn:
            row = conn.execute("SELECT envelope_json FROM actions WHERE action_id=?", (action_id,)).fetchone()
            if not row:
                raise BrainProviderError("brain task has no ledger action")
            data = json.loads(row["envelope_json"])
            metadata = dict(data.get("metadata", {}))
            metadata.update(updates)
            data["metadata"] = metadata
            conn.execute("UPDATE actions SET envelope_json=?, updated_at=? WHERE action_id=?", (json.dumps(data, sort_keys=True, ensure_ascii=False), utc_now(), action_id))

    def _block_task(self, task: BrainTask, reason: str, metadata: dict[str, Any] | None = None) -> BrainResult:
        metadata = metadata or {}
        self._update_task(task.task_id, status="blocked", result_digest=metadata.get("result_digest"), artifact_paths=[metadata["artifact_path"]] if metadata.get("artifact_path") else None, error=reason)
        self._patch_action_metadata(task.action_id, {**metadata, "blocked_reason": reason})
        self.ledger.transition(task.action_id, ApprovalState.BLOCKED, actor=self.provider_name, result={"error": reason, **metadata}, event_type="brain_blocked")
        return self.get_result(task.task_id)


class HermesGateway(LocalCliProvider):
    provider_name = "hermes"
    provider_version = "hermes-cli-wrapper-v1"

    def __init__(self, ledger: ActionLedger, *, command: Sequence[str] = ("hermes", "run"), **kwargs: Any) -> None:
        super().__init__(ledger, command=command, **kwargs)

    def _build_argv(self, task: BrainTask, prompt: str) -> list[str]:
        argv = list(self.command)
        argv.extend(["--task-id", task.task_id])
        for tool in task.allowed_tools:
            argv.extend(["--allow-tool", tool])
        if self.profile_id:
            argv.extend(["--profile", self.profile_id])
        return argv

    def provider_metadata(self) -> dict[str, Any]:
        data = super().provider_metadata()
        data.update({"gateway": "hermes", "hermes_integration": "cli"})
        return data


class CodexCliProvider(LocalCliProvider):
    provider_name = "codex_cli"
    provider_version = "codex-cli-wrapper-v1"

    def __init__(self, ledger: ActionLedger, *, command: Sequence[str] = ("codex", "exec"), **kwargs: Any) -> None:
        super().__init__(ledger, command=command, **kwargs)


class ClaudeCliProvider(LocalCliProvider):
    provider_name = "claude_cli"
    provider_version = "claude-cli-wrapper-v1"

    def __init__(self, ledger: ActionLedger, *, command: Sequence[str] = ("claude", "--print"), **kwargs: Any) -> None:
        super().__init__(ledger, command=command, **kwargs)


class FakeHermesGateway(HermesGateway):
    def __init__(self, ledger: ActionLedger, *, response: str = "fake hermes result", **kwargs: Any) -> None:
        self.fake_runner = FakeProcessRunner([ProcessResult(0, response, "")])
        super().__init__(ledger, runner=self.fake_runner, command=("fake-hermes", "run"), **kwargs)
