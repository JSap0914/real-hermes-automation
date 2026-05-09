from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import AdapterConfig, AppConfig
from .ledger import ActionLedger, LedgerError
from .memory import redact_secrets
from .models import ActionType, ApprovalState, Platform, Sink, SourceProvenance, SourceType, TrustLevel, make_action
from .policy import PolicyGate


class MessagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class MessageEvent:
    surface: str
    user_id: str
    channel_id: str
    text: str
    message_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageReceipt:
    ok: bool
    action_id: str | None = None
    rejected: bool = False
    reason: str = ""
    dry_run: bool = True
    payload: dict | None = None

    @property
    def accepted(self) -> bool:
        return self.ok and not self.rejected

    @property
    def adapter_called(self) -> bool:
        return bool((self.payload or {}).get("network_write"))


class MessagingProvider(Protocol):
    def ingest_event(self, event: MessageEvent) -> MessageReceipt: ...

    def preview_reply(self, *, action_id: str, text: str) -> MessageReceipt: ...


def _platform_for_surface(surface: str) -> Platform:
    lowered = surface.lower()
    if lowered == "discord":
        return Platform.DISCORD
    if lowered == "telegram":
        return Platform.TELEGRAM
    return Platform.LOCAL


def _source_for_surface(surface: str) -> SourceType:
    if surface.lower() == "discord":
        return SourceType.PRIVATE_DISCORD
    if surface.lower() in {"telegram", "dm", "direct"}:
        return SourceType.PRIVATE_DM
    return SourceType.MANUAL_PROMPT


def _reject_secret_profile(profile_id: str) -> None:
    redacted, changed = redact_secrets(profile_id)
    if changed or redacted != profile_id or any(marker in profile_id.lower() for marker in ("token", "cookie", "password", "bearer", "oauth")):
        raise MessagingError("messaging provider profile IDs must not contain secrets")


class LedgerMessagingGateway:
    """Common safe messaging/control-plane wrapper.

    Inbound events become ``control_command`` ledger actions after user/channel
    allowlist checks. Outbound replies are previews by default; live private
    replies remain disabled unless an action is already approved and the adapter
    is explicitly live-ready.
    """

    provider_name = "ledger_messaging"

    def __init__(
        self,
        ledger: ActionLedger,
        config: AppConfig | None = None,
        *,
        provider_profile_id: str = "messaging-default",
        adapter_name: str = "discord",
    ) -> None:
        _reject_secret_profile(provider_profile_id)
        self.ledger = ledger
        self.config = config or AppConfig()
        self.provider_profile_id = provider_profile_id
        self.adapter_name = adapter_name
        self.policy = PolicyGate(self.config.policy, allowed_discord_channels=frozenset(self.config.adapter("discord").allowed_channels))

    @property
    def adapter_config(self) -> AdapterConfig:
        return self.config.adapter(self.adapter_name)

    def ingest_event(self, event: MessageEvent) -> MessageReceipt:
        if not self._is_allowlisted(event):
            return MessageReceipt(False, rejected=True, reason="unauthorized messaging control event")
        safe_text, redacted = redact_secrets(event.text)
        platform = _platform_for_surface(event.surface)
        provenance = SourceProvenance(_source_for_surface(event.surface), TrustLevel.UNKNOWN, (Sink.PRIVATE_REPLY, Sink.MEMORY))
        env = make_action(
            action_type=ActionType.CONTROL_COMMAND,
            platform=platform,
            text=safe_text,
            source_ids=[event.message_id or f"{event.surface}:{event.channel_id}:{event.user_id}"],
            provenance=provenance,
            account_or_channel_id=event.channel_id,
            created_by=event.user_id,
            metadata={
                "provider": self.provider_name,
                "provider_profile_id": self.provider_profile_id,
                "surface": event.surface,
                "message_id": event.message_id,
                "redaction_applied": redacted,
            },
        )
        env = self.policy.apply(env)
        self.ledger.create_action(env, actor=event.user_id)
        return MessageReceipt(True, action_id=env.action_id, dry_run=True, payload={"state": env.state, "network_write": False})

    def preview_reply(self, *, action_id: str, text: str) -> MessageReceipt:
        env = self.ledger.get_action(action_id)
        safe_text, redacted = redact_secrets(text)
        payload = {
            "ok": True,
            "dry_run": True,
            "network_write": False,
            "adapter": self.provider_name,
            "action_id": action_id,
            "channel_id": env.target.account_or_channel_id,
            "text": safe_text,
            "redaction_applied": redacted,
        }
        self.ledger.record_adapter_result(env, self.provider_name, payload)
        return MessageReceipt(True, action_id=action_id, dry_run=True, payload=payload)

    def send_private_reply(self, *, action_id: str, text: str) -> MessageReceipt:
        env = self.ledger.get_action(action_id)
        state = self.ledger.state(action_id)
        if not self.adapter_config.ready_for_live() or state not in {ApprovalState.APPROVED_FOR_LIVE.value, ApprovalState.EXECUTING.value}:
            raise MessagingError("live messaging replies require readiness and approved ledger action")
        safe_text, redacted = redact_secrets(text)
        payload = {
            "ok": True,
            "dry_run": False,
            "network_write": True,
            "adapter": self.provider_name,
            "action_id": action_id,
            "channel_id": env.target.account_or_channel_id,
            "text": safe_text,
            "redaction_applied": redacted,
        }
        self.ledger.record_adapter_result(env, self.provider_name, payload)
        return MessageReceipt(True, action_id=action_id, dry_run=False, payload=payload)

    def _is_allowlisted(self, event: MessageEvent) -> bool:
        cfg = self.config.adapter(event.surface.lower())
        allowed_users = set(cfg.allowed_users or cfg.owner_ids or self.config.adapter("discord").allowed_users or self.config.adapter("telegram").owner_ids)
        allowed_channels = set(cfg.allowed_channels or self.config.adapter("discord").allowed_channels)
        return event.user_id in allowed_users and (not allowed_channels or event.channel_id in allowed_channels)


