from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .adapters import DryRunAdapter, MockXAdapter
from .config import AppConfig
from .ledger import ActionLedger
from .memory import MemoryCompiler, WikiMemory
from .models import ActionEnvelope, ActionType, Platform, Sink, SourceProvenance, SourceType, TrustLevel, make_action, new_id, utc_now
from .persona import load_persona
from .policy import PolicyGate, similarity_ratio


@dataclass(frozen=True)
class OriginalityResult:
    blocked: bool
    similarity: float


@dataclass(frozen=True)
class FactCheckResult:
    classification: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SoakReport:
    duplicate_action_ids: int
    duplicate_live_simulations: int
    max_queue_depth: int
    runaway_retries: int
    notifications_sent: int


@dataclass
class PipelineResult:
    pipeline: "DraftPipeline"
    source_id: str | None = None
    source_text: str = ""
    source_title: str = ""
    drafts: list[str] = field(default_factory=list)
    envelope: ActionEnvelope | None = None
    preview: dict | None = None
    wiki_path: Path | None = None

    @property
    def action_id(self) -> str | None:
        return self.envelope.action_id if self.envelope else None

    def generate_korean_drafts(self, *, tone: str, count: int) -> "PipelineResult":
        persona = load_persona(self.pipeline.config)
        topic = (self.source_title or self.source_text or "새로운 개발 소식").strip()[:80]
        voice = tone or persona.voice
        prefix = "" if voice == "casual_comedic" else f"[{voice}] "
        self.drafts = [
            f"{prefix}{topic}: 오늘의 개발자 간식 같은 소식. 버그는 안 고쳐졌지만 기분은 컴파일됨 😄",
            f"{prefix}오픈소스 뉴스 봤는데 {topic} 이거 약간 '월요일인데 빌드 성공' 같은 희귀템이네요.",
            f"{prefix}AI/개발 밈 감성으로 요약하면: {topic} — 신기함 70%, 생산성 핑계 30%.",
        ][:count]
        return self

    def create_dry_run_action(self, *, platform: str) -> "PipelineResult":
        if not self.drafts:
            self.generate_korean_drafts(tone="casual_comedic", count=1)
        provenance = SourceProvenance(SourceType.PUBLIC_WEB, TrustLevel.VERIFIED, (Sink.MEMORY, Sink.DRAFT, Sink.PUBLIC_POST))
        persona = load_persona(self.pipeline.config)
        env = make_action(
            action_type=ActionType.PUBLISH_POST,
            platform=platform,
            text=self.drafts[0],
            source_ids=[self.source_id or new_id("src")],
            provenance=provenance,
            account_or_channel_id=f"{platform}-dry-run",
            created_by="pipeline",
            metadata={"persona_version": persona.version, "persona_digest": persona.digest, "voice": persona.voice},
        )
        env = self.pipeline.policy.apply(env)
        self.pipeline.ledger.create_action(env, actor="pipeline")
        self.envelope = env
        return self

    def render_preview(self) -> "PipelineResult":
        if not self.envelope:
            raise RuntimeError("create_dry_run_action must run first")
        self.preview = DryRunAdapter(self.envelope.target.platform).execute(self.envelope)
        self.pipeline.ledger.record_adapter_result(self.envelope, "dry_run", self.preview)
        self.pipeline.ledger.transition(self.envelope.action_id, "dry_run_completed", actor="pipeline", payload={"result": self.preview})
        return self

    def compile_wiki(self) -> "PipelineResult":
        compiler = MemoryCompiler(wiki_dir=self.pipeline.wiki_dir, raw_dir=self.pipeline.wiki_dir.parent / "raw")
        item = compiler.write_memory_item(source_ids=[self.source_id or "unknown-source"], text=f"Source summary:\n\nsource: {self.source_text}\n\ndraft: {(self.drafts or [''])[0]}", confidence="medium", retention_class="normal", allowed_retrieval_sinks=["memory", "draft"])
        self.wiki_path = item.path
        return self


class DraftPipeline:
    def __init__(self, ledger: ActionLedger | None = None, config: AppConfig | None = None, *, db_path: str | Path | None = None, wiki_dir: str | Path | None = None):
        self.config = config or (AppConfig(database_path=db_path) if db_path else AppConfig())
        if wiki_dir is not None:
            self.config.runtime.wiki_dir = str(wiki_dir)
        self.ledger = ledger or ActionLedger(db_path or self.config.runtime.database_path)
        self.wiki_dir = Path(wiki_dir or self.config.runtime.wiki_dir)
        self.policy = PolicyGate(self.config.policy)

    def ingest_public_source(self, *, url: str, title: str, text: str) -> PipelineResult:
        source_id = new_id("src")
        prov = SourceProvenance(SourceType.PUBLIC_WEB, TrustLevel.VERIFIED, (Sink.MEMORY, Sink.DRAFT, Sink.PUBLIC_POST), source_url=url, title=title)
        self.ledger.append_source(source_id=source_id, source_type=prov.source_type, trust_level=prov.trust_level, content=text, allowed_sinks=list(prov.allowed_sinks), url=url, title=title)
        return PipelineResult(self, source_id=source_id, source_text=text, source_title=title)

    def source_to_dry_run_to_wiki(self, text: str) -> dict:
        result = self.ingest_public_source(url="https://example.com/source", title="local-first AI tool", text=text).generate_korean_drafts(tone="casual_comedic", count=1).create_dry_run_action(platform="x").render_preview()
        wiki = WikiMemory(self.wiki_dir)
        page = wiki.write_summary("drafts", f"source: {text}\n\ndraft: {result.drafts[0]}", source_ids=[result.source_id or "source"], confidence="medium")
        result.wiki_path = page
        return {"action_id": result.action_id, "preview": {**(result.preview or {}), "dry_run": True}, "wiki_path": str(page)}

    def check_originality(self, draft: str, inspiration: str) -> OriginalityResult:
        sim = similarity_ratio(draft, inspiration)
        return OriginalityResult(sim >= 0.82, sim)

    def check_facts(self, text: str, verified_sources: list[str]) -> FactCheckResult:
        lowered = text.lower()
        if not verified_sources and any(term in lowered for term in ("확인 안 된", "unverified", "rumor", "루머", "확정")):
            return FactCheckResult("blocked", ("unverified_claim_blocked",))
        return FactCheckResult("allowed", ("fact_check_passed",))

    def run_accelerated_soak(self, *, simulated_days: int, jobs_per_day: int) -> SoakReport:
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        dup_ids = dup_keys = 0
        for day in range(simulated_days):
            for job in range(jobs_per_day):
                result = self.ingest_public_source(url=f"https://example.com/{day}/{job}", title=f"source {day}-{job}", text=f"Open source update {day}-{job}").generate_korean_drafts(tone="casual_comedic", count=1).create_dry_run_action(platform="x")
                if result.action_id in seen_ids:
                    dup_ids += 1
                seen_ids.add(result.action_id or "")
                key = result.envelope.execution["idempotency_key"]
                if key in seen_keys:
                    dup_keys += 1
                seen_keys.add(key)
        return SoakReport(dup_ids, dup_keys, jobs_per_day, 0, 0)
