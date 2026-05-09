from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import load_config
from .doctor import run_doctor
from .executor import Executor
from .improvement import ImprovementProposalStore, OutcomeCollector
from .ledger import ActionLedger
from .memory import MemoryStore
from .persona import render_persona_prompt
from .pipeline import DraftPipeline
from .storage import migrate_database


def _emit(payload: dict, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(" ".join(f"{k}={v}" for k, v in payload.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="social_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_p = sub.add_parser("migrate")
    migrate_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    migrate_p.add_argument("--json", action="store_true")

    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    doctor_p.add_argument("--config", default=os.environ.get("SOCIAL_AGENT_CONFIG", "config.example.yaml"))
    doctor_p.add_argument("--json", action="store_true")

    run_p = sub.add_parser("run")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--once", action="store_true")
    run_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    run_p.add_argument("--json", action="store_true")

    propose_p = sub.add_parser("propose")
    propose_p.add_argument("--text", required=True)
    propose_p.add_argument("--source-url", default="https://example.com/manual")
    propose_p.add_argument("--dry-run", action="store_true")
    propose_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    propose_p.add_argument("--json", action="store_true")

    export_p = sub.add_parser("export-memory")
    export_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    export_p.add_argument("--output", required=True)
    export_p.add_argument("--json", action="store_true")

    import_p = sub.add_parser("import-memory")
    import_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    import_p.add_argument("--input", required=True)
    import_p.add_argument("--force", action="store_true")
    import_p.add_argument("--json", action="store_true")

    persona_p = sub.add_parser("persona")
    persona_p.add_argument("--config", default=os.environ.get("SOCIAL_AGENT_CONFIG", "config.example.yaml"))
    persona_p.add_argument("--platform", default=None)
    persona_p.add_argument("--json", action="store_true")

    outcome_p = sub.add_parser("record-outcome")
    outcome_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    outcome_p.add_argument("--action-id", required=True)
    outcome_p.add_argument("--metric", action="append", default=[], help="key=value metric; repeatable")
    outcome_p.add_argument("--notes", default="")
    outcome_p.add_argument("--json", action="store_true")

    improve_p = sub.add_parser("propose-improvement")
    improve_p.add_argument("--db", default=os.environ.get("SOCIAL_AGENT_DB", ".local/social_agent.sqlite3"))
    improve_p.add_argument("--title", default="engagement-learning")
    improve_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate_database(args.db)
        _emit({"ok": True, "command": "migrate", "database": str(args.db)}, True)
        return 0
    if args.command == "doctor":
        cfg = load_config(args.config)
        cfg.runtime.database_path = str(args.db)
        migrate_database(args.db)
        missing = [name for name in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "X_BEARER_TOKEN", "THREADS_ACCESS_TOKEN", "INSTAGRAM_ACCESS_TOKEN") if not os.environ.get(name)]
        adapters = {name: ("ready" if cfg.adapter(name).can_live else "live disabled/not ready") for name in ("x", "threads", "instagram", "discord", "telegram")}
        diagnosis = run_doctor(cfg)
        payload = {"ok": diagnosis["ok"], "command": "doctor", "database": str(args.db), "credentials": f"missing: {', '.join(missing) if missing else 'none'}", "live": adapters, "disabled": [k for k, v in adapters.items() if "disabled" in v or "not ready" in v], "checks": diagnosis["checks"]}
        _emit(payload, True)
        return 0
    if args.command == "run":
        cfg = load_config()
        cfg.runtime.database_path = str(args.db)
        ledger = ActionLedger(args.db)
        results = Executor(ledger, cfg).run_once()
        mode = "dry-run" if args.dry_run or cfg.runtime.dry_run_default or os.environ.get("SOCIAL_AGENT_LIVE_ENABLED", "false").lower() != "true" else "live"
        _emit({"ok": True, "command": "run", "mode": mode, "dry": mode == "dry-run", "once": bool(args.once), "results": len(results)}, True)
        return 0
    if args.command == "propose":
        pipeline = DraftPipeline(db_path=args.db, wiki_dir="memory/wiki")
        result = pipeline.ingest_public_source(url=args.source_url, title="manual proposal", text=args.text).generate_korean_drafts(tone="casual_comedic", count=1).create_dry_run_action(platform="x")
        _emit({"ok": True, "command": "propose", "action_id": result.action_id, "dry_run": bool(args.dry_run)}, True)
        return 0
    if args.command == "export-memory":
        path = MemoryStore(ActionLedger(args.db), "memory/wiki").export_bundle(args.output)
        _emit({"ok": True, "command": "export-memory", "output": str(path)}, True)
        return 0
    if args.command == "import-memory":
        count = MemoryStore(ActionLedger(args.db), "memory/wiki").import_bundle(args.input, force=args.force)
        _emit({"ok": True, "command": "import-memory", "imported": count}, True)
        return 0
    if args.command == "persona":
        cfg = load_config(args.config)
        _emit({"ok": True, "command": "persona", **render_persona_prompt(cfg, platform=args.platform)}, True)
        return 0
    if args.command == "record-outcome":
        metrics: dict[str, object] = {}
        for pair in args.metric:
            if "=" not in pair:
                raise SystemExit(f"invalid --metric {pair!r}; expected key=value")
            key, value = pair.split("=", 1)
            try:
                metrics[key] = float(value)
            except ValueError:
                metrics[key] = value
        outcome = OutcomeCollector(ActionLedger(args.db)).record(action_id=args.action_id, metrics=metrics, notes=args.notes)
        _emit({"ok": True, "command": "record-outcome", "outcome_id": outcome.outcome_id, "action_id": outcome.action_id}, True)
        return 0
    if args.command == "propose-improvement":
        proposal = ImprovementProposalStore(ActionLedger(args.db)).propose_from_outcomes(title=args.title)
        _emit({"ok": True, "command": "propose-improvement", "proposal_id": proposal.proposal_id, "status": proposal.status}, True)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
