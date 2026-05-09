from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import AppConfig
from .ledger import ActionLedger
from .persona import load_persona


REQUIRED_READINESS = (
    "docs_reviewed",
    "scopes_documented",
    "mocked_tests_passed",
    "dry_run_preview_passed",
    "policy_wired",
    "rate_limits_configured",
    "idempotency_tests_passed",
    "manual_live_enable",
)


def run_doctor(config: AppConfig) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    db_path = Path(config.runtime.database_path)
    unsafe = any(part.lower() in {"onedrive", "dropbox", "google drive"} for part in db_path.parts)
    add("sqlite_path", not unsafe, "unsafe synced/network path" if unsafe else str(db_path))
    try:
        ActionLedger(db_path)
        add("sqlite_migration", True, "schema created/verified")
    except Exception as exc:  # pragma: no cover - surfaced in CLI smoke
        add("sqlite_migration", False, repr(exc))
    add("dry_run_default", config.runtime.dry_run_default, "dry-run is default" if config.runtime.dry_run_default else "dry-run disabled")
    add("live_adapters_disabled", not any(a.live_enabled for a in config.adapters.values()), "no adapter live_enabled by default")
    add("telegram_credentials", bool(os.environ.get("TELEGRAM_BOT_TOKEN")), "missing is ok for dry-run" if not os.environ.get("TELEGRAM_BOT_TOKEN") else "present")
    add("discord_credentials", bool(os.environ.get("DISCORD_BOT_TOKEN")), "missing is ok for dry-run" if not os.environ.get("DISCORD_BOT_TOKEN") else "present")
    for provider_name in ("fake_hermes", "codex_cli", "claude_cli", "hermes"):
        provider = config.providers.get(provider_name)
        if not provider:
            continue
        executable = provider.command[0] if provider.command else provider_name.removesuffix("_cli")
        found = shutil.which(str(executable)) is not None
        detail = "disabled" if not provider.enabled else ("command available" if found or provider_name == "fake_hermes" else f"command not found: {executable}")
        add(f"{provider_name}_provider", (not provider.enabled) or found or provider_name == "fake_hermes", detail)
    for adapter_name in ("x", "threads", "instagram"):
        adapter = config.adapter(adapter_name)
        missing = [flag for flag in REQUIRED_READINESS if not adapter.readiness.get(flag, False)]
        credential_ok = bool(adapter.credential_profile_id)
        if adapter.live_enabled:
            add(f"{adapter_name}_credential_profile", credential_ok, "profile id present" if credential_ok else "live enabled but credential profile missing")
        add(
            f"{adapter_name}_readiness",
            adapter.ready_for_live(),
            "ready for live" if adapter.ready_for_live() else "live gated until: " + ", ".join(missing or ["adapter disabled/live_disabled"]),
        )
    add("command_surface", bool(config.adapter("telegram").enabled or config.adapter("discord").enabled), "telegram/discord control plane configured")
    persona = load_persona(config)
    add("persona_profile", True, f"{persona.name}@{persona.version} digest={persona.digest[:12]}")
    return {"ok": all(c["ok"] or "missing is ok" in c["detail"] or "live gated" in c["detail"] for c in checks), "checks": checks}
