from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .ledger import ActionLedger, LedgerError
from .models import ActionType, ApprovalState, Platform, Sink, SourceProvenance, SourceType, TrustLevel, make_action, new_id, utc_now

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|[A-Z_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)\s*[:=]\s*[^\s]+|BEGIN PRIVATE KEY)", re.I)
PII_RE = re.compile(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\+?\d[\d -]{8,}\d\b)")


class MemoryError(RuntimeError):
    pass


def redact_secrets(text: str) -> tuple[str, bool]:
    redacted = SECRET_RE.sub("[REDACTED_SECRET]", text)
    return redacted, redacted != text


def sanitize_text(text: str) -> tuple[str, bool]:
    return redact_secrets(text)


def contains_pii(text: str) -> bool:
    return bool(PII_RE.search(text))


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redaction_applied: bool


def scan_and_redact_secrets(text: str) -> RedactionResult:
    redacted, changed = redact_secrets(text)
    return RedactionResult(redacted, changed)


class WikiMemory:
    def __init__(self, wiki_dir: str | Path = "memory/wiki") -> None:
        self.wiki_dir = Path(wiki_dir)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

    def write_summary(self, page: str, summary: str, *, source_ids: list[str], confidence: str = "medium", retention: str = "standard", allowed_sinks: list[str] | None = None) -> Path:
        safe_page = slugify(page)
        redacted, changed = redact_secrets(summary)
        if contains_pii(redacted) and allowed_sinks and "public_post" in allowed_sinks:
            raise MemoryError("PII cannot be written to public-retrievable memory")
        body = (
            f"# {safe_page}\n\n"
            f"- updated_at: {utc_now()}\n"
            f"- source_ids: {', '.join(source_ids)}\n"
            f"- confidence: {confidence}\n"
            f"- retention: {retention}\n"
            f"- allowed_sinks: {', '.join(allowed_sinks or ['memory'])}\n"
            f"- redaction_applied: {str(changed).lower()}\n\n"
            f"## Summary\n\n{redacted}\n"
        )
        path = self.wiki_dir / f"{safe_page}.md"
        _atomic_write(path, body)
        return path

    def forget(self, page: str, reason: str = "operator request") -> Path:
        path = self.wiki_dir / f"{slugify(page)}.md"
        if path.exists():
            _atomic_write(path, path.read_text(encoding="utf-8") + f"\n\nTOMBSTONE: {reason}\n")
            return path
        return self.write_summary(f"{page}-tombstone", f"Forgotten: {reason}", source_ids=["operator"], confidence="high", retention="tombstone")

    def export_bundle(self, output: str | Path, include_raw: bool = False) -> Path:
        pages: dict[str, str] = {}
        for path in sorted(self.wiki_dir.glob("*.md")):
            text, _ = redact_secrets(path.read_text(encoding="utf-8"))
            pages[path.name] = text
        bundle = {"schema_version": 1, "created_at": utc_now(), "wiki_pages": pages}
        out = Path(output)
        out.write_text(json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return out

    def import_bundle(self, input_path: str | Path, *, force: bool = False) -> list[Path]:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise MemoryError("unsupported memory bundle schema")
        written: list[Path] = []
        pages = data.get("wiki_pages", data.get("items", {}))
        if isinstance(pages, list):
            iterable = [(item.get("name", f"import-{i}.md"), item.get("text", "")) for i, item in enumerate(pages)]
        else:
            iterable = list(pages.items())
        for name, text in iterable:
            if contains_pii(text) or SECRET_RE.search(text):
                raise MemoryError(f"refusing unsafe memory page: {name}")
            path = self.wiki_dir / Path(name).name
            if path.exists() and not force:
                raise MemoryError(f"refusing to overwrite existing page without force: {name}")
            _atomic_write(path, text)
            written.append(path)
        return written


class MemoryStore(WikiMemory):
    def __init__(self, ledger=None, wiki_dir: str | Path = "memory/wiki") -> None:
        super().__init__(wiki_dir)
        self.ledger = ledger

    def write_memory(self, *, title: str, summary: str, source_ids: list[str], confidence: str = "medium", retention_class: str = "normal") -> Path:
        return self.write_summary(title, summary, source_ids=source_ids, confidence=confidence, retention=retention_class)

    def export_bundle(self, output: str | Path, include_raw: bool = False) -> Path:
        return super().export_bundle(output, include_raw=include_raw)

    def import_bundle(self, input_path: str | Path, *, force: bool = False) -> int:
        return len(super().import_bundle(input_path, force=force))


@dataclass(frozen=True)
class MemoryItem:
    path: Path
    source_ids: tuple[str, ...] = ()
    confidence: str = "medium"
    retention_class: str = "normal"
    allowed_retrieval_sinks: tuple[str, ...] = ("memory",)
    ledger_action_id: str | None = None


@dataclass(frozen=True)
class MemorySearchResult:
    path: Path
    title: str
    snippet: str
    source_ids: tuple[str, ...]
    allowed_retrieval_sinks: tuple[str, ...]


class MemoryProvider(Protocol):
    def write_memory_item(
        self,
        *,
        text: str,
        source_ids: list[str],
        title: str | None = None,
        confidence: str = "medium",
        retention_class: str = "normal",
        allowed_retrieval_sinks: list[str] | None = None,
        origin: str = "operator",
        ledger_action_id: str | None = None,
    ) -> MemoryItem: ...

    def search_memory(self, query: str, *, sink: str = "memory", limit: int = 10) -> list[MemorySearchResult]: ...

    def export_bundle(self, output: str | Path, include_raw: bool = False) -> Path: ...

    def import_bundle(self, input_path: str | Path, *, force: bool = False) -> list[Path]: ...

    def forget(self, query: str, *, reason: str = "operator request", ledger_action_id: str | None = None) -> int: ...


@dataclass(frozen=True)
class ImportResult:
    imported_count: int


class WikiMemoryProvider:
    """Ledger-aware provider over the existing redacting Markdown wiki.

    Operator/local maintenance writes stay lightweight. Agent/Hermes/external
    writes are represented by an ``agent_memory_write`` ledger action before any
    page is changed, so uncertain learned memory cannot bypass the safety audit.
    """

    AGENT_ORIGINS = {"agent", "hermes", "research", "external", "messaging"}
    EXECUTABLE_STATES = {ApprovalState.DRY_RUN_READY.value, ApprovalState.APPROVED_FOR_LIVE.value, ApprovalState.EXECUTING.value}

    def __init__(self, *, wiki_dir: str | Path = "memory/wiki", ledger: ActionLedger | None = None) -> None:
        self.wiki = WikiMemory(wiki_dir)
        self.wiki_dir = self.wiki.wiki_dir
        self.ledger = ledger

    def write_memory_item(
        self,
        *,
        text: str,
        source_ids: list[str],
        title: str | None = None,
        confidence: str = "medium",
        retention_class: str = "normal",
        allowed_retrieval_sinks: list[str] | None = None,
        origin: str = "operator",
        ledger_action_id: str | None = None,
    ) -> MemoryItem:
        sinks = list(allowed_retrieval_sinks or ["memory"])
        page_title = title or "memory-" + "-".join(source_ids or [new_id("memorysrc")])
        action_id = self._ensure_agent_memory_action(
            title=page_title,
            text=text,
            source_ids=source_ids,
            origin=origin,
            ledger_action_id=ledger_action_id,
            allowed_sinks=sinks,
        )
        path = self.wiki.write_summary(
            page_title,
            text,
            source_ids=source_ids,
            confidence=confidence,
            retention=retention_class,
            allowed_sinks=sinks,
        )
        if action_id and self.ledger and self.ledger.state(action_id) in {ApprovalState.DRY_RUN_READY.value, ApprovalState.EXECUTING.value, ApprovalState.APPROVED_FOR_LIVE.value}:
            self.ledger.transition(
                action_id,
                ApprovalState.DRY_RUN_COMPLETED,
                actor="memory_provider",
                result={"path": str(path), "provider": "wiki_memory", "artifact_paths": [str(path)]},
            )
        return MemoryItem(path, tuple(source_ids), confidence, retention_class, tuple(sinks), action_id)

    def search_memory(self, query: str, *, sink: str = "memory", limit: int = 10) -> list[MemorySearchResult]:
        needle = query.lower()
        results: list[MemorySearchResult] = []
        for path in sorted(self.wiki_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            allowed = _frontmatter_list(text, "allowed_sinks") or ("memory",)
            if sink not in allowed:
                continue
            if needle and needle not in text.lower() and needle not in path.stem.lower():
                continue
            source_ids = _frontmatter_list(text, "source_ids")
            snippet = _snippet(text, needle)
            results.append(MemorySearchResult(path, path.stem, snippet, tuple(source_ids), tuple(allowed)))
            if len(results) >= limit:
                break
        return results

    def export_bundle(self, output: str | Path, include_raw: bool = False) -> Path:
        return self.wiki.export_bundle(output, include_raw=include_raw)

    def import_bundle(self, input_path: str | Path, *, force: bool = False) -> list[Path]:
        return self.wiki.import_bundle(input_path, force=force)

    def forget(self, query: str, *, reason: str = "operator request", ledger_action_id: str | None = None) -> int:
        action_id = self._ensure_agent_memory_action(
            title=f"forget-{query}",
            text=f"Forget request: {reason}",
            source_ids=["operator"],
            origin="operator" if ledger_action_id is None else "agent",
            ledger_action_id=ledger_action_id,
            allowed_sinks=["memory"],
        )
        count = 0
        for path in self.wiki_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            if query in text or query in path.stem:
                _atomic_write(path, text + f"\nTOMBSTONE: {reason}\n")
                count += 1
        if count == 0:
            self.wiki.forget(query, reason)
            count = 1
        if action_id and self.ledger and self.ledger.state(action_id) in self.EXECUTABLE_STATES:
            self.ledger.transition(action_id, ApprovalState.DRY_RUN_COMPLETED, actor="memory_provider", result={"forgotten": count})
        return count

    def _ensure_agent_memory_action(
        self,
        *,
        title: str,
        text: str,
        source_ids: list[str],
        origin: str,
        ledger_action_id: str | None,
        allowed_sinks: list[str],
    ) -> str | None:
        requires_ledger = origin.lower() in self.AGENT_ORIGINS or ledger_action_id is not None
        if not requires_ledger:
            return None
        if self.ledger is None:
            raise MemoryError("agent-origin memory writes require an action ledger")
        if ledger_action_id:
            try:
                env = self.ledger.get_action(ledger_action_id)
                state = self.ledger.state(ledger_action_id)
            except LedgerError as exc:
                raise MemoryError("memory write references an unknown ledger action") from exc
            if env.action_type != ActionType.AGENT_MEMORY_WRITE:
                raise MemoryError("memory write ledger action must be agent_memory_write")
            if state not in self.EXECUTABLE_STATES:
                raise MemoryError(f"memory write action is not executable from state {state}")
            return ledger_action_id
        provenance = SourceProvenance(SourceType.LOCAL_NOTE, TrustLevel.UNKNOWN, tuple(Sink(s) for s in allowed_sinks if s in {sink.value for sink in Sink}) or (Sink.MEMORY,))
        safe_text, redacted = redact_secrets(text)
        env = make_action(
            action_type=ActionType.AGENT_MEMORY_WRITE,
            platform=Platform.LOCAL,
            text=f"{title}\n\n{safe_text}",
            source_ids=source_ids or [new_id("memorysrc")],
            provenance=provenance,
            created_by=origin,
            metadata={"provider": "wiki_memory", "redaction_applied": redacted},
        )
        self.ledger.create_action(env, actor=origin)
        return env.action_id


class MemoryCompiler:
    def __init__(self, *, wiki_dir: str | Path, raw_dir: str | Path | None = None) -> None:
        self.wiki_dir = Path(wiki_dir)
        self.raw_dir = Path(raw_dir) if raw_dir is not None else self.wiki_dir.parent / "raw"
        self.wiki = WikiMemory(self.wiki_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def write_memory_item(self, *, source_ids: list[str], text: str, confidence: str, retention_class: str, allowed_retrieval_sinks: list[str]) -> MemoryItem:
        path = self.wiki.write_summary("memory-" + "-".join(source_ids), text, source_ids=source_ids, confidence=confidence, retention=retention_class, allowed_sinks=allowed_retrieval_sinks)
        return MemoryItem(path, tuple(source_ids), confidence, retention_class, tuple(allowed_retrieval_sinks))

    def forget(self, query: str) -> int:
        count = 0
        for path in self.wiki_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            if query in text:
                _atomic_write(path, text + "\nTOMBSTONE: forgotten by query.\n")
                count += 1
        if count == 0:
            self.wiki.forget(query)
            count = 1
        return count


def export_memory(*, wiki_dir: str | Path, output: str | Path) -> Path:
    return WikiMemory(wiki_dir).export_bundle(output)


def import_memory(*, input_path: str | Path, wiki_dir: str | Path, force: bool = False) -> ImportResult:
    return ImportResult(len(WikiMemory(wiki_dir).import_bundle(input_path, force=force)))


def _frontmatter_list(text: str, key: str) -> tuple[str, ...]:
    prefix = f"- {key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            raw = line.split(":", 1)[1].strip()
            return tuple(part.strip() for part in raw.split(",") if part.strip())
    return ()


def _snippet(text: str, needle: str, length: int = 180) -> str:
    compact = " ".join(text.split())
    if not needle:
        return compact[:length]
    idx = compact.lower().find(needle)
    if idx < 0:
        return compact[:length]
    start = max(0, idx - 40)
    return compact[start : start + length]


def slugify(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9가-힣_.-]+", "-", value.strip()).strip(".-")
    safe = safe.replace("..", "-")
    return safe or new_id("page")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
