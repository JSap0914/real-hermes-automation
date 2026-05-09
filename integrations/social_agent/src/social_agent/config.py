from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SECRET_LIKE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY)\s*[:=]\s*[^\s]+)", re.I)


@dataclass(slots=True)
class AdapterConfig:
    enabled: bool = False
    live_enabled: bool = False
    credential_profile_id: str | None = None
    artifact_dir: str | None = None
    readiness: dict[str, bool] = field(default_factory=lambda: {
        "docs_reviewed": False,
        "scopes_documented": False,
        "mocked_tests_passed": False,
        "dry_run_preview_passed": False,
        "policy_wired": False,
        "rate_limits_configured": False,
        "idempotency_tests_passed": False,
        "manual_live_enable": False,
    })
    allowed_channels: list[str] = field(default_factory=list)
    allowed_users: list[str] = field(default_factory=list)
    owner_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.credential_profile_id:
            reject_secret_value("adapter.credential_profile_id", self.credential_profile_id)

    def ready_for_live(self) -> bool:
        required = [
            "docs_reviewed",
            "scopes_documented",
            "mocked_tests_passed",
            "dry_run_preview_passed",
            "policy_wired",
            "rate_limits_configured",
            "idempotency_tests_passed",
            "manual_live_enable",
        ]
        return self.enabled and self.live_enabled and all(self.readiness.get(k, False) for k in required)

    @property
    def ready(self) -> bool:
        return self.ready_for_live()

    @property
    def can_live(self) -> bool:
        return self.ready_for_live()


@dataclass(slots=True)
class RuntimeConfig:
    dry_run_default: bool = True
    paused: bool = False
    database_path: str = ".local/social_agent.sqlite3"
    raw_log_dir: str = "memory/raw"
    wiki_dir: str = "memory/wiki"
    safe_mode: bool = True


@dataclass(slots=True)
class PolicyConfig:
    platform_policy_version: str = "2026-04-28"
    max_post_chars: int = 280
    similarity_threshold: float = 0.82
    allow_autonomous_live: bool = False


@dataclass(slots=True)
class PersonaConfig:
    name: str = "Hermes Social Agent"
    profile_path: str = "memory/wiki/persona.yaml"
    voice_guide_path: str = "memory/wiki/voice-guide.md"
    language: str = "ko"
    voice: str = "casual_comedic"
    humor_level: float = 0.7
    sarcasm_level: float = 0.2
    profanity_level: str = "mild"
    version: str = "default-v1"
    require_approval_for_updates: bool = True

    def __post_init__(self) -> None:
        for field_name in ("name", "profile_path", "voice_guide_path", "language", "voice", "profanity_level", "version"):
            reject_secret_value(f"persona.{field_name}", str(getattr(self, field_name)))


@dataclass(slots=True)
class BrainConfig:
    provider: str = "fake_hermes"
    provider_profile_id: str = "local-fake-profile"
    artifact_dir: str = ".local/artifacts"
    default_allowed_tools: list[str] = field(default_factory=lambda: ["ledger_gateway.create_action", "ledger_gateway.record_artifact"])

    def __post_init__(self) -> None:
        reject_secret_value("brain.provider_profile_id", self.provider_profile_id)


@dataclass(slots=True)
class ProviderConfig:
    enabled: bool = False
    provider_name: str = ""
    version: str = "unknown"
    profile_id: str = ""
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.profile_id:
            reject_secret_value(f"providers.{self.provider_name or 'provider'}.profile_id", self.profile_id)


@dataclass(slots=True)
class MemoryConfig:
    provider: str = "wiki"
    provider_profile_id: str = "local-wiki"
    wiki_dir: str = "memory/wiki"
    require_ledger_for_agent_writes: bool = True


@dataclass(slots=True)
class ResearchConfig:
    provider: str = "safe_public"
    provider_profile_id: str = "public-default"
    allow_authenticated_scraping: bool = False
    allow_cookie_browsing: bool = False
    insane_search_enabled: bool = False
    allow_bypass_modes: bool = False


@dataclass(slots=True)
class AgentDirectoryEntry:
    name: str
    channel_id: str
    repo: str = "."
    timeout_seconds: int = 1800


@dataclass(slots=True)
class MessagingConfig:
    hermes_gateway_enabled: bool = False
    agent_messenger_enabled: bool = False
    provider_profile_id: str = "messaging-default"
    allow_desktop_token_extraction: bool = False


@dataclass(slots=True)
class ExperimentalConfig:
    private_session_enabled: bool = False
    experimental_cookie_write: bool = False


