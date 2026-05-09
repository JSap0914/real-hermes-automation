from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  provenance_json TEXT,
  raw_text TEXT,
  source_type TEXT,
  trust_level TEXT,
  url TEXT,
  title TEXT,
  text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  platform TEXT NOT NULL,
  state TEXT NOT NULL,
  mode TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  provider_name TEXT,
  provider_version TEXT,
  prompt_digest TEXT,
  result_digest TEXT,
  artifact_paths_json TEXT DEFAULT '[]',
  allowed_tools_json TEXT DEFAULT '[]',
  credential_profile_id TEXT,
  correlation_id TEXT,
  envelope_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adapter_results (
  action_id TEXT PRIMARY KEY,
  adapter TEXT NOT NULL,
  remote_id TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_groups (
  group_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by TEXT NOT NULL,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_group_items (
  group_id TEXT NOT NULL,
  action_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  sequence_index INTEGER NOT NULL,
  sequence_total INTEGER NOT NULL,
  predecessor_action_id TEXT,
  dependency_policy TEXT NOT NULL DEFAULT 'block_if_predecessor_missing',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY (group_id, action_id),
  UNIQUE (group_id, sequence_index)
);
CREATE TABLE IF NOT EXISTS inspirations (
  inspiration_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
  memory_id TEXT PRIMARY KEY,
  source_ids_json TEXT NOT NULL,
  path TEXT NOT NULL,
  confidence TEXT NOT NULL,
  retention_class TEXT NOT NULL,
  allowed_retrieval_sinks_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome_events (
  outcome_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  notes TEXT,
  collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS improvement_proposals (
  proposal_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  suggested_update TEXT NOT NULL,
  risk TEXT NOT NULL,
  rollback_path TEXT NOT NULL,
  target_path TEXT NOT NULL,
  ledger_action_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tasks (
  task_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL,
  agent_name TEXT,
  channel_id TEXT,
  target_channel TEXT,
  repo TEXT,
  status TEXT NOT NULL,
  provider_name TEXT,
  provider_version TEXT,
  provider_profile_id TEXT,
  prompt_digest TEXT,
  result_digest TEXT,
  allowed_tools_json TEXT DEFAULT '[]',
  artifact_paths_json TEXT DEFAULT '[]',
  provenance_json TEXT DEFAULT '{}',
  requester TEXT,
  correlation_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  transcript_json TEXT DEFAULT '[]',
  transcript TEXT DEFAULT '',
  result_summary TEXT DEFAULT '',
  timeout_seconds INTEGER DEFAULT 1800,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  action_id TEXT NOT NULL,
  path TEXT NOT NULL,
  digest TEXT NOT NULL,
  artifact_type TEXT NOT NULL DEFAULT 'result',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_provider_tasks (
  task_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL,
  provider_name TEXT NOT NULL,
  provider_version TEXT,
  status TEXT NOT NULL,
  prompt_digest TEXT,
  result_digest TEXT,
  artifact_paths_json TEXT DEFAULT '[]',
  allowed_tools_json TEXT DEFAULT '[]',
  credential_profile_id TEXT,
  correlation_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adapter_readiness (
  adapter TEXT PRIMARY KEY,
  docs_reviewed INTEGER NOT NULL DEFAULT 0,
  scopes_documented INTEGER NOT NULL DEFAULT 0,
  mocked_tests_passed INTEGER NOT NULL DEFAULT 0,
  dry_run_preview_passed INTEGER NOT NULL DEFAULT 0,
  policy_wired INTEGER NOT NULL DEFAULT 0,
  rate_limits_configured INTEGER NOT NULL DEFAULT 0,
  idempotency_tests_passed INTEGER NOT NULL DEFAULT 0,
  manual_live_enable INTEGER NOT NULL DEFAULT 0,
  reviewed_url TEXT,
  reviewed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_completed_live_idempotency ON actions(idempotency_key) WHERE mode = 'live' AND state = 'completed';
INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
"""

MIGRATION_COLUMNS = {
    "actions": {
        "provider_name": "TEXT",
        "provider_version": "TEXT",
        "prompt_digest": "TEXT",
        "result_digest": "TEXT",
        "artifact_paths_json": "TEXT DEFAULT '[]'",
        "allowed_tools_json": "TEXT DEFAULT '[]'",
        "credential_profile_id": "TEXT",
        "correlation_id": "TEXT",
    },
    "agent_tasks": {
        "provider_name": "TEXT",
        "provider_version": "TEXT",
        "prompt_digest": "TEXT",
        "result_digest": "TEXT",
        "artifact_paths_json": "TEXT DEFAULT '[]'",
        "allowed_tools_json": "TEXT DEFAULT '[]'",
        "credential_profile_id": "TEXT",
        "correlation_id": "TEXT",
    },
}


AGENT_TASK_COLUMNS = {
    "provider_name": "TEXT",
    "provider_version": "TEXT",
    "provider_profile_id": "TEXT",
    "prompt_digest": "TEXT",
    "result_digest": "TEXT",
    "allowed_tools_json": "TEXT DEFAULT '[]'",
    "artifact_paths_json": "TEXT DEFAULT '[]'",
    "provenance_json": "TEXT DEFAULT '{}'",
    "requester": "TEXT",
    "correlation_id": "TEXT",
    "metadata_json": "TEXT DEFAULT '{}'",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations that CREATE TABLE IF NOT EXISTS cannot cover."""
    for table, columns in MIGRATION_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()}
    for column, definition in AGENT_TASK_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_artifacts (
          artifact_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          action_id TEXT NOT NULL,
          path TEXT NOT NULL,
          digest TEXT NOT NULL,
          artifact_type TEXT NOT NULL DEFAULT 'result',
          metadata_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_provider_tasks (
          task_id TEXT PRIMARY KEY,
          action_id TEXT NOT NULL,
          provider_name TEXT NOT NULL,
          provider_version TEXT,
          status TEXT NOT NULL,
          prompt_digest TEXT,
          result_digest TEXT,
          artifact_paths_json TEXT DEFAULT '[]',
          allowed_tools_json TEXT DEFAULT '[]',
          credential_profile_id TEXT,
          correlation_id TEXT,
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_groups (
          group_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          mode TEXT NOT NULL,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          metadata_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_group_items (
          group_id TEXT NOT NULL,
          action_id TEXT NOT NULL,
          platform TEXT NOT NULL,
          sequence_index INTEGER NOT NULL,
          sequence_total INTEGER NOT NULL,
          predecessor_action_id TEXT,
          dependency_policy TEXT NOT NULL DEFAULT 'block_if_predecessor_missing',
          metadata_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL,
          PRIMARY KEY (group_id, action_id),
          UNIQUE (group_id, sequence_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outcome_events (
          outcome_id TEXT PRIMARY KEY,
          action_id TEXT NOT NULL,
          platform TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          notes TEXT,
          collected_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS improvement_proposals (
          proposal_id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          suggested_update TEXT NOT NULL,
          risk TEXT NOT NULL,
          rollback_path TEXT NOT NULL,
          target_path TEXT NOT NULL,
          ledger_action_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, strftime('%Y-%m-%dT%H:%M:%SZ','now'))")

def connect_database(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def migrate_database(path: str | Path) -> None:
    con = connect_database(path)
    try:
        con.executescript(SCHEMA)
        ensure_schema(con)
        con.commit()
    finally:
        con.close()


def _ensure_columns(con: sqlite3.Connection) -> None:
    """Add Hermes-first metadata columns to databases created by earlier schemas."""
    for table, columns in MIGRATION_COLUMNS.items():
        existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
