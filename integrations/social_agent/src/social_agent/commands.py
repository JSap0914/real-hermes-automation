from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .ledger import ActionLedger
from .models import ActionType, ApprovalState, Platform, Sink, SourceProvenance, SourceType, TrustLevel, make_action, new_id
from .policy import PolicyGate, SHELL_PATTERNS


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandContext:
    surface: str
    user_id: str
    channel_id: str = "control"


@dataclass(frozen=True)
class CommandResponse:
    rejected: bool = False
    reason: str = ""
    action_id: str | None = None
    action_type: str | None = None
    approval_state: ApprovalState | str | None = None
    adapter_called: bool = False
    paused: bool = False


class CommandPlane:
    """Allowlisted Telegram/Discord command plane that only creates ledger actions."""

    def __init__(self, ledger: ActionLedger | None = None, config: AppConfig | None = None, *, db_path: str | Path | None = None, allowed_users: set[str] | None = None, allowed_channels: set[str] | None = None) -> None:
        self.config = config or AppConfig(database_path=db_path)
        self.ledger = ledger or ActionLedger(db_path or self.config.runtime.database_path)
        self.allowed_users = set(allowed_users or self.config.adapter("telegram").owner_ids or self.config.adapter("discord").allowed_users or {"owner"})
        self.allowed_channels = set(allowed_channels or self.config.adapter("discord").allowed_channels or {"control", "dev-agent-test"})
        self.paused = False
        self.policy = PolicyGate(self.config.policy, allowed_discord_channels=frozenset(self.allowed_channels))

    def handle(self, ctx: CommandContext, text: str) -> dict[str, Any]:
        if ctx.user_id not in self.allowed_users:
            return {"ok": False, "error": "unauthorized"}
        if text.startswith("/status"):
            return {"ok": True, "paused": self.paused, "queue": self.ledger.status()}
        if text.startswith("/pause"):
            self.paused = True
            self.config.runtime.paused = True
            return {"ok": True, "paused": True}
        if text.startswith("/resume"):
            self.paused = False
            self.config.runtime.paused = False
            return {"ok": True, "paused": False}
        if text.startswith("/post-now"):
            body = text.split(" ", 1)[1] if " " in text else "manual post"
            env = self._post_proposal(body, ctx.user_id)
            self.ledger.create_action(env, actor=ctx.user_id)
            return {"ok": True, "action_id": env.action_id, "state": env.state}
        return {"ok": False, "error": "unknown_command"}

    def handle_telegram_command(self, *, user_id: str, chat_id: str, text: str) -> CommandResponse:
        if user_id not in self.allowed_users or chat_id not in self.allowed_channels:
            return CommandResponse(rejected=True, reason="unauthorized telegram command")
        command = text.split(" ", 1)[0]
        if command == "/pause":
            self.paused = True
            self.config.runtime.paused = True
            return CommandResponse(paused=True)
        if command == "/resume":
            self.paused = False
            self.config.runtime.paused = False
            return CommandResponse(paused=False)
        if command in {"/post-now", "/approve"}:
            body = text.split(" ", 1)[1] if " " in text else "manual post"
            env = self._post_proposal(body, user_id)
            self.ledger.create_action(env, actor=user_id)
            return CommandResponse(action_id=env.action_id, action_type=env.action_type.value, approval_state=env.approval.state, adapter_called=False)
        if command in {"/status", "/why", "/memory"}:
            return CommandResponse()
        return CommandResponse(rejected=True, reason="unknown telegram command")

    def handle_discord_slash(self, *, user_id: str, channel_id: str, command: str, args: str) -> CommandResponse:
        if user_id not in self.allowed_users or channel_id not in self.allowed_channels:
            return CommandResponse(rejected=True, reason="unauthorized discord command")
        if command != "/agent":
            return CommandResponse(rejected=True, reason="unknown discord command")
        try:
            action_id = DiscordAgentManager(self.ledger, self.config).create_task(args, user_id=user_id, channel_id=channel_id)
        except CommandError as exc:
            return CommandResponse(rejected=True, reason=str(exc))
        env = self.ledger.get_action(action_id)
        return CommandResponse(action_id=env.action_id, action_type=env.action_type.value, approval_state=env.approval.state, adapter_called=False)

    def executor_may_consume_live_actions(self) -> bool:
        return not self.paused and not self.config.runtime.paused

    def _post_proposal(self, body: str, user_id: str):
        provenance = SourceProvenance(SourceType.MANUAL_PROMPT, TrustLevel.UNKNOWN, (Sink.MEMORY, Sink.DRAFT, Sink.PUBLIC_POST))
        env = make_action(action_type=ActionType.PUBLISH_POST, platform=Platform.X, text=body, source_ids=[new_id("manual")], provenance=provenance, account_or_channel_id="local-x", created_by=user_id)
        return PolicyGate(self.config.policy).apply(env)


class DiscordAgentManager:
    def __init__(self, ledger: ActionLedger, config: AppConfig) -> None:
        self.ledger = ledger
        self.config = config

    def create_task(self, task_text: str, *, user_id: str, channel_id: str) -> str:
        if user_id not in set(self.config.adapter("discord").allowed_users or {"owner"}):
            raise CommandError("unauthorized user")
        if channel_id not in set(self.config.adapter("discord").allowed_channels or {"dev-agent-test"}):
            raise CommandError("unauthorized channel")
        lowered = task_text.lower()
        if SHELL_PATTERNS.search(task_text) or "&&" in task_text or "../" in task_text or "..\\" in task_text:
            raise CommandError("unsafe shell or path traversal rejected")
        provenance = SourceProvenance(SourceType.MANUAL_PROMPT, TrustLevel.UNKNOWN, (Sink.MEMORY, Sink.PRIVATE_REPLY))
        env = make_action(
            action_type=ActionType.DELEGATE_AGENT_TASK,
            platform=Platform.DISCORD,
            text=task_text,
            source_ids=[new_id("manual")],
            provenance=provenance,
            account_or_channel_id=channel_id,
            created_by=user_id,
            target_extra={"agent_name": "codex-local"},
        )
        env = PolicyGate(self.config.policy, allowed_discord_channels=frozenset(self.config.adapter("discord").allowed_channels)).apply(env)
        if env.state == "policy_rejected":
            raise CommandError("unsafe delegation rejected")
        self.ledger.create_action(env, actor=user_id)
        return env.action_id
