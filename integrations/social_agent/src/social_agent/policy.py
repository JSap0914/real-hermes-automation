from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import ActionEnvelope, ActionType, Platform, PolicyClassification, PolicyDecision, Sink, Target, sink_for_action

POLITICAL_TERMS = ("election", "대선", "총선", "정당", "president", "국회", "민주당", "국민의힘", "정치", "후보")
HATE_TERMS = ("kill all", "inferior race", "멸종", "혐오")
SEXUAL_TERMS = ("porn", "explicit sexual", "야동", "야한", "성적")
RUMOR_TERMS = ("rumor", "unconfirmed", "카더라", "찌라시", "루머")
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?:sk-|ghp_)[A-Za-z0-9_\-]{10,}"),
)
SHELL_PATTERNS = re.compile(r"\b(rm\s+-rf|curl\s+.+\|\s*(bash|sh)|sudo\s+|powershell\s+|cmd\.exe|bash\s+-c|python\s+-c)\b", re.I)


def _value(v: Any) -> str:
    return getattr(v, "value", v)


@dataclass(frozen=True)
class GateDecision:
    classification: str
    reasons: tuple[str, ...]
    platform_policy_version: str = "2026-04-28"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_metadata(self) -> dict[str, Any]:
        return self.metadata

    def to_dict(self) -> dict[str, Any]:
        return {"classification": self.classification, "reasons": list(self.reasons), "platform_policy_version": self.platform_policy_version, "metadata": self.metadata, "risk_metadata": self.metadata}