@dataclass(slots=True, init=False)
class AppConfig:
    runtime: RuntimeConfig
    policy: PolicyConfig
    persona: PersonaConfig
    brain: BrainConfig
    providers: dict[str, ProviderConfig]
    memory: MemoryConfig
    research: ResearchConfig
    messaging: MessagingConfig
    experimental: ExperimentalConfig
    adapters: dict[str, AdapterConfig]
    agents: list[AgentDirectoryEntry]

    def __init__(
        self,
        database_path: str | os.PathLike[str] | None = None,
        *,
        dry_run_default: bool = True,
        paused: bool = False,
        runtime: RuntimeConfig | None = None,
        policy: PolicyConfig | None = None,
        persona: PersonaConfig | None = None,
        brain: BrainConfig | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        memory: MemoryConfig | None = None,
        research: ResearchConfig | None = None,
        experimental: ExperimentalConfig | None = None,
        adapters: dict[str, AdapterConfig] | None = None,
        agents: list[AgentDirectoryEntry] | None = None,
        messaging: MessagingConfig | None = None,
    ) -> None:
        self.runtime = runtime or RuntimeConfig(dry_run_default=dry_run_default, paused=paused)
        if database_path is not None:
            self.runtime.database_path = str(database_path)
        self.policy = policy or PolicyConfig()
        self.persona = persona or PersonaConfig()
        self.brain = brain or BrainConfig()
        self.providers = providers or {
            "fake_hermes": ProviderConfig(enabled=True, provider_name="fake_hermes", version="test", profile_id="local-fake-profile"),
            "codex_cli": ProviderConfig(enabled=False, provider_name="codex_cli", version="local-cli", profile_id="codex-local"),
            "claude_cli": ProviderConfig(enabled=False, provider_name="claude_cli", version="local-cli", profile_id="claude-local"),
        }
        self.memory = memory or MemoryConfig(wiki_dir=self.runtime.wiki_dir)
        self.research = research or ResearchConfig()
        self.messaging = messaging or MessagingConfig()
        self.experimental = experimental or ExperimentalConfig()
        self.adapters = adapters or {
            "x": AdapterConfig(),
            "threads": AdapterConfig(),
            "instagram": AdapterConfig(),
            "discord": AdapterConfig(enabled=True, allowed_channels=["dev-agent-test", "control"], allowed_users=["owner"]),
            "telegram": AdapterConfig(enabled=True, owner_ids=["owner"]),
        }
        self.agents = agents or [AgentDirectoryEntry("codex-local", "dev-agent-test")]

    def adapter(self, name: str) -> AdapterConfig:
        return self.adapters.setdefault(name, AdapterConfig())

    def provider(self, name: str) -> ProviderConfig:
        return self.providers.setdefault(name, ProviderConfig(provider_name=name))

    @property
    def database_path(self) -> str:
        return self.runtime.database_path

    @property
    def wiki_dir(self) -> str:
        return self.runtime.wiki_dir

    @property
    def raw_log_dir(self) -> str:
        return self.runtime.raw_log_dir

    @property
    def dry_run_default(self) -> bool:
        return self.runtime.dry_run_default

    @property
    def paused(self) -> bool:
        return self.runtime.paused


def _parse_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() in {"null", "none"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip('"').strip("'") for part in inner.split(",")]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def reject_secret_value(field_name: str, value: str) -> None:
    if SECRET_LIKE_RE.search(value):
        raise ValueError(f"{field_name} must store a provider profile id, not an OAuth/API/cookie secret")


def _apply_dataclass_section(instance: Any, data: dict[str, Any]) -> None:
    for key in instance.__dataclass_fields__:
        if key in data:
            value = data[key]
            if key.endswith("profile_id") or key == "profile_id":
                reject_secret_value(key, str(value))
            setattr(instance, key, value)


def _minimal_yaml(path: Path) -> dict[str, Any]:
    """Tiny parser for the committed config.example.yaml shape; avoids PyYAML dependency."""
    if not path.exists():
        return {}
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("-"):
            # The default config only uses list-of-map under agents.allowed; handled by defaults.
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    path = path or os.environ.get("SOCIAL_AGENT_CONFIG", "config.example.yaml")
    data = _minimal_yaml(Path(path))
    cfg = AppConfig()
    runtime = data.get("runtime", {})
    for key in RuntimeConfig.__dataclass_fields__:
        if key in runtime:
            setattr(cfg.runtime, key, runtime[key])
    policy = data.get("policy", {})
    _apply_dataclass_section(cfg.policy, policy)
    persona = data.get("persona", {})
    _apply_dataclass_section(cfg.persona, persona)
    brain = data.get("brain", {})
    _apply_dataclass_section(cfg.brain, brain)
    cfg.memory.wiki_dir = cfg.runtime.wiki_dir
    memory = data.get("memory", {})
    _apply_dataclass_section(cfg.memory, memory)
    research = data.get("research", {})
    _apply_dataclass_section(cfg.research, research)
    messaging = data.get("messaging", {})
    _apply_dataclass_section(cfg.messaging, messaging)
    experimental = data.get("experimental", {})
    _apply_dataclass_section(cfg.experimental, experimental)
    providers = data.get("providers", {})
    for name, pdata in providers.items():
        provider = cfg.provider(name)
        provider.provider_name = name
        if isinstance(pdata, dict):
            _apply_dataclass_section(provider, pdata)
    adapters = data.get("adapters", {})
    for name, adata in adapters.items():
        adapter = cfg.adapter(name)
        if isinstance(adata, dict):
            for key in ("enabled", "live_enabled", "credential_profile_id", "artifact_dir", "allowed_channels", "allowed_users", "owner_ids"):
                if key in adata:
                    if key == "credential_profile_id" and adata[key]:
                        reject_secret_value(f"adapters.{name}.credential_profile_id", str(adata[key]))
                    setattr(adapter, key, adata[key])
            if isinstance(adata.get("readiness"), dict):
                adapter.readiness.update(adata["readiness"])
    if cfg.research.allow_authenticated_scraping or cfg.research.allow_cookie_browsing or cfg.research.allow_bypass_modes:
        cfg.research.allow_authenticated_scraping = False
        cfg.research.allow_cookie_browsing = False
        cfg.research.allow_bypass_modes = False
    if cfg.messaging.allow_desktop_token_extraction:
        cfg.messaging.allow_desktop_token_extraction = False
    if not cfg.experimental.private_session_enabled:
        cfg.experimental.experimental_cookie_write = False
    if os.environ.get("SOCIAL_AGENT_DB"):
        cfg.runtime.database_path = os.environ["SOCIAL_AGENT_DB"]
    live = os.environ.get("SOCIAL_AGENT_LIVE_ENABLED")
    if live and live.lower() not in {"1", "true", "yes"}:
        for adapter in cfg.adapters.values():
            adapter.live_enabled = False
    return cfg
