from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig, PersonaConfig, reject_secret_value
from .memory import redact_secrets
from .models import stable_hash


@dataclass(frozen=True)
class PersonaProfile:
    name: str
    language: str
    voice: str
    humor_level: float
    sarcasm_level: float
    profanity_level: str
    version: str
    voice_guide: str = ""
    defaults: tuple[str, ...] = field(default_factory=tuple)
    taboo: tuple[str, ...] = field(default_factory=tuple)
    platform_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return stable_hash(self.prompt_context())

    def prompt_context(self, *, platform: str | None = None) -> str:
        parts = [
            f"persona.name={self.name}",
            f"persona.version={self.version}",
            f"language={self.language}",
            f"voice={self.voice}",
            f"humor_level={self.humor_level}",
            f"sarcasm_level={self.sarcasm_level}",
            f"profanity_level={self.profanity_level}",
        ]
        if self.defaults:
            parts.append("default_style=" + "; ".join(self.defaults))
        if self.taboo:
            parts.append("taboo=" + "; ".join(self.taboo))
        if platform and platform in self.platform_overrides:
            parts.append(f"platform_override[{platform}]=" + self.platform_overrides[platform])
        if self.voice_guide.strip():
            parts.append("voice_guide:\n" + self.voice_guide.strip())
        return "\n".join(parts)


DEFAULT_STYLE = (
    "짧고 자연스럽게 쓴다",
    "개발자/오픈소스/AI 밈은 허용하지만 과장 광고는 피한다",
    "확인되지 않은 사실은 단정하지 않는다",
    "외부 게시 전에는 항상 ledger dry-run preview를 만든다",
)

DEFAULT_TABOO = (
    "개인정보나 secret 노출",
    "괴롭힘/혐오/성적 콘텐츠",
    "출처 없는 확정 표현",
    "자동 좋아요/팔로우/스팸성 반복 답글",
)

DEFAULT_PLATFORM_OVERRIDES = {
    "x": "280자 안에서 훅이 먼저 보이게 쓴다. 긴 내용은 ledgered thread group으로 나눈다.",
    "threads": "대화체로 풀어 쓰되 여러 글은 순서 있는 thread group으로 만든다.",
    "instagram": "캡션형으로 덜 기술적으로 쓰고, 외부 링크/출처는 내부 ledger provenance에 남긴다.",
}


def load_persona(config: AppConfig) -> PersonaProfile:
    cfg = config.persona
    voice_guide = _safe_read_voice_guide(Path(cfg.voice_guide_path))
    return build_persona(cfg, voice_guide=voice_guide)


def build_persona(config: PersonaConfig, *, voice_guide: str = "") -> PersonaProfile:
    safe_guide, _ = redact_secrets(voice_guide)
    for label, value in (
        ("name", config.name),
        ("language", config.language),
        ("voice", config.voice),
        ("profanity_level", config.profanity_level),
        ("version", config.version),
    ):
        reject_secret_value(f"persona.{label}", str(value))
    return PersonaProfile(
        name=config.name,
        language=config.language,
        voice=config.voice,
        humor_level=float(config.humor_level),
        sarcasm_level=float(config.sarcasm_level),
        profanity_level=config.profanity_level,
        version=config.version,
        voice_guide=safe_guide,
        defaults=DEFAULT_STYLE,
        taboo=DEFAULT_TABOO,
        platform_overrides=dict(DEFAULT_PLATFORM_OVERRIDES),
    )


def render_persona_prompt(config: AppConfig, *, platform: str | None = None) -> dict[str, Any]:
    persona = load_persona(config)
    context = persona.prompt_context(platform=platform)
    return {
        "persona": persona.name,
        "version": persona.version,
        "digest": persona.digest,
        "platform": platform,
        "prompt_context": context,
    }


def _safe_read_voice_guide(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    safe, _ = redact_secrets(text)
    return safe
