# Local Autonomous Social + Discord Agent Orchestrator

A local-first Python 3.12 daemon for drafting Korean casual/comedic social posts, managing approvals, and orchestrating Discord coding-agent tasks through an append-only SQLite action ledger.

## Safety defaults

- Dry-run by default.
- No external side effect can execute unless represented as an action envelope in the SQLite ledger.
- Live X/Threads/Instagram paths exist only behind adapter readiness and `live_enabled` gates.
- Discord automation uses bot/channel allowlists only; no selfbots or arbitrary shell commands.
- Public posts require source provenance and policy checks.
- Memory, research, and messaging providers store profile IDs/artifact paths only; OAuth/API/cookie secrets are rejected or redacted.
- Agent/Hermes-origin memory writes are ledgered as `agent_memory_write` actions, and messaging ingress becomes `control_command` actions before tasks run.
- Optional Agent Messenger / Hermes messaging wrappers are disabled by default; desktop-token extraction, cookie browsing, private-session writes, and anti-bot bypass modes stay off.

## Fresh clone setup

```bash
git clone <repo>
cd <repo>
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m social_agent migrate
python -m social_agent doctor
python -m social_agent run --dry-run
```

## Common commands

```bash
python -m social_agent migrate --db .local/social_agent.sqlite3
python -m social_agent doctor
python -m social_agent propose --text "오픈소스 릴리스 소식" --source-url https://example.com --dry-run
python -m social_agent run --dry-run --once
python -m social_agent export-memory --output memory-export.json
python -m social_agent import-memory --input memory-export.json
```

## Live adapters

Live social posting is disabled until all readiness checks are true and the operator enables a specific adapter. X/Threads `publish_post`/`reply_to_post` and Instagram `publish_post` use the same ledger, approval, readiness, idempotency, and policy paths.
