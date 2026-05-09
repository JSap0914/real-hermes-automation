from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import AdapterConfig, AppConfig
from .ledger import ActionLedger, LedgerError
from .models import ActionEnvelope, ActionType, ApprovalState, Platform


class AdapterError(RuntimeError):
    pass


SAFE_ENV_RE = re.compile(r"[^A-Z0-9_]")


class EnvProfileCredentialResolver:
    """Resolve a profile label to a bearer token at runtime without persistence."""

    def __init__(self, *, prefix: str = "SOCIAL_AGENT_CREDENTIAL_") -> None:
        self.prefix = prefix

    def resolve(self, profile_id: str | None) -> str:
        if not profile_id:
            raise AdapterError("credential profile id is required for live HTTP")
        env_name = self.prefix + SAFE_ENV_RE.sub("_", profile_id.upper())
        token = os.environ.get(env_name)
        if not token:
            raise AdapterError(f"credential profile {profile_id} is not available in environment")
        return token


@dataclass
class AdapterResult:
    ok: bool
    dry_run: bool
    adapter: str
    remote_id: str | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = {"ok": self.ok, "dry_run": self.dry_run, "adapter": self.adapter, "remote_id": self.remote_id, "payload": self.payload}
        for key in ("preview", "network_write", "action_type", "target_post_id", "content_hash", "reply_to_post"):
            if key in self.payload:
                data[key] = self.payload[key]
        return data


