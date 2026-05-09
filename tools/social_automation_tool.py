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


def _readiness(args: dict[str, Any]) -> dict[str, Any]:
    _ensure_social_agent_path()
    from social_agent.adapters import READINESS_FLAGS, SAFE_ENV_RE

    requested = str(args.get("platform") or "all").strip().lower()
    platforms = ["x", "threads", "instagram"] if requested in {"", "all"} else [requested]
    invalid = sorted(set(platforms) - {"x", "threads", "instagram"})
    if invalid:
        return {"success": False, "error": f"unsupported readiness platform(s): {', '.join(invalid)}"}

    _paths, cfg, _ledger, _Executor, _DraftPipeline = _load_runtime()
    guides = {}
    for platform in platforms:
        adapter = cfg.adapter(platform)
        profile = str(args.get("credential_profile_id") or adapter.credential_profile_id or f"{platform}-operator")
        env_name = "SOCIAL_AGENT_CREDENTIAL_" + SAFE_ENV_RE.sub("_", profile.upper())
        guides[platform] = {
            "enabled": adapter.enabled,
            "live_enabled": adapter.live_enabled,
            "ready_for_live": adapter.ready_for_live(),
            "credential_profile_id": profile,
            "credential_env_var": env_name,
            "required_flags": list(READINESS_FLAGS),
            "missing_flags": [flag for flag in READINESS_FLAGS if not adapter.readiness.get(flag, False)],
            "live_write_policy": "blocked until adapter.enabled, adapter.live_enabled, every readiness flag, ledger approval, and credential env var are all present",
        }
    return {
        "success": True,
        "action": "readiness",
        "platforms": guides,
        "network_write": False,
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


def _split_post_texts(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or "")
    # Stable slash-command delimiter for multi-post threads/campaigns.
    parts = [part.strip() for part in raw.split("||")]
    return [part for part in parts if part]


def _source_for_thread(ledger: Any, *, text: str, source_url: str, title: str) -> str:
    _ensure_social_agent_path()
    from social_agent.models import Sink, SourceProvenance, SourceType, TrustLevel, new_id

    source_id = new_id("src")
    provenance = SourceProvenance(SourceType.PUBLIC_WEB, TrustLevel.VERIFIED, (Sink.MEMORY, Sink.DRAFT, Sink.PUBLIC_POST), source_url=source_url, title=title)
    ledger.append_source(
        source_id=source_id,
        provenance=provenance.to_dict(),
        content=text,
        url=source_url,
        title=title,
    )
    return source_id


def _plan_thread(args: dict[str, Any]) -> dict[str, Any]:
    _ensure_social_agent_path()
    from social_agent.threading import SocialThreadPlanner

    platform = str(args.get("platform") or "x").strip().lower()
    if platform not in {"x", "threads", "instagram"}:
        return {"success": False, "error": "platform must be one of: x, threads, instagram"}
    texts = _split_post_texts(args.get("texts") if "texts" in args else args.get("text"))
    if not texts:
        return {"success": False, "error": "thread requires text or texts"}
    account_id = str(args.get("account_id") or f"{platform}-dry-run")
    source_url = str(args.get("source_url") or "https://example.com/manual-thread")
    title = str(args.get("title") or "manual thread proposal")
    render_preview = bool(args.get("render_preview", True))

    paths, cfg, ledger, Executor, _DraftPipeline = _load_runtime()
    source_id = _source_for_thread(ledger, text="\n\n".join(texts), source_url=source_url, title=title)
    plan = SocialThreadPlanner(ledger).create_thread(
        platform=platform,
        account_id=account_id,
        texts=texts,
        source_ids=[source_id],
        created_by="social_automation",
        mode="dry_run",
    )
    previews = [Executor(ledger, cfg).execute(item.action_id) for item in plan.items] if render_preview else []
    return {
        "success": True,
        "action": "thread",
        "group_id": plan.group_id,
        "platform": plan.platform,
        "total": plan.total,
        "items": [item.__dict__ for item in plan.items],
        "previews": previews,
        "network_write": False,
        "database": str(paths["db"]),
    }


def _plan_campaign(args: dict[str, Any]) -> dict[str, Any]:
    _ensure_social_agent_path()
    from social_agent.threading import SocialThreadPlanner

    raw_posts = args.get("posts")
    if not isinstance(raw_posts, dict):
        return {"success": False, "error": "campaign requires posts as a platform-to-texts object"}
    posts = {str(platform).lower(): _split_post_texts(texts) for platform, texts in raw_posts.items()}
    posts = {platform: texts for platform, texts in posts.items() if texts}
    invalid = sorted(set(posts) - {"x", "threads", "instagram"})
    if invalid:
        return {"success": False, "error": f"unsupported campaign platform(s): {', '.join(invalid)}"}
    if not posts:
        return {"success": False, "error": "campaign requires at least one non-empty post"}
    account_ids = {platform: str(args.get("account_ids", {}).get(platform) if isinstance(args.get("account_ids"), dict) else "") or f"{platform}-dry-run" for platform in posts}
    source_url = str(args.get("source_url") or "https://example.com/manual-campaign")
    title = str(args.get("title") or "manual campaign proposal")
    render_preview = bool(args.get("render_preview", True))

    paths, cfg, ledger, Executor, _DraftPipeline = _load_runtime()
    source_text = "\n\n".join(text for texts in posts.values() for text in texts)
    source_id = _source_for_thread(ledger, text=source_text, source_url=source_url, title=title)
    plan = SocialThreadPlanner(ledger).create_campaign(
        posts=posts,
        account_ids=account_ids,
        source_ids=[source_id],
        created_by="social_automation",
        mode="dry_run",
    )
    previews = [Executor(ledger, cfg).execute(item.action_id) for item in plan.items] if render_preview else []
    return {
        "success": True,
        "action": "campaign",
        "group_id": plan.group_id,
        "platform": plan.platform,
        "total": plan.total,
        "items": [item.__dict__ for item in plan.items],
        "previews": previews,
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


def parse_social_command_args(raw_args: str) -> dict[str, Any]:
    """Parse `/social ...` slash-command arguments into tool arguments.

    This intentionally supports a small, stable control-plane vocabulary for
    Telegram/Discord/CLI users.  Richer planning still goes through the agent
    loop, which can call the same tool schema directly.
    """
    text = (raw_args or "").strip()
    if not text:
        return {"action": "status"}

    parts = text.split(maxsplit=1)
    subcommand = parts[0].lower().replace("-", "_")
    rest = parts[1].strip() if len(parts) > 1 else ""

    aliases = {
        "list": "list_actions",
        "ls": "list_actions",
        "run": "run_once",
        "run_once": "run_once",
        "runonce": "run_once",
        "post": "propose",
        "draft": "propose",
        "threads": "thread",
        "campaigns": "campaign",
        "ready": "readiness",
        "readiness": "readiness",
        "audit": "why",
    }
    action = aliases.get(subcommand, subcommand)

    if action in {"status", "migrate", "run_once", "readiness", "list_actions"}:
        args: dict[str, Any] = {"action": action}
        if action == "list_actions" and rest:
            args["state"] = rest
        if action == "readiness" and rest:
            args["platform"] = rest
        return args

    if action in {"preview", "why", "approve"}:
        if not rest:
            return {"action": action, "error": f"{action} requires an action_id"}
        first, _, tail = rest.partition(" ")
        args = {"action": action, "action_id": first}
        if action == "approve" and tail.strip():
            args["approver"] = tail.strip()
        return args

    if action == "propose":
        platform = "x"
        body = rest
        body_parts = rest.split(maxsplit=1)
        if body_parts and body_parts[0].lower() in {"x", "threads", "instagram"}:
            platform = body_parts[0].lower()
            body = body_parts[1].strip() if len(body_parts) > 1 else ""
        return {
            "action": "propose",
            "platform": platform,
            "text": body,
            "render_preview": True,
        }

    if action == "thread":
        platform = "x"
        body = rest
        body_parts = rest.split(maxsplit=1)
        if body_parts and body_parts[0].lower() in {"x", "threads", "instagram"}:
            platform = body_parts[0].lower()
            body = body_parts[1].strip() if len(body_parts) > 1 else ""
        return {
            "action": "thread",
            "platform": platform,
            "text": body,
            "render_preview": True,
        }

    if action == "campaign":
        posts: dict[str, str] = {}
        for chunk in [part.strip() for part in rest.split(";") if part.strip()]:
            platform, sep, text = chunk.partition("=")
            if not sep:
                platform, sep, text = chunk.partition(":")
            if sep and platform.strip().lower() in {"x", "threads", "instagram"}:
                posts[platform.strip().lower()] = text.strip()
        return {
            "action": "campaign",
            "posts": posts,
            "render_preview": True,
        }

    return {"action": action}


def format_social_result(payload: dict[str, Any]) -> str:
    """Return a concise human-readable result for slash-command surfaces."""
    if not payload.get("success"):
        return f"📣 Social automation error: {payload.get('error', 'unknown error')}"

    action = payload.get("action")
    if action == "status":
        status = payload.get("status") or {}
        counts = ", ".join(f"{key}={value}" for key, value in sorted(status.items())) or "empty"
        return (
            "📣 **Social automation status**\n"
            f"DB: `{payload.get('database')}`\n"
            f"Dry-run default: `{payload.get('dry_run_default')}`\n"
            f"Queue: {counts}"
        )
    if action == "migrate":
        return f"📣 Social automation ledger ready: `{payload.get('database')}`"
    if action == "readiness":
        lines = ["📣 **Social automation readiness**"]
        for platform, guide in sorted((payload.get("platforms") or {}).items()):
            lines.append(
                f"- `{platform}` ready=`{guide.get('ready_for_live')}` "
                f"env=`{guide.get('credential_env_var')}` missing=`{len(guide.get('missing_flags') or [])}`"
            )
        return "\n".join(lines)
    if action == "propose":
        preview = payload.get("preview") or {}
        return (
            "📣 **Dry-run social proposal created**\n"
            f"Action: `{payload.get('action_id')}`\n"
            f"Platform: `{payload.get('platform')}`\n"
            f"State: `{payload.get('state')}`\n"
            f"Network write: `{preview.get('network_write', payload.get('network_write'))}`\n"
            f"Draft: {((payload.get('drafts') or [''])[0])}"
        )
    if action == "run_once":
        return f"📣 Social automation run-once completed: {len(payload.get('results') or [])} result(s), network_write=`{payload.get('network_write')}`"
    if action in {"thread", "campaign"}:
        return (
            f"📣 Social automation {action} planned: group=`{payload.get('group_id')}` "
            f"items=`{payload.get('total')}` previews=`{len(payload.get('previews') or [])}` "
            f"network_write=`{payload.get('network_write')}`"
        )
    if action == "list_actions":
        items = payload.get("items") or []
        if not items:
            return "📣 No social automation actions found."
        lines = ["📣 **Social automation actions**"]
        for item in items[:10]:
            lines.append(f"- `{item['action_id']}` {item['platform']}/{item['action_type']} state=`{item['state']}`")
        return "\n".join(lines)
    if action == "preview":
        result = payload.get("result") or {}
        return f"📣 Preview rendered for `{result.get('action_id')}` state=`{result.get('state')}` network_write=`{payload.get('network_write')}`"
    if action == "why":
        why = payload.get("why") or {}
        approval = why.get("approval") or {}
        return (
            "📣 **Social action audit**\n"
            f"Action: `{why.get('action_id')}`\n"
            f"State: `{approval.get('state')}`\n"
            f"Events: `{len(why.get('events') or [])}`"
        )
    if action == "approve":
        item = payload.get("item") or {}
        return f"📣 Approved `{item.get('action_id')}` into state `{item.get('state')}`. {payload.get('note')}"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def social_automation(args: dict[str, Any] | None = None, task_id: str | None = None) -> str:
    """Run safe social automation actions against the Hermes profile ledger."""
    args = dict(args or {})
    action = str(args.get("action") or "status").strip().lower().replace("-", "_")
    try:
        if action == "status":
            payload = _status()
        elif action == "readiness":
            payload = _readiness(args)
        elif action == "migrate":
            payload = _migrate()
        elif action == "propose":
            payload = _propose(args)
        elif action == "run_once":
            payload = _run_once()
        elif action == "thread":
            payload = _plan_thread(args)
        elif action == "campaign":
            payload = _plan_campaign(args)
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
                "enum": ["status", "readiness", "migrate", "propose", "run_once", "thread", "campaign", "list_actions", "preview", "why", "approve"],
                "description": "Operation to perform. Use propose for new dry-run social drafts, preview to render a queued action, and why for audit details.",
            },
            "text": {"type": "string", "description": "Source text or instruction for propose."},
            "texts": {"type": "array", "items": {"type": "string"}, "description": "Ordered post texts for thread planning."},
            "posts": {"type": "object", "description": "Campaign posts keyed by platform, each value a string with || delimiters or a list of strings."},
            "account_id": {"type": "string", "description": "Optional platform account/user id for thread planning. Defaults to dry-run placeholder."},
            "account_ids": {"type": "object", "description": "Optional campaign account ids keyed by platform."},
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
