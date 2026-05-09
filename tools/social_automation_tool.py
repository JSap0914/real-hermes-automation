"""Hermes social automation tool.

This is the first integration slice for the embedded local-first social agent.
It exposes a single action-oriented tool that talks to the vendored
``integrations/social_agent`` package while keeping all runtime state under the
active Hermes profile home.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from hermes_constants import display_hermes_home, get_hermes_home
from tools.registry import registry

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOCIAL_AGENT_SRC = _REPO_ROOT / "integrations" / "social_agent" / "src"


def _ensure_social_agent_path() -> None:
    src = str(_SOCIAL_AGENT_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)


def _runtime_paths() -> dict[str, Path]:
    base = get_hermes_home() / "social_automation"
    return {
        "base": base,
        "db": base / "social_agent.sqlite3",
        "wiki": base / "memory" / "wiki",
        "raw": base / "memory" / "raw",
        "artifacts": base / "artifacts",
    }


def _ensure_runtime_dirs() -> dict[str, Path]:
    paths = _runtime_paths()
    for key, path in paths.items():
        if key == "db":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return paths


def _load_runtime():
    _ensure_social_agent_path()
    from social_agent.config import AppConfig
    from social_agent.executor import Executor
    from social_agent.ledger import ActionLedger
    from social_agent.pipeline import DraftPipeline
    from social_agent.storage import migrate_database

    paths = _ensure_runtime_dirs()
    migrate_database(paths["db"])
    cfg = AppConfig(database_path=paths["db"])
    cfg.runtime.database_path = str(paths["db"])
    cfg.runtime.wiki_dir = str(paths["wiki"])
    cfg.runtime.raw_log_dir = str(paths["raw"])
    cfg.brain.artifact_dir = str(paths["artifacts"])
    cfg.runtime.dry_run_default = True
    ledger = ActionLedger(paths["db"])
    return paths, cfg, ledger, Executor, DraftPipeline


def _action_summary(envelope: Any) -> dict[str, Any]:
    return {
        "action_id": envelope.action_id,
        "action_type": envelope.action_type.value,
        "platform": envelope.platform,
        "state": envelope.state,
        "mode": envelope.mode,
        "target": envelope.target.to_dict(),
        "text": envelope.content.text,
        "created_by": envelope.created_by,
        "created_at": envelope.created_at,
        "metadata": dict(envelope.metadata),
    }


def _status() -> dict[str, Any]:
    paths, cfg, ledger, _Executor, _DraftPipeline = _load_runtime()
    return {
        "success": True,
        "action": "status",
        "home": display_hermes_home(),
        "database": str(paths["db"]),
        "wiki_dir": str(paths["wiki"]),
        "dry_run_default": cfg.runtime.dry_run_default,
        "paused": cfg.runtime.paused,
        "live_enabled": False,
        "status": ledger.status(),
        "adapters": {
            name: {
                "enabled": adapter.enabled,
                "live_enabled": adapter.live_enabled,
                "ready_for_live": adapter.ready_for_live(),
            }
            for name, adapter in cfg.adapters.items()
        },
    }


def _migrate() -> dict[str, Any]:
    paths, _cfg, ledger, _Executor, _DraftPipeline = _load_runtime()
    return {
        "success": True,
        "action": "migrate",
        "database": str(paths["db"]),
        "status": ledger.status(),
    }


def _propose(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text") or "").strip()
    if not text:
        return {"success": False, "error": "text is required for propose"}
    platform = str(args.get("platform") or "x").strip().lower()
    if platform not in {"x", "threads", "instagram"}:
        return {"success": False, "error": "platform must be one of: x, threads, instagram"}
    source_url = str(args.get("source_url") or "https://example.com/manual")
    title = str(args.get("title") or "manual proposal")
    render_preview = bool(args.get("render_preview", True))

    paths, cfg, ledger, _Executor, DraftPipeline = _load_runtime()
    pipeline = DraftPipeline(config=cfg, db_path=paths["db"], wiki_dir=paths["wiki"])
    result = (
        pipeline.ingest_public_source(url=source_url, title=title, text=text)
        .generate_korean_drafts(tone=str(args.get("tone") or "casual_comedic"), count=1)
        .create_dry_run_action(platform=platform)
    )
    if render_preview:
        result.render_preview()
    state = ledger.state(result.action_id) if result.action_id else (result.envelope.state if result.envelope else None)
    return {
        "success": True,
        "action": "propose",
        "action_id": result.action_id,
        "platform": platform,
        "state": state,
        "drafts": result.drafts,
        "preview": result.preview,
        "network_write": False,
        "database": str(paths["db"]),
    }


def _run_once() -> dict[str, Any]:
    paths, cfg, ledger, Executor, _DraftPipeline = _load_runtime()
    results = Executor(ledger, cfg).run_once()
    return {
        "success": True,
        "action": "run_once",
        "results": results,
        "status": ledger.status(),
        "network_write": False,
        "database": str(paths["db"]),
    }


def _list_actions(args: dict[str, Any]) -> dict[str, Any]:
    _paths, _cfg, ledger, _Executor, _DraftPipeline = _load_runtime()
    limit = int(args.get("limit") or 20)
    state = args.get("state")
    states = [str(state)] if state else None
    actions = ledger.list_actions(states=states, limit=limit)
    return {
        "success": True,
        "action": "list_actions",
        "items": [_action_summary(item) for item in actions],
    }


def _preview(args: dict[str, Any]) -> dict[str, Any]:
    action_id = str(args.get("action_id") or "").strip()
    if not action_id:
        return {"success": False, "error": "action_id is required for preview"}
    _paths, cfg, ledger, Executor, _DraftPipeline = _load_runtime()
    result = Executor(ledger, cfg).execute(action_id)
    return {"success": True, "action": "preview", "result": result, "network_write": False}


def _why(args: dict[str, Any]) -> dict[str, Any]:
    action_id = str(args.get("action_id") or "").strip()
    if not action_id:
        return {"success": False, "error": "action_id is required for why"}
    _paths, _cfg, ledger, _Executor, _DraftPipeline = _load_runtime()
    return {"success": True, "action": "why", "why": ledger.why(action_id)}


def _approve(args: dict[str, Any]) -> dict[str, Any]:
    action_id = str(args.get("action_id") or "").strip()
    if not action_id:
        return {"success": False, "error": "action_id is required for approve"}
    approver = str(args.get("approver") or "operator")
    _paths, _cfg, ledger, _Executor, _DraftPipeline = _load_runtime()
    updated = ledger.transition(action_id, "approved_for_live", approver=approver, event_type="operator_approved")
    return {
        "success": True,
        "action": "approve",
        "item": _action_summary(updated),
        "note": "Approval only updates the ledger. Live execution remains blocked unless an adapter is explicitly live-enabled and readiness-gated.",
    }


def social_automation(args: dict[str, Any] | None = None, task_id: str | None = None) -> str:
    """Run safe social automation actions against the Hermes profile ledger."""
    args = dict(args or {})
    action = str(args.get("action") or "status").strip().lower().replace("-", "_")
    try:
        if action == "status":
            payload = _status()
        elif action == "migrate":
            payload = _migrate()
        elif action == "propose":
            payload = _propose(args)
        elif action == "run_once":
            payload = _run_once()
        elif action in {"list", "list_actions"}:
            payload = _list_actions(args)
        elif action == "preview":
            payload = _preview(args)
        elif action == "why":
            payload = _why(args)
        elif action == "approve":
            payload = _approve(args)
        else:
            payload = {"success": False, "error": f"unknown social automation action: {action}"}
    except Exception as exc:
        payload = {"success": False, "error": str(exc), "action": action}
    if task_id:
        payload.setdefault("task_id", task_id)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


SOCIAL_AUTOMATION_SCHEMA = {
    "name": "social_automation",
    "description": "Profile-safe social automation ledger for dry-run post proposals, previews, approvals, status, and safe run-once execution. Defaults to no live network writes.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "migrate", "propose", "run_once", "list_actions", "preview", "why", "approve"],
                "description": "Operation to perform. Use propose for new dry-run social drafts, preview to render a queued action, and why for audit details.",
            },
            "text": {"type": "string", "description": "Source text or instruction for propose."},
            "source_url": {"type": "string", "description": "Public source URL for provenance when proposing."},
            "title": {"type": "string", "description": "Source title for provenance when proposing."},
            "platform": {"type": "string", "enum": ["x", "threads", "instagram"], "description": "Social platform for proposed dry-run content."},
            "tone": {"type": "string", "description": "Draft tone. Defaults to casual_comedic."},
            "render_preview": {"type": "boolean", "description": "When true, immediately render and ledger a dry-run preview after proposing."},
            "action_id": {"type": "string", "description": "Ledger action ID for preview, why, or approve."},
            "approver": {"type": "string", "description": "Operator label recorded for approve."},
            "state": {"type": "string", "description": "Optional state filter for list_actions."},
            "limit": {"type": "integer", "description": "Maximum actions to return for list_actions."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="social_automation",
    toolset="social_automation",
    schema=SOCIAL_AUTOMATION_SCHEMA,
    handler=lambda args, **kw: social_automation(args, task_id=kw.get("task_id")),
    check_fn=lambda: _SOCIAL_AGENT_SRC.exists(),
    emoji="📣",
)