class FakeSocialHttpTransport:
    """Fake HTTP transport that records calls without touching the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def write_count(self) -> int:
        return len(self.calls)

    def post(self, endpoint: str, payload: dict[str, Any], *, idempotency_key: str, correlation_id: str | None = None) -> dict[str, Any]:
        self.calls.append({"endpoint": endpoint, "payload": payload, "idempotency_key": idempotency_key, "correlation_id": correlation_id})
        return {"ok": True, "remote_id": f"fake-{len(self.calls)}", "endpoint": endpoint}


class UrllibSocialHttpTransport:
    """Small official-API HTTP boundary used only when explicitly configured.

    Tests/default runtime use FakeSocialHttpTransport. This transport resolves a
    bearer token from an environment-backed profile label at call time, sends it
    to the remote API, and never returns/persists the token in result payloads.
    """

    def __init__(self, *, base_url: str, resolver: EnvProfileCredentialResolver | None = None, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.resolver = resolver or EnvProfileCredentialResolver()
        self.timeout_seconds = timeout_seconds
        self.calls: list[dict[str, Any]] = []

    @property
    def write_count(self) -> int:
        return len(self.calls)

    def post(self, *, platform: str, endpoint: str, payload: dict[str, Any], headers: dict[str, str]):
        profile_id = headers.get("X-Credential-Profile")
        token = self.resolver.resolve(profile_id)
        request_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": headers.get("Idempotency-Key", ""),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + endpoint, data=body, headers=request_headers, method="POST")
        self.calls.append({"method": "POST", "platform": platform, "endpoint": endpoint, "payload": dict(payload), "credential_profile_id": profile_id})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # nosec - explicit live transport only
                response_body = response.read().decode("utf-8") or "{}"
                try:
                    parsed = json.loads(response_body)
                except json.JSONDecodeError:
                    parsed = {"body": response_body}
                return FakeSocialHttpResponse(int(response.status), parsed)
        except urllib.error.HTTPError as exc:
            try:
                parsed = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                parsed = {"error": "http_error"}
            return FakeSocialHttpResponse(int(exc.code), parsed)


class SideEffectAdapter(Protocol):
    def execute(self, envelope: ActionEnvelope) -> dict[str, Any]: ...


READINESS_FLAGS = (
    "docs_reviewed",
    "scopes_documented",
    "mocked_tests_passed",
    "dry_run_preview_passed",
    "policy_wired",
    "rate_limits_configured",
    "idempotency_tests_passed",
    "manual_live_enable",
)


def _readiness_dict(readiness: dict[str, bool] | bool | None) -> dict[str, bool]:
    if readiness is True:
        return {flag: True for flag in READINESS_FLAGS}
    if readiness is False or readiness is None:
        return {flag: False for flag in READINESS_FLAGS}
    return {flag: bool(readiness.get(flag, False)) for flag in READINESS_FLAGS}


def _missing_readiness(readiness: dict[str, bool] | bool | None) -> list[str]:
    values = _readiness_dict(readiness)
    return [flag for flag in READINESS_FLAGS if not values.get(flag, False)]


def _readiness_ok(readiness: dict[str, bool] | bool | None) -> bool:
    return not _missing_readiness(readiness)


class DryRunAdapter:
    name = "dry_run"

    def __init__(self, platform: Platform | str | ActionLedger = Platform.LOCAL):
        self.ledger = platform if isinstance(platform, ActionLedger) else None
        self.platform = Platform.LOCAL if self.ledger is not None else Platform(platform)

    def execute(self, envelope: ActionEnvelope) -> dict[str, Any]:
        return {
            "ok": True,
            "dry_run": True,
            "adapter": self.name,
            "platform": envelope.platform,
            "action_type": envelope.action_type.value,
            "target": envelope.target.to_dict(),
            "text": envelope.content.text,
            "content_hash": envelope.content.content_hash,
            "idempotency_key": envelope.execution["idempotency_key"],
            "network_write": False,
            "preview": True,
        }

    def render_preview(self, envelope: ActionEnvelope) -> AdapterResult:
        if self.ledger is not None:
            try:
                self.ledger.state(envelope.action_id)
            except LedgerError as exc:
                raise AdapterError("external side effects require an action ledger record") from exc
        payload = self.execute(envelope)
        if self.ledger is not None:
            self.ledger.record_adapter_result(envelope, self.name, payload)
        return AdapterResult(True, True, self.name, None, payload)


@dataclass
class FakeSocialHttpResponse:
    status_code: int
    json_body: dict[str, Any]


class FakeSocialHttpTransport:
    """In-memory fake transport used by official adapters and tests.

    This is deliberately not a real HTTP client. It records the request shape
    that a live adapter would send only after the ledger/readiness gates pass.
    """

    def __init__(self, *, status_code: int = 200, json_body: dict[str, Any] | None = None):
        self.status_code = status_code
        self.json_body = json_body or {}
        self.calls: list[dict[str, Any]] = []

    @property
    def write_count(self) -> int:
        return len(self.calls)

    def post(self, *, platform: str, endpoint: str, payload: dict[str, Any], headers: dict[str, str]) -> FakeSocialHttpResponse:
        call = {"method": "POST", "platform": platform, "endpoint": endpoint, "payload": dict(payload), "headers": dict(headers)}
        self.calls.append(call)
        body = dict(self.json_body)
        body.setdefault("id", f"{platform}-fake-{len(self.calls)}")
        return FakeSocialHttpResponse(self.status_code, body)


class OfficialSocialAdapter:
    """Ledger-gated official social adapter path with deterministic fake HTTP.

    The adapter renders dry-run previews without network access. Live execution
    is intentionally possible only when all of these are true:
    - a ledger instance was supplied and contains the action,
    - the action is approved/scheduled/executing for live,
    - the adapter is enabled, live-enabled, and all readiness flags are true.
    """

    platform: Platform
    endpoint: str
    supported_actions: frozenset[ActionType]

    def __init__(
        self,
        platform: Platform | str,
        endpoint: str,
        *,
        supported_actions: set[ActionType] | frozenset[ActionType],
        ledger: ActionLedger | None = None,
        config: AdapterConfig | None = None,
        readiness: dict[str, bool] | bool | None = None,
        live_enabled: bool | None = None,
        transport: FakeSocialHttpTransport | None = None,
    ):
        self.platform = Platform(platform)
        self.endpoint = endpoint
        self.supported_actions = frozenset(supported_actions)
        self.ledger = ledger
        if config is None:
            config = AdapterConfig(enabled=True)
            if readiness is not None:
                config.readiness.update(_readiness_dict(readiness))
            if live_enabled is not None:
                config.live_enabled = live_enabled
        self.config = config
        self.transport = transport or FakeSocialHttpTransport()
        self._executed: set[str] = set()

    @property
    def name(self) -> str:
        return self.platform.value

    def publish_post(self, envelope: ActionEnvelope) -> AdapterResult:
        if envelope.action_type != ActionType.PUBLISH_POST:
            raise AdapterError(f"{self.name} publish_post requires publish_post envelope")
        result = self.execute(envelope)
        return AdapterResult(True, result["dry_run"], self.name, result.get("remote_id"), result)

    def reply_to_post(self, envelope: ActionEnvelope) -> AdapterResult:
        if envelope.action_type != ActionType.REPLY_TO_POST:
            raise AdapterError(f"{self.name} reply_to_post requires reply_to_post envelope")
        result = self.execute(envelope)
        return AdapterResult(True, result["dry_run"], self.name, result.get("remote_id"), result)

    def dry_run(self, envelope: ActionEnvelope) -> dict[str, Any]:
        self._validate_envelope(envelope)
        if self.ledger is not None:
            try:
                self.ledger.state(envelope.action_id)
            except LedgerError as exc:
                raise AdapterError("external side effects require an action ledger record") from exc
        payload = self._base_payload(envelope)
        payload.update(
            {
                "dry_run": True,
                "network_write": False,
                "preview": True,
                "http_method": "POST",
                "endpoint": self.endpoint,
            "request_payload": self._request_payload(envelope, resolve_remote=False),
            }
        )
        return payload

    def render_preview(self, envelope: ActionEnvelope) -> AdapterResult:
        payload = self.dry_run(envelope)
        if self.ledger is not None:
            self.ledger.record_adapter_result(envelope, self.name, payload)
        return AdapterResult(True, True, self.name, None, payload)

    def execute(self, envelope: ActionEnvelope) -> dict[str, Any]:
        self._validate_envelope(envelope)
        if envelope.mode == "dry_run":
            return self.dry_run(envelope)

        ledger_state = self._require_live_ledger_state(envelope)
        if ledger_state not in {"approved_for_live", "scheduled", "executing"} or envelope.approval.state not in {ApprovalState.APPROVED_FOR_LIVE, ApprovalState.SCHEDULED, ApprovalState.EXECUTING}:
            raise AdapterError("live adapter blocked: state is not approved")
        if not self.config.enabled:
            raise AdapterError(f"live adapter blocked: {self.name} adapter is disabled")
        if not self.config.live_enabled:
            raise AdapterError(f"live adapter blocked: {self.name} live_enabled is false")
        missing = _missing_readiness(self.config.readiness)
        if missing:
            raise AdapterError(f"live adapter blocked: readiness gates incomplete ({', '.join(missing)})")
        if envelope.execution["idempotency_key"] in self._executed:
            raise AdapterError("duplicate idempotency key")

        response = self.transport.post(
            platform=self.name,
            endpoint=self.endpoint,
            payload=self._request_payload(envelope, resolve_remote=True),
            headers={
                "Idempotency-Key": envelope.execution["idempotency_key"],
                "X-Credential-Profile": self.config.credential_profile_id or "",
            },
        )
        if response.status_code >= 400:
            raise AdapterError(f"{self.name} fake HTTP write failed with status {response.status_code}")

        self._executed.add(envelope.execution["idempotency_key"])
        remote_id = str(response.json_body.get("id") or f"{self.name}-fake-{envelope.action_id}")
        result = self._base_payload(envelope)
        result.update(
            {
                "dry_run": False,
                "network_write": True,
                "preview": False,
                "http_method": "POST",
                "endpoint": self.endpoint,
                "status_code": response.status_code,
                "remote_id": remote_id,
            }
        )
        if self.ledger is not None:
            self.ledger.record_adapter_result(envelope, self.name, result)
        return result

    def _validate_envelope(self, envelope: ActionEnvelope) -> None:
        if envelope.target.platform != self.platform:
            raise AdapterError(f"wrong platform for {self.name} adapter")
        if envelope.action_type not in self.supported_actions:
            raise AdapterError(f"{self.name} adapter does not support {envelope.action_type.value}")

    def _require_live_ledger_state(self, envelope: ActionEnvelope) -> str:
        if self.ledger is None:
            raise AdapterError("external side effects require an action ledger record")
        try:
            return self.ledger.state(envelope.action_id)
        except LedgerError as exc:
            raise AdapterError("external side effects require an action ledger record") from exc

    def _base_payload(self, envelope: ActionEnvelope) -> dict[str, Any]:
        payload = {
            "ok": True,
            "adapter": self.name,
            "official_api": True,
            "platform": envelope.platform,
            "action_type": envelope.action_type.value,
            "target": envelope.target.to_dict(),
            "text": envelope.content.text,
            "content_hash": envelope.content.content_hash,
            "idempotency_key": envelope.execution["idempotency_key"],
            "correlation_id": envelope.action_id,
            "credential_profile_id": self.config.credential_profile_id,
            "artifact_dir": self.config.artifact_dir,
            "remote_id": None,
        }
        if envelope.action_type == ActionType.REPLY_TO_POST:
            payload["reply_to_post"] = envelope.target.post_id
            payload["target_post_id"] = envelope.target.post_id
        thread_meta = self._thread_metadata(envelope)
        if thread_meta:
            payload.update(thread_meta)
        return payload

    def _request_payload(self, envelope: ActionEnvelope, *, resolve_remote: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "text": envelope.content.text,
            "account_id": envelope.target.account_or_channel_id,
            "correlation_id": envelope.action_id,
            "content_hash": envelope.content.content_hash,
        }
        if envelope.action_type == ActionType.REPLY_TO_POST:
            data["reply_to_post_id"] = self._resolve_reply_target(envelope, resolve_remote=resolve_remote)
        return data

    def _resolve_reply_target(self, envelope: ActionEnvelope, *, resolve_remote: bool) -> str | None:
        predecessor = envelope.metadata.get("predecessor_action_id")
        if not predecessor:
            return envelope.target.post_id
        if not resolve_remote:
            return f"ledger:{predecessor}"
        if self.ledger is None:
            raise AdapterError("thread predecessor resolution requires an action ledger")
        result = self.ledger.adapter_result(str(predecessor))
        remote_id = (result or {}).get("remote_id")
        if not remote_id:
            raise AdapterError("thread predecessor has no recorded remote_id")
        return str(remote_id)

    def _thread_metadata(self, envelope: ActionEnvelope) -> dict[str, Any]:
        keys = (
            "thread_group_id",
            "thread_kind",
            "thread_index",
            "thread_total",
            "sequence_index",
            "sequence_total",
            "predecessor_action_id",
            "dependency_policy",
            "publish_strategy",
        )
        return {key: envelope.metadata[key] for key in keys if key in envelope.metadata}


class OfficialXAdapter(OfficialSocialAdapter):
    def __init__(self, ledger: ActionLedger | None = None, config: AdapterConfig | None = None, readiness: dict[str, bool] | bool | None = None, live_enabled: bool | None = None, transport: FakeSocialHttpTransport | None = None):
        if isinstance(ledger, dict) or isinstance(ledger, bool):
            readiness = ledger
            ledger = None
        super().__init__(Platform.X, "/2/tweets", supported_actions={ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST}, ledger=ledger, config=config, readiness=readiness, live_enabled=live_enabled, transport=transport)


class OfficialThreadsAdapter(OfficialSocialAdapter):
    def __init__(self, ledger: ActionLedger | None = None, config: AdapterConfig | None = None, readiness: dict[str, bool] | bool | None = None, live_enabled: bool | None = None, transport: FakeSocialHttpTransport | None = None):
        if isinstance(ledger, dict) or isinstance(ledger, bool):
            readiness = ledger
            ledger = None
        super().__init__(Platform.THREADS, "/threads/publish", supported_actions={ActionType.PUBLISH_POST, ActionType.REPLY_TO_POST}, ledger=ledger, config=config, readiness=readiness, live_enabled=live_enabled, transport=transport)


class OfficialInstagramAdapter(OfficialSocialAdapter):
    def __init__(self, ledger: ActionLedger | None = None, config: AdapterConfig | None = None, readiness: dict[str, bool] | bool | None = None, live_enabled: bool | None = None, transport: FakeSocialHttpTransport | None = None):
        if isinstance(ledger, dict) or isinstance(ledger, bool):
            readiness = ledger
            ledger = None
        super().__init__(Platform.INSTAGRAM, "/instagram/content_publish", supported_actions={ActionType.PUBLISH_POST}, ledger=ledger, config=config, readiness=readiness, live_enabled=live_enabled, transport=transport)


class MockSocialAdapter:
    def __init__(self, platform: Platform | str, readiness: dict[str, bool] | bool = False, live_enabled: bool = False, ledger: ActionLedger | None = None):
        self.platform = Platform(platform)
        self.readiness = readiness
        self.live_enabled = live_enabled
        self.ledger = ledger
        self._executed: set[str] = set()

    def execute(self, envelope: ActionEnvelope) -> dict[str, Any]:
        if envelope.target.platform != self.platform:
            raise AdapterError(f"wrong platform for {self.platform.value} adapter")
        if self.ledger is not None:
            try:
                state = self.ledger.state(envelope.action_id)
            except LedgerError as exc:
                raise AdapterError("external side effects require an action ledger record") from exc
        else:
            state = envelope.state
        if envelope.mode == "live":
            if self.ledger is None:
                raise AdapterError("external side effects require an action ledger record")
            if state not in {"approved_for_live", "scheduled", "executing"} or envelope.approval.state not in {ApprovalState.APPROVED_FOR_LIVE, ApprovalState.SCHEDULED, ApprovalState.EXECUTING}:
                raise AdapterError("live adapter blocked: state is not approved")
            if not self.live_enabled or not _readiness_ok(self.readiness):
                raise AdapterError("live adapter blocked: readiness gates are not complete")
            if envelope.execution["idempotency_key"] in self._executed:
                raise AdapterError("duplicate idempotency key")
            self._executed.add(envelope.execution["idempotency_key"])
        payload = {
            "ok": True,
            "dry_run": envelope.mode == "dry_run",
            "adapter": self.platform.value,
            "platform": envelope.platform,
            "action_type": envelope.action_type.value,
            "content_hash": envelope.content.content_hash,
            "idempotency_key": envelope.execution["idempotency_key"],
            "network_write": envelope.mode == "live",
            "remote_id": None if envelope.mode == "dry_run" else f"{self.platform.value}-mock-{envelope.action_id}",
        }
        if envelope.action_type == ActionType.REPLY_TO_POST:
            payload["reply_to_post"] = envelope.target.post_id
            payload["target_post_id"] = envelope.target.post_id
        return payload

# Backward-compatible aliases used by early Hermes-first tests.
XOfficialAdapter = OfficialXAdapter
ThreadsOfficialAdapter = OfficialThreadsAdapter
InstagramOfficialAdapter = OfficialInstagramAdapter


class MockXAdapter(OfficialXAdapter):
    def __init__(self, ledger: ActionLedger | None = None, readiness: dict[str, bool] | bool = False, live_enabled: bool = False):
        if isinstance(ledger, dict) or isinstance(ledger, bool):
            readiness = ledger
            ledger = None
        super().__init__(ledger=ledger, readiness=readiness, live_enabled=live_enabled)


class MockThreadsAdapter(OfficialThreadsAdapter):
    def __init__(self, ledger: ActionLedger | None = None, readiness: dict[str, bool] | bool = False, live_enabled: bool = False):
        if isinstance(ledger, dict) or isinstance(ledger, bool):
            readiness = ledger
            ledger = None
        super().__init__(ledger=ledger, readiness=readiness, live_enabled=live_enabled)


class MockDiscordAdapter:
    name = "discord"

    def __init__(self, ledger: ActionLedger | None = None, config: AdapterConfig | None = None, allowed_channels: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None):
        if allowed_channels is None and ledger is not None and not isinstance(ledger, ActionLedger):
            allowed_channels = ledger  # type: ignore[assignment]
            ledger = None
        self.ledger = ledger
        self.config = config or AdapterConfig(enabled=True, allowed_channels=list(allowed_channels or []))
        if allowed_channels is not None:
            self.config.allowed_channels = list(allowed_channels)

    def send_message(self, envelope: ActionEnvelope) -> AdapterResult:
        result = self.execute(envelope)
        return AdapterResult(True, result["dry_run"], "discord", result.get("remote_id"), result)

    def dry_run(self, envelope: ActionEnvelope) -> dict[str, Any]:
        if self.config.allowed_channels and envelope.target.account_or_channel_id not in self.config.allowed_channels:
            raise AdapterError("discord channel is not allowlisted")
        return DryRunAdapter(Platform.DISCORD).execute(envelope)

    def execute(self, envelope: ActionEnvelope) -> dict[str, Any]:
        if envelope.target.platform != Platform.DISCORD:
            raise AdapterError("discord adapter requires discord envelope")
        if self.config.allowed_channels and envelope.target.account_or_channel_id not in self.config.allowed_channels:
            raise AdapterError("discord channel is not allowlisted")
        if envelope.mode == "live":
            if self.ledger is None:
                raise AdapterError("external side effects require an action ledger record")
            try:
                state = self.ledger.state(envelope.action_id)
            except LedgerError as exc:
                raise AdapterError("external side effects require an action ledger record") from exc
            if state not in {"approved_for_live", "scheduled", "executing"} or envelope.approval.state not in {ApprovalState.APPROVED_FOR_LIVE, ApprovalState.SCHEDULED, ApprovalState.EXECUTING}:
                raise AdapterError("live adapter blocked: state is not approved")
        return {**DryRunAdapter(Platform.DISCORD).execute(envelope), "adapter": "discord"}


class Executor:
    """Compatibility executor exported from adapters for older tests."""

    def __init__(self, ledger: ActionLedger, config: AppConfig):
        self.ledger = ledger
        self.config = config
        self.dry_run = DryRunAdapter()
        self.x = OfficialXAdapter(ledger, config.adapter("x"))
        self.threads = OfficialThreadsAdapter(ledger, config.adapter("threads"))
        self.instagram = OfficialInstagramAdapter(ledger, config.adapter("instagram"))
        self.discord = MockDiscordAdapter(ledger, config.adapter("discord"))

    def execute(self, action_id: str) -> AdapterResult:
        env = self.ledger.get_action(action_id)
        state = self.ledger.state(action_id)
        if env.mode == "dry_run":
            if state not in {"dry_run_ready", "needs_human_approval", "approved_for_live"}:
                raise AdapterError(f"dry-run execution blocked from state {state}")
            adapter = self._adapter_for(env)
            payload = adapter.dry_run(env) if hasattr(adapter, "dry_run") else self.dry_run.execute(env)
            adapter_name = getattr(adapter, "name", "dry_run")
            self.ledger.record_adapter_result(env, adapter_name, payload)
            self.ledger.transition(action_id, "dry_run_completed", actor="executor", payload={"result": payload})
            return AdapterResult(True, True, adapter_name, None, payload)
        if self.config.runtime.paused:
            raise AdapterError("executor paused")
        executing = self.ledger.transition(action_id, "executing", actor="executor")
        adapter_obj = self._adapter_for(executing)
        adapter = getattr(adapter_obj, "name", executing.platform)
        try:
            result = adapter_obj.execute(executing)
        except AdapterError as exc:
            self.ledger.transition(action_id, "blocked", actor="executor", payload={"error": str(exc)}, event_type="blocked")
            raise
        self.ledger.record_adapter_result(env, adapter, result)
        self.ledger.transition(action_id, "completed", actor="executor", payload={"result": result})
        return AdapterResult(True, False, adapter, result.get("remote_id"), result)

    def execute_group(self, group_id: str) -> list[AdapterResult]:
        group = self.ledger.get_action_group(group_id)
        results: list[AdapterResult] = []
        for item in group["items"]:
            try:
                results.append(self.execute(item["action_id"]))
            except AdapterError as exc:
                self._block_dependents(group, item["sequence_index"], str(exc))
                raise
        return results

    def approve_group_for_live(self, group_id: str, *, approver: str) -> int:
        try:
            return self.ledger.promote_group_to_live(group_id, approver=approver)
        except LedgerError as exc:
            raise AdapterError(str(exc).replace("group live approval requires all dry-run previews to be completed", "group live approval requires all dry-run previews")) from exc

    def _block_dependents(self, group: dict[str, Any], failed_sequence_index: int, reason: str) -> None:
        for item in group["items"]:
            if int(item["sequence_index"]) <= failed_sequence_index:
                continue
            try:
                state = self.ledger.state(item["action_id"])
                if state not in {"completed", "blocked", "failed", "cancelled", "expired", "policy_rejected"}:
                    self.ledger.transition(item["action_id"], "blocked", actor="executor", payload={"blocked_by_sequence_index": failed_sequence_index, "reason": reason}, event_type="dependency_blocked")
            except Exception:
                continue

    def _adapter_for(self, envelope: ActionEnvelope) -> Any:
        if envelope.platform == "x":
            return self.x
        if envelope.platform == "threads":
            return self.threads
        if envelope.platform == "instagram":
            return self.instagram
        if envelope.platform == "discord":
            return self.discord
        return self.dry_run