@dataclass(frozen=True)
class PolicyGate:
    config: Any | None = None
    platform_policy_version: str = "2026-04-28"
    max_post_chars: int = 280
    similarity_threshold: float = 0.82
    allowed_discord_channels: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        cfg = self.config
        if cfg is not None:
            object.__setattr__(self, "platform_policy_version", getattr(cfg, "platform_policy_version", self.platform_policy_version))
            object.__setattr__(self, "max_post_chars", getattr(cfg, "max_post_chars", self.max_post_chars))
            object.__setattr__(self, "similarity_threshold", getattr(cfg, "similarity_threshold", self.similarity_threshold))

    def evaluate(self, envelope: ActionEnvelope) -> GateDecision:
        decision = self.evaluate_text(
            text=envelope.content["text"],
            action_type=envelope.action_type.value,
            source=envelope.source_provenance,
            target_sink=sink_for_action(envelope.action_type, envelope.target).value,
            target=envelope.target.to_dict() if hasattr(envelope.target, "to_dict") else dict(envelope.target),
            context=dict(envelope.metadata),
        )
        metadata = dict(decision.metadata)
        if "개웃기" in envelope.content["text"] or "미쳤" in envelope.content["text"]:
            metadata["casual_profanity_allowed"] = True
        return GateDecision(decision.classification, decision.reasons, decision.platform_policy_version, metadata)

    def apply(self, envelope: ActionEnvelope) -> ActionEnvelope:
        decision = self.evaluate(envelope)
        data = envelope.to_dict()
        data["policy"] = decision.to_dict()
        if decision.classification == "blocked":
            data["approval"]["state"] = "policy_rejected"
        elif decision.classification == "needs_human_approval":
            data["approval"]["state"] = "needs_human_approval"
        else:
            data["approval"]["state"] = "dry_run_ready"
        return ActionEnvelope.from_dict(data)

    def evaluate_text(self, *, text: str, action_type: str, source: Any, target_sink: str, target: dict[str, Any] | None = None, context: dict[str, Any] | None = None, inspirations: Iterable[str] = (), explicit_public_approval: bool = False) -> GateDecision:
        target = target or {"platform": "x", "account_or_channel_id": "default"}
        context = context or {}
        reasons: list[str] = []
        metadata: dict[str, Any] = {}
        lowered = text.lower()
        forbidden = self._forbidden_action_string(action_type, target, lowered, context)
        if forbidden:
            reasons.append(forbidden)
        if any(term.lower() in lowered for term in POLITICAL_TERMS):
            reasons.append("politics_or_civic_content_blocked")
        if any(term.lower() in lowered for term in HATE_TERMS):
            reasons.append("hate_content_blocked")
        if any(term.lower() in lowered for term in SEXUAL_TERMS):
            reasons.append("sexual_content_blocked")
        if any(term.lower() in lowered for term in RUMOR_TERMS):
            reasons.append("unverified_news_or_rumor_blocked")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            reasons.append("secret_like_text_blocked")
        if len(text) > self.max_post_chars and target.get("platform", "x") == "x":
            reasons.append("x_post_too_long")
        if "ignore previous instructions" in lowered:
            metadata["prompt_injection_detected"] = True
            reasons.append("prompt_injection_treated_as_untrusted_data")
        allowed_sinks = set(_value(s) for s in getattr(source, "allowed_sinks", ()))
        source_type = _value(getattr(source, "source_type", "unknown"))
        if target_sink == "public_post" and "public_post" not in allowed_sinks:
            if explicit_public_approval:
                metadata["private_source_public_post_explicitly_approved"] = True
            else:
                reasons.append("source_not_allowed_for_public_post")
        if source_type in {"private_discord", "private_dm", "local_note"} and target_sink == "public_post" and not explicit_public_approval:
            reasons.append("private_source_public_post_requires_explicit_approval")
        max_similarity = max((similarity_ratio(text, old) for old in inspirations), default=0.0)
        metadata["max_similarity"] = round(max_similarity, 3)
        if max_similarity >= self.similarity_threshold:
            reasons.append("near_copy_blocked")
        if action_type == "reply_to_post" and target.get("platform", "x") == "x":
            metadata.update({"official_api_required": True, "requires_official_api": True, "live_gated": True, "policy_risk": "x_ai_reply"})
            if context.get("bulk") or context.get("reply_class") == "bulk_unsolicited" or "bulk" in lowered or context.get("mentions"):
                reasons.append("bulk_unsolicited_reply_blocked")
            elif not reasons:
                return GateDecision("needs_human_approval", ("x_ai_reply_live_gated",), self.platform_policy_version, metadata)
        if target.get("platform") == "discord" and action_type in {"send_discord_message", "delegate_agent_task"}:
            channel = target.get("account_or_channel_id")
            if self.allowed_discord_channels and channel not in self.allowed_discord_channels:
                reasons.append("discord_channel_not_allowlisted")
            if SHELL_PATTERNS.search(text):
                reasons.append("arbitrary_shell_rejected")
        if reasons:
            return GateDecision("blocked", tuple(dict.fromkeys(reasons)), self.platform_policy_version, metadata)
        if "개웃기" in text or "미쳤" in text:
            metadata["casual_profanity_allowed"] = True
        return GateDecision("allowed", ("policy_checks_passed",), self.platform_policy_version, metadata)

    def decide(self, *, action_type: ActionType, target: Target, text: str, provenance: Any, inspirations: Iterable[str] = (), explicit_public_approval: bool = False) -> PolicyDecision:
        decision = self.evaluate_text(text=text, action_type=action_type.value, source=provenance, target_sink=sink_for_action(action_type, target).value, target=target.to_dict(), inspirations=inspirations, explicit_public_approval=explicit_public_approval)
        return PolicyDecision(PolicyClassification(decision.classification), decision.reasons, self.platform_policy_version, decision.metadata)

    def _forbidden_action_string(self, action_type: str, target: dict[str, Any], lowered: str, context: dict[str, Any]) -> str | None:
        if action_type in {"account_delete", "delete_account"} or "delete account" in lowered or "계정 삭제" in lowered or "비밀번호" in lowered or "change password" in lowered:
            return "account_destruction_forbidden"
        if action_type in {"auto_like", "like"} or "like this" in lowered or "auto-like" in lowered or "좋아요" in lowered:
            return "automated_like_forbidden"
        if action_type == "x_browser_automation" or ("브라우저" in lowered and target.get("platform", "x") == "x") or "browser automation" in lowered:
            return "x_browser_automation_blocked"
        if action_type == "discord_selfbot_message" or "selfbot" in lowered or "user token" in lowered or "유저 토큰" in lowered:
            return "discord_selfbot_or_user_token_blocked"
        if "follow" in lowered or "unfollow" in lowered:
            return "follow_unfollow_forbidden"
        if action_type == "reply_to_post" and target.get("platform", "x") not in {"x", "threads"}:
            return "reply_platform_invalid"
        return None


def similarity_ratio(a: str, b: str) -> float:
    words_a = {w for w in re.findall(r"[\w가-힣]+", a.lower()) if len(w) > 1}
    words_b = {w for w in re.findall(r"[\w가-힣]+", b.lower()) if len(w) > 1}
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)
