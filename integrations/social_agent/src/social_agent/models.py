from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Iterable


class ValidationError(ValueError):
    """Raised when a typed action envelope violates safety invariants."""


class AttrDict(dict):
    """Dictionary with attribute access for test/backward compatibility."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value):
        self[name] = value


def _attrdict(value):
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, dict):
        return AttrDict({k: _attrdict(v) for k, v in value.items()})
    return value


class Platform(StrEnum):
    X = "x"
    THREADS = "threads"
    INSTAGRAM = "instagram"
    DISCORD = "discord"
    TELEGRAM = "telegram"
    LOCAL = "local"


class ActionType(StrEnum):
    DRAFT_POST = "draft_post"
    PUBLISH_POST = "publish_post"
    REPLY_TO_POST = "reply_to_post"
    SEND_DISCORD_MESSAGE = "send_discord_message"
    SEND_TELEGRAM_MESSAGE = "send_telegram_message"
    DELEGATE_AGENT_TASK = "delegate_agent_task"
    AGENT_RESEARCH = "agent_research"
    AGENT_DRAFT = "agent_draft"
    AGENT_TOOL_CALL = "agent_tool_call"
    AGENT_SOCIAL_INTENT = "agent_social_intent"
    AGENT_MEMORY_WRITE = "agent_memory_write"
    CONTROL_COMMAND = "control_command"
    CANCEL_TASK = "cancel_task"
    KILL_SWITCH = "kill_switch"
    MEMORY_WRITE = "memory_write"


class SourceType(StrEnum):
    PUBLIC_WEB = "public_web"
    PUBLIC_SOCIAL = "public_social"
    PRIVATE_DISCORD = "private_discord"
    PRIVATE_DM = "private_dm"
    LOCAL_NOTE = "local_note"
    MANUAL_PROMPT = "manual_prompt"


class TrustLevel(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class Sink(StrEnum):
    MEMORY = "memory"
    DRAFT = "draft"
    PRIVATE_REPLY = "private_reply"
    PUBLIC_POST = "public_post"


class PolicyClassification(StrEnum):
    ALLOWED = "allowed"
    NEEDS_HUMAN_APPROVAL = "needs_human_approval"
    BLOCKED = "blocked"


class ApprovalState(StrEnum):
    PROPOSED = "proposed"
    POLICY_REJECTED = "policy_rejected"
    NEEDS_HUMAN_APPROVAL = "needs_human_approval"
    DRY_RUN_READY = "dry_run_ready"
    DRY_RUN_COMPLETED = "dry_run_completed"
    APPROVED_FOR_LIVE = "approved_for_live"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


PLATFORMS = {p.value for p in Platform}
ACTION_TYPES = {a.value for a in ActionType}
APPROVAL_STATES = {s.value for s in ApprovalState}
LIVE_ELIGIBLE_STATES = {ApprovalState.APPROVED_FOR_LIVE.value, ApprovalState.SCHEDULED.value}
TERMINAL_STATES = {s.value for s in (ApprovalState.COMPLETED, ApprovalState.FAILED, ApprovalState.BLOCKED, ApprovalState.CANCELLED, ApprovalState.EXPIRED, ApprovalState.POLICY_REJECTED)}
PUBLIC_ACTIONS = {ActionType.PUBLISH_POST.value, ActionType.REPLY_TO_POST.value}
PROVENANCE_TYPES = {s.value for s in SourceType}
TRUST_LEVELS = {t.value for t in TrustLevel}
SINKS = {s.value for s in Sink}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return utc_now()


def lease_expiry(minutes: int = 5) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return stable_hash(text)


def new_id(prefix: str = "act") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def idempotency_for(parts: Iterable[str]) -> str:
    return stable_hash("|".join(str(p) for p in parts))


@dataclass
class SourceProvenance:
    source_type: SourceType | str
    trust_level: TrustLevel | str = TrustLevel.UNKNOWN
    allowed_sinks: Iterable[Sink | str] = field(default_factory=lambda: (Sink.MEMORY, Sink.DRAFT))
    source_url: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        self.source_type = SourceType(self.source_type)
        self.trust_level = TrustLevel(self.trust_level)
        self.allowed_sinks = [Sink(s) for s in self.allowed_sinks]
        self.validate()

    def validate(self) -> None:
        if self.source_type.value not in PROVENANCE_TYPES:
            raise ValidationError(f"invalid source_type: {self.source_type}")
        if self.trust_level.value not in TRUST_LEVELS:
            raise ValidationError(f"invalid trust_level: {self.trust_level}")

    def allows(self, sink: Sink | str) -> bool:
        return Sink(sink) in self.allowed_sinks

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "trust_level": self.trust_level.value,
            "allowed_sinks": [s.value for s in self.allowed_sinks],
            "source_url": self.source_url,
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceProvenance":
        return cls(data["source_type"], data.get("trust_level", "unknown"), data.get("allowed_sinks", []), data.get("source_url"), data.get("title"))


@dataclass
class Target:
    platform: Platform | str
    account_or_channel_id: str = ""
    post_id: str | None = None
    reply_to_post_id: str | None = None
    agent_name: str | None = None

    def __post_init__(self) -> None:
        self.platform = Platform(self.platform)
        if self.post_id and not self.reply_to_post_id:
            self.reply_to_post_id = self.post_id
        if self.reply_to_post_id and not self.post_id:
            self.post_id = self.reply_to_post_id

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def values(self):
        return self.to_dict().values()

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "account_or_channel_id": self.account_or_channel_id,
            "post_id": self.post_id,
            "reply_to_post_id": self.reply_to_post_id,
            "agent_name": self.agent_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Target":
        return cls(data["platform"], data.get("account_or_channel_id", ""), data.get("post_id"), data.get("reply_to_post_id"), data.get("agent_name"))


class Content(dict):
    def __init__(self, text: str, content_hash: str | None = None):
        super().__init__(text=text, content_hash=content_hash or stable_hash(text))

    @property
    def text(self) -> str:
        return self["text"]

    @property
    def content_hash(self) -> str:
        return self["content_hash"]

    def to_dict(self) -> dict[str, str]:
        return dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Content":
        if "text" not in data or "content_hash" not in data:
            raise ValidationError("content.text and content.content_hash required")
        return cls(str(data["text"]), str(data["content_hash"]))


@dataclass
class PolicyDecision:
    classification: PolicyClassification | str
    reasons: Iterable[str]
    platform_policy_version: str = "2026-04-28"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.classification = PolicyClassification(self.classification)
        self.reasons = tuple(self.reasons)
        self.metadata = dict(self.metadata)

    @property
    def risk_metadata(self) -> dict[str, Any]:
        return self.metadata

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def values(self):
        return self.to_dict().values()

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "reasons": list(self.reasons),
            "platform_policy_version": self.platform_policy_version,
            "metadata": self.metadata,
            "risk_metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyDecision":
        return cls(data["classification"], data.get("reasons", []), data.get("platform_policy_version", "unknown"), data.get("metadata", data.get("risk_metadata", {})))


class ApprovalDict(dict):
    @property
    def state(self) -> ApprovalState:
        return ApprovalState(self["state"])

    @property
    def approver(self) -> str | None:
        return self.get("approver")

    @property
    def approved_at(self) -> str | None:
        return self.get("approved_at")


class ExecutionDict(dict):
    @property
    def mode(self) -> str:
        return str(self.get("mode", "dry_run"))

    @property
    def idempotency_key(self) -> str:
        return str(self["idempotency_key"])

    @property
    def lease_owner(self) -> str | None:
        return self.get("lease_owner")

    @property
    def lease_expires_at(self) -> str | None:
        return self.get("lease_expires_at")

    @property
    def retry_count(self) -> int:
        return int(self.get("retry_count", 0))


class AuditDict(dict):
    pass


@dataclass
class ActionEnvelope:
    action_type: ActionType | str
    target: Target | dict[str, Any]
    content: Content | dict[str, Any]
    source_ids: list[str]
    source_provenance: SourceProvenance | dict[str, Any]
    created_by: str = "agent"
    action_id: str = field(default_factory=lambda: new_id("act"))
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now)
    policy: PolicyDecision | dict[str, Any] = field(default_factory=lambda: PolicyDecision("needs_human_approval", [], "2026-04-28"))
    approval: ApprovalDict | dict[str, Any] = field(default_factory=lambda: {"state": "proposed", "approver": None, "approved_at": None})
    execution: ExecutionDict | dict[str, Any] = field(default_factory=dict)
    audit: AuditDict | dict[str, Any] = field(default_factory=lambda: {"redaction_applied": False, "preview_rendered": False, "result": None})
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action_type = ActionType(self.action_type)
        self.target = self.target if isinstance(self.target, Target) else Target.from_dict(self.target)
        self.content = self.content if isinstance(self.content, Content) else Content.from_dict(self.content)
        self.source_provenance = self.source_provenance if isinstance(self.source_provenance, SourceProvenance) else SourceProvenance.from_dict(self.source_provenance)
        self.policy = self.policy if isinstance(self.policy, PolicyDecision) else PolicyDecision.from_dict(self.policy)
        self.approval = ApprovalDict({"state": ApprovalState(self.approval.get("state", "proposed")).value, "approver": self.approval.get("approver"), "approved_at": self.approval.get("approved_at")})
        if not self.execution:
            self.execution = {}
        self.execution = ExecutionDict({
            "mode": self.execution.get("mode", "dry_run"),
            "idempotency_key": self.execution.get("idempotency_key") or idempotency_for([self.action_type.value, self.platform, self.content.content_hash, self.target.post_id or self.target.account_or_channel_id]),
            "lease_owner": self.execution.get("lease_owner"),
            "lease_expires_at": self.execution.get("lease_expires_at"),
            "retry_count": int(self.execution.get("retry_count", 0)),
        })
        self.audit = AuditDict({"redaction_applied": bool(self.audit.get("redaction_applied", False)), "preview_rendered": bool(self.audit.get("preview_rendered", False)), "result": self.audit.get("result")})
        self.validate()

    @property
    def platform(self) -> str:
        return self.target.platform.value

    @property
    def state(self) -> str:
        return self.approval.state.value

    @property
    def mode(self) -> str:
        return self.execution.mode

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported schema_version")
        if not self.action_id or not self.created_at or not self.created_by:
            raise ValidationError("action_id, created_at, and created_by are required")
        if not self.source_ids:
            raise ValidationError("source_ids required")
        if not self.content.get("content_hash"):
            raise ValidationError("content_hash required")
        if not self.execution.get("idempotency_key"):
            raise ValidationError("execution.idempotency_key required")
        if self.mode not in {"dry_run", "live"}:
            raise ValidationError("execution.mode must be dry_run or live")
        valid_pairs = {
            Platform.X: {ActionType.DRAFT_POST, ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST},
            Platform.THREADS: {ActionType.DRAFT_POST, ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST},
            Platform.INSTAGRAM: {ActionType.DRAFT_POST, ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST},
            Platform.DISCORD: {ActionType.SEND_DISCORD_MESSAGE, ActionType.DELEGATE_AGENT_TASK, ActionType.CONTROL_COMMAND, ActionType.CANCEL_TASK, ActionType.KILL_SWITCH},
            Platform.TELEGRAM: {ActionType.SEND_TELEGRAM_MESSAGE, ActionType.CONTROL_COMMAND, ActionType.CANCEL_TASK, ActionType.KILL_SWITCH},
            Platform.LOCAL: {
                ActionType.DRAFT_POST,
                ActionType.AGENT_RESEARCH,
                ActionType.AGENT_DRAFT,
                ActionType.AGENT_TOOL_CALL,
                ActionType.AGENT_SOCIAL_INTENT,
                ActionType.AGENT_MEMORY_WRITE,
                ActionType.CONTROL_COMMAND,
                ActionType.CANCEL_TASK,
                ActionType.KILL_SWITCH,
                ActionType.MEMORY_WRITE,
            },
        }
        if self.action_type not in valid_pairs[self.target.platform]:
            raise ValidationError(f"{self.action_type.value} is invalid for platform {self.platform}")
        if self.action_type == ActionType.REPLY_TO_POST and not (self.target.reply_to_post_id or self.target.post_id):
            raise ValidationError("reply_to_post requires target post id")
        if self.action_type in {ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST} and not self.target.account_or_channel_id:
            raise ValidationError("public social action requires account_or_channel_id")
        if self.action_type in {ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST} and not self.source_provenance.allows(Sink.PUBLIC_POST) and not self.metadata.get("explicit_public_approval"):
            raise ValidationError("public action requires public_post allowed sink or explicit approval metadata")
        if self.action_type in {ActionType.SEND_DISCORD_MESSAGE, ActionType.DELEGATE_AGENT_TASK} and self.target.platform != Platform.DISCORD:
            raise ValidationError("discord action requires discord target")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "target": self.target.to_dict(),
            "content": self.content.to_dict(),
            "source_ids": list(self.source_ids),
            "source_provenance": self.source_provenance.to_dict(),
            "created_by": self.created_by,
            "action_id": self.action_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "policy": self.policy.to_dict(),
            "approval": dict(self.approval),
            "execution": dict(self.execution),
            "audit": dict(self.audit),
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionEnvelope":
        return cls(**dict(data))

    def is_live_executable(self) -> bool:
        return self.mode == "live" and self.state in LIVE_ELIGIBLE_STATES

    def require_live_executable(self) -> None:
        if not self.is_live_executable():
            raise PermissionError(f"action {self.action_id} is not approved for live execution")

    def with_state(self, state: ApprovalState | str, *, result: dict[str, Any] | None = None, approver: str | None = None) -> "ActionEnvelope":
        data = self.to_dict()
        next_state = ApprovalState(state)
        data["approval"]["state"] = next_state.value
        if next_state == ApprovalState.APPROVED_FOR_LIVE:
            data["approval"]["approver"] = approver or data["approval"].get("approver")
            data["approval"]["approved_at"] = utc_now()
        if result is not None:
            data["audit"]["result"] = result
        if next_state == ApprovalState.DRY_RUN_COMPLETED:
            data["audit"]["preview_rendered"] = True
        return ActionEnvelope.from_dict(data)


def make_content(text: str) -> Content:
    return Content(text)


def sink_for_action(action_type: ActionType | str, target: Target | dict[str, Any] | None = None) -> Sink:
    action = ActionType(action_type)
    if action in {ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST}:
        return Sink.PUBLIC_POST
    if action in {ActionType.SEND_DISCORD_MESSAGE, ActionType.SEND_TELEGRAM_MESSAGE, ActionType.DELEGATE_AGENT_TASK, ActionType.CONTROL_COMMAND}:
        return Sink.PRIVATE_REPLY
    if action in {ActionType.MEMORY_WRITE, ActionType.AGENT_MEMORY_WRITE}:
        return Sink.MEMORY
    if action == ActionType.AGENT_SOCIAL_INTENT:
        return Sink.PUBLIC_POST
    return Sink.DRAFT


def make_action(
    *,
    action_type: ActionType | str,
    platform: Platform | str | None = None,
    target: Target | dict[str, Any] | None = None,
    text: str,
    source_ids: Iterable[str],
    provenance: SourceProvenance,
    policy: PolicyDecision | dict[str, Any] | None = None,
    account_or_channel_id: str | None = None,
    reply_to_post_id: str | None = None,
    mode: str = "dry_run",
    state: ApprovalState | str | None = None,
    created_by: str = "agent",
    metadata: dict[str, Any] | None = None,
    target_extra: dict[str, Any] | None = None,
) -> ActionEnvelope:
    action = ActionType(action_type)
    if target is None:
        if platform is None:
            raise ValidationError("platform or target is required")
        target_data: dict[str, Any] = {"platform": Platform(platform).value, "account_or_channel_id": account_or_channel_id or ""}
        if reply_to_post_id:
            target_data["reply_to_post_id"] = reply_to_post_id
            target_data["post_id"] = reply_to_post_id
        if action == ActionType.DELEGATE_AGENT_TASK:
            target_data.setdefault("agent_name", (target_extra or {}).get("agent_name", "codex-local"))
        if target_extra:
            target_data.update(target_extra)
        target = Target.from_dict(target_data)
    else:
        target = target if isinstance(target, Target) else Target.from_dict(target)
    if policy is None:
        policy = PolicyDecision(PolicyClassification.ALLOWED, ("policy_checks_pending",), "2026-04-28")
    # Derive initial approval from policy unless caller supplied an explicit state.
    pol = PolicyDecision.from_dict(policy) if isinstance(policy, dict) else policy
    selected_state = ApprovalState(state).value if state is not None else (ApprovalState.POLICY_REJECTED.value if pol.classification == PolicyClassification.BLOCKED else ApprovalState.NEEDS_HUMAN_APPROVAL.value if pol.classification == PolicyClassification.NEEDS_HUMAN_APPROVAL else ApprovalState.DRY_RUN_READY.value)
    return ActionEnvelope(
        action_type=action,
        target=target,
        content=Content(text),
        source_ids=list(source_ids),
        source_provenance=provenance,
        created_by=created_by,
        policy=pol,
        approval={"state": selected_state, "approver": None, "approved_at": None},
        execution={"mode": mode, "idempotency_key": idempotency_for([action.value, target.platform.value, target.account_or_channel_id, target.post_id or "", stable_hash(text)]), "lease_owner": None, "lease_expires_at": None, "retry_count": 0},
        metadata=metadata or {},
    )
