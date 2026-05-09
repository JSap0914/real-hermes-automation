from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse
from typing import Any, Protocol

from .ledger import ActionLedger
from .memory import redact_secrets
from .models import ActionType, Platform, Sink, SourceProvenance, SourceType, Target, TrustLevel, make_action, new_id
from .policy import PolicyGate


@dataclass(frozen=True)
class Inspiration:
    source_id: str
    title: str
    url: str
    text: str
    provenance: SourceProvenance


class ResearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchQuery:
    query: str
    provider_profile_id: str = "public-default"
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchSource:
    source_id: str
    title: str
    url: str
    text: str
    provenance: SourceProvenance
    provider: str
    artifact_path: str | None = None

    @property
    def trust_level(self) -> str:
        return self.provenance.trust_level.value


class ResearchProvider(Protocol):
    def search(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]: ...

    def search_public(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]: ...

    def ingest_public_source(self, *, url: str, title: str, text: str, trust_level: TrustLevel = TrustLevel.UNVERIFIED) -> ResearchSource: ...


def _reject_private_or_unsafe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ResearchError("research provider accepts only public http(s) URLs")
    if parsed.username or parsed.password:
        raise ResearchError("credential-bearing URLs are not allowed")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        raise ResearchError("private/local research URLs are disabled by default")


def _reject_secret_like_profile(profile_id: str) -> None:
    redacted, changed = redact_secrets(profile_id)
    if changed or redacted != profile_id or any(marker in profile_id.lower() for marker in ("cookie", "bearer ", "oauth", "token=", "password=")):
        raise ResearchError("provider profile IDs must be labels, not OAuth/API/cookie secrets")


class SafePublicResearchProvider:
    """Default no-network research provider that records public sources.

    A caller may pass a deterministic ``search_backend`` for tests or an
    approved public API client. The provider rejects authenticated/cookie/local
    URLs and always persists returned items as ledger sources with provenance
    before handing them to drafting code.
    """

    name = "safe_public_research"

    def __init__(
        self,
        ledger: ActionLedger,
        *,
        provider_profile_id: str = "public-default",
        search_backend: Callable[[str, int], list[dict[str, str]]] | None = None,
    ) -> None:
        _reject_secret_like_profile(provider_profile_id)
        self.ledger = ledger
        self.provider_profile_id = provider_profile_id
        self.search_backend = search_backend

    def search(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]:
        q = query if isinstance(query, ResearchQuery) else ResearchQuery(str(query), self.provider_profile_id)
        _reject_secret_like_profile(q.provider_profile_id)
        if self.search_backend is None:
            rows = [
                {
                    "url": f"https://example.com/research/{new_id('query')}",
                    "title": f"Public research result for {q.query[:80]}",
                    "text": f"Deterministic public research placeholder for query: {q.query}",
                }
            ]
        else:
            rows = self.search_backend(q.query, limit)
        sources: list[ResearchSource] = []
        allowed_domains = {d.lower() for d in q.allowed_domains}
        for row in rows[:limit]:
            url = row.get("url", "")
            _reject_private_or_unsafe_url(url)
            host = (urlparse(url).hostname or "").lower()
            if allowed_domains and host not in allowed_domains:
                raise ResearchError(f"research result outside allowed domains: {host}")
            sources.append(self.ingest_public_source(url=url, title=row.get("title", url), text=row.get("text", ""), trust_level=TrustLevel.UNVERIFIED))
        return sources

    def search_public(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]:
        return self.search(query, limit=limit)

    def ingest_public_source(self, *, url: str, title: str, text: str, trust_level: TrustLevel = TrustLevel.UNVERIFIED) -> ResearchSource:
        _reject_private_or_unsafe_url(url)
        safe_text, redacted = redact_secrets(text)
        source_id = new_id("src")
        provenance = SourceProvenance(SourceType.PUBLIC_WEB, trust_level, (Sink.MEMORY, Sink.DRAFT, Sink.PUBLIC_POST), source_url=url, title=title)
        self.ledger.append_source(
            source_id=source_id,
            source_type=provenance.source_type.value,
            trust_level=provenance.trust_level.value,
            content=safe_text,
            allowed_sinks=[sink.value for sink in provenance.allowed_sinks],
            url=url,
            title=title,
        )
        return ResearchSource(source_id, title, url, safe_text, provenance, self.name, artifact_path=None if not redacted else f"ledger://sources/{source_id}")

    def ingest(self, *, url: str, title: str, text: str, trust_level: TrustLevel = TrustLevel.UNVERIFIED) -> ResearchSource:
        return self.ingest_public_source(url=url, title=title, text=text, trust_level=trust_level)