class HermesMessagingGateway(LedgerMessagingGateway):
    provider_name = "hermes_messaging"


class AgentMessengerAdapter(LedgerMessagingGateway):
    provider_name = "agent_messenger"

    def __init__(
        self,
        ledger: ActionLedger,
        config: AppConfig | None = None,
        *,
        provider_profile_id: str = "agent-messenger-bot",
        adapter_name: str = "discord",
        enabled: bool = False,
        allow_desktop_token_extraction: bool = False,
    ) -> None:
        if allow_desktop_token_extraction:
            raise MessagingError("desktop-token extraction is experimental and disabled")
        super().__init__(ledger, config, provider_profile_id=provider_profile_id, adapter_name=adapter_name)
        self.enabled = enabled

    def ingest_event(self, event: MessageEvent) -> MessageReceipt:
        if not self.enabled:
            return MessageReceipt(False, rejected=True, reason="agent-messenger adapter is disabled by default")
        return super().ingest_event(event)

    def send_private_reply(self, *, action_id: str, text: str) -> MessageReceipt:
        if not self.enabled:
            raise MessagingError("agent-messenger adapter is disabled by default")
        try:
            return super().send_private_reply(action_id=action_id, text=text)
        except LedgerError as exc:
            raise MessagingError(str(exc)) from exc


# Compatibility surface from the initial Hermes-first lane.
InboundMessage = MessageEvent


class MessagingControlPlane:
    def __init__(self, ledger: ActionLedger, *, allowed_users: set[str] | None = None, allowed_channels: set[str] | None = None) -> None:
        cfg = AppConfig()
        cfg.adapters["telegram"].allowed_users = sorted(allowed_users or set())
        cfg.adapters["telegram"].allowed_channels = sorted(allowed_channels or set())
        cfg.adapters["discord"].allowed_users = sorted(allowed_users or set())
        cfg.adapters["discord"].allowed_channels = sorted(allowed_channels or set())
        self.gateway = HermesMessagingGateway(ledger, cfg, adapter_name="telegram")

    def handle_inbound(self, event: MessageEvent) -> MessageReceipt:
        self.gateway.adapter_name = event.surface.lower()
        return self.gateway.ingest_event(event)