class InsaneSearchSandboxProvider:
    """Optional wrapper placeholder that keeps risky search modes disabled."""

    name = "insane_search_sandbox"

    def __init__(self, delegate: SafePublicResearchProvider, *, enabled: bool = False, allow_bypass_modes: bool = False) -> None:
        self.delegate = delegate
        self.enabled = enabled
        self.allow_bypass_modes = allow_bypass_modes

    def search(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]:
        if not self.enabled:
            raise ResearchError("insane-search sandbox is disabled by default")
        if self.allow_bypass_modes:
            raise ResearchError("anti-bot bypass/identity-spoofing modes are not permitted")
        return self.delegate.search(query, limit=limit)

    def search_public(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]:
        return self.search(query, limit=limit)

    def ingest_public_source(self, *, url: str, title: str, text: str, trust_level: TrustLevel = TrustLevel.UNVERIFIED) -> ResearchSource:
        if not self.enabled:
            raise ResearchError("insane-search sandbox is disabled by default")
        return self.delegate.ingest_public_source(url=url, title=title, text=text, trust_level=trust_level)


class InsaneSearchSandbox(InsaneSearchSandboxProvider):
    def __init__(self, ledger: ActionLedger, *, enabled: bool = False, allow_bypass_modes: bool = False) -> None:
        if allow_bypass_modes:
            raise ValueError("anti-bot bypass/identity-spoofing modes are not permitted")
        super().__init__(SafePublicResearchProvider(ledger), enabled=enabled, allow_bypass_modes=allow_bypass_modes)

    def search(self, query: str | ResearchQuery, *, limit: int = 5) -> list[ResearchSource]:
        try:
            return super().search(query, limit=limit)
        except ResearchError as exc:
            raise PermissionError(str(exc)) from exc


class ResearchPipeline:
    def __init__(self, ledger: ActionLedger, policy: PolicyGate) -> None:
        self.ledger = ledger
        self.policy = policy

    def ingest_public_source(self, *, url: str, title: str, text: str, trust_level: TrustLevel = TrustLevel.UNVERIFIED) -> Inspiration:
        source = SafePublicResearchProvider(self.ledger).ingest_public_source(url=url, title=title, text=text, trust_level=trust_level)
        return Inspiration(source.source_id, source.title, source.url, source.text, source.provenance)

    def draft_korean_variants(self, inspiration: Inspiration, *, count: int = 3) -> list[str]:
        topic = inspiration.title.strip() or inspiration.text[:40]
        variants = [
            f"{topic}: 오늘의 개발자 간식 같은 소식. 버그는 안 고쳐졌지만 기분은 컴파일됨 😄",
            f"오픈소스 뉴스 봤는데 {topic} 이거 약간 '월요일인데 빌드 성공' 같은 희귀템이네요.",
            f"AI/개발 밈 감성으로 요약하면: {topic} — 신기함 70%, 생산성 핑계 30%.",
        ]
        return variants[:count]

    def create_dry_run_post(self, inspiration: Inspiration, *, account: str = "local-x"):
        draft = self.draft_korean_variants(inspiration, count=1)[0]
        target = Target(Platform.X, account)
        decision = self.policy.decide(action_type=ActionType.PUBLISH_POST, target=target, text=draft, provenance=inspiration.provenance, inspirations=[inspiration.text])
        envelope = make_action(
            action_type=ActionType.PUBLISH_POST,
            target=target,
            text=draft,
            source_ids=[inspiration.source_id],
            provenance=inspiration.provenance,
            policy=decision,
            mode="dry_run",
        )
        self.ledger.create_action(envelope)
        return envelope
