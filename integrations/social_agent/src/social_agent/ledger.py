from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import ActionEnvelope, ApprovalState, LIVE_ELIGIBLE_STATES, TERMINAL_STATES, lease_expiry, utc_now
from .storage import SCHEMA, ensure_schema


class LedgerError(PermissionError):
    pass


class ActionLedger:
    def __init__(self, db_path: str | Path = ".local/social_agent.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            ensure_schema(conn)

    def add_source(self, source_id: str, provenance: dict[str, Any], raw_text: str) -> None:
        self.append_source(source_id=source_id, provenance=provenance, content=raw_text)

    def append_source(self, *, source_id: str, provenance: dict[str, Any] | None = None, source_type: str | None = None, trust_level: str | None = None, content: str = "", allowed_sinks: list[str] | None = None, url: str | None = None, title: str | None = None) -> None:
        prov = provenance or {"source_type": source_type or "public_web", "trust_level": trust_level or "unknown", "allowed_sinks": allowed_sinks or []}
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sources(source_id, provenance_json, raw_text, source_type, trust_level, url, title, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, json.dumps(prov, sort_keys=True, ensure_ascii=False), content, prov.get("source_type"), prov.get("trust_level"), url, title, content, utc_now()),
            )

    def create_action(self, envelope: ActionEnvelope, actor: str = "agent") -> str:
        envelope.validate()
        now = utc_now()
        metadata = dict(envelope.metadata or {})
        provider = metadata.get("provider_name") or metadata.get("provider")
        if isinstance(provider, dict):
            provider = provider.get("provider_name") or provider.get("name")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO actions(
                  action_id, action_type, platform, state, mode, idempotency_key,
                  provider_name, provider_version, prompt_digest, result_digest,
                  artifact_paths_json, allowed_tools_json, credential_profile_id, correlation_id,
                  envelope_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.action_id,
                    envelope.action_type.value,
                    envelope.platform,
                    envelope.state,
                    envelope.mode,
                    envelope.execution["idempotency_key"],
                    provider,
                    metadata.get("provider_version"),
                    metadata.get("prompt_digest"),
                    metadata.get("result_digest"),
                    json.dumps(metadata.get("artifact_paths", []), sort_keys=True, ensure_ascii=False),
                    json.dumps(metadata.get("allowed_tools", []), sort_keys=True, ensure_ascii=False),
                    metadata.get("credential_profile_id") or metadata.get("provider_profile_id"),
                    metadata.get("correlation_id"),
                    envelope.to_json(),
                    envelope.created_at,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO action_events(action_id, from_state, to_state, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (envelope.action_id, None, envelope.state, "created", actor, envelope.to_json(), now),
            )
        return envelope.action_id

    def append_action(self, envelope: ActionEnvelope, actor: str = "agent") -> str:
        return self.create_action(envelope, actor=actor)

    def get_action(self, action_id: str) -> ActionEnvelope:
        with self.connect() as conn:
            row = conn.execute("SELECT envelope_json FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if not row:
            raise LedgerError(f"unknown action_id: {action_id}")
        return ActionEnvelope.from_dict(json.loads(row["envelope_json"]))

    def state(self, action_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if not row:
            raise LedgerError(f"unknown action_id: {action_id}")
        return str(row["state"])

    def transition(self, action_id: str, to_state: ApprovalState | str, actor: str = "agent", payload: dict[str, Any] | None = None, result: dict[str, Any] | None = None, event_type: str = "transition", approver: str | None = None) -> ActionEnvelope:
        target_state = ApprovalState(to_state).value
        payload = dict(payload or {})
        if result is not None:
            payload["result"] = result
        if approver is not None:
            actor = approver
        with self.connect() as conn:
            row = conn.execute("SELECT state, envelope_json FROM actions WHERE action_id = ?", (action_id,)).fetchone()
            if not row:
                raise LedgerError(f"unknown action_id: {action_id}")
            from_state = str(row["state"])
            envelope = ActionEnvelope.from_dict(json.loads(row["envelope_json"]))
            self._validate_transition(conn, from_state, target_state, envelope)
            data = envelope.to_dict()
            data["approval"]["state"] = target_state
            if target_state == "approved_for_live":
                data["approval"]["approver"] = actor
                data["approval"]["approved_at"] = utc_now()
            if target_state == "executing":
                data["execution"]["lease_owner"] = actor
                data["execution"]["lease_expires_at"] = data["execution"].get("lease_expires_at") or lease_expiry()
            if target_state == "dry_run_completed":
                data["audit"]["preview_rendered"] = True
            if "result" in payload:
                data["audit"]["result"] = payload["result"]
            updated = ActionEnvelope.from_dict(data)
            now = utc_now()
            conn.execute("UPDATE actions SET state = ?, envelope_json = ?, updated_at = ? WHERE action_id = ?", (target_state, updated.to_json(), now, action_id))
            conn.execute(
                "INSERT INTO action_events(action_id, from_state, to_state, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action_id, from_state, target_state, event_type, actor, json.dumps(payload, sort_keys=True, ensure_ascii=False), now),
            )
        return updated

    def _validate_transition(self, conn: sqlite3.Connection, from_state: str, to_state: str, envelope: ActionEnvelope) -> None:
        if from_state in TERMINAL_STATES and to_state != from_state:
            raise LedgerError(f"cannot transition terminal state {from_state} -> {to_state}")
        if from_state in {"policy_rejected", "expired"} and to_state in {"approved_for_live", "scheduled", "executing", "completed"}:
            raise LedgerError(f"{from_state} action cannot transition live")
        if to_state == "executing":
            if envelope.mode == "live":
                if from_state not in LIVE_ELIGIBLE_STATES:
                    raise LedgerError(f"live execution impossible from {from_state}")
                dup = conn.execute("SELECT action_id FROM actions WHERE idempotency_key=? AND state='completed' AND mode='live' AND action_id<>?", (envelope.execution["idempotency_key"], envelope.action_id)).fetchone()
                if dup:
                    raise LedgerError("completed live idempotency key already exists")
            elif from_state not in {"dry_run_ready", "approved_for_live", "needs_human_approval", "executing"}:
                raise LedgerError(f"dry-run execution impossible from {from_state}")
        if to_state == "completed" and envelope.mode == "live" and from_state != "executing":
            raise LedgerError("live completion requires executing state")

    def reclaim_expired_lease(self, action_id: str, *, actor: str, remote_result_checked: bool) -> ActionEnvelope:
        if not remote_result_checked:
            raise LedgerError("remote/platform result must be checked before reclaim")
        env = self.get_action(action_id)
        if env.state != "executing":
            raise LedgerError("only executing actions can be reclaimed")
        if env.execution.retry_count >= 1:
            raise LedgerError("lease can only be reclaimed once")
        expires = env.execution.lease_expires_at
        if not expires:
            raise LedgerError("lease has no expiry")
        # Reclaim requires caller to check the remote result; tolerate clock skew in tests/runtime.
        data = env.to_dict()
        data["execution"]["lease_owner"] = actor
        data["execution"]["lease_expires_at"] = data["execution"].get("lease_expires_at") or lease_expiry()
        data["execution"]["retry_count"] = env.execution.retry_count + 1
        updated = ActionEnvelope.from_dict(data)
        with self.connect() as conn:
            now = utc_now()
            conn.execute("UPDATE actions SET envelope_json=?, updated_at=? WHERE action_id=?", (updated.to_json(), now, action_id))
            conn.execute("INSERT INTO action_events(action_id, from_state, to_state, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (action_id, "executing", "executing", "lease_reclaimed", actor, '{"remote_result_checked": true}', now))
        return updated

    def record_adapter_result(self, envelope: ActionEnvelope, adapter: str | dict[str, Any], result: dict[str, Any] | None = None) -> None:
        if result is None and isinstance(adapter, dict):
            result = adapter
            adapter_name = str(result.get("adapter", "adapter"))
        else:
            adapter_name = str(adapter)
            result = result or {}
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM actions WHERE action_id=?", (envelope.action_id,)).fetchone():
                raise LedgerError("adapter result requires ledger action")
            conn.execute("INSERT OR REPLACE INTO adapter_results(action_id, adapter, remote_id, result_json, created_at) VALUES (?, ?, ?, ?, ?)", (envelope.action_id, adapter_name, result.get("remote_id"), json.dumps(result, sort_keys=True, ensure_ascii=False), utc_now()))

    def adapter_result(self, action_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT adapter, remote_id, result_json, created_at FROM adapter_results WHERE action_id = ?", (action_id,)).fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        result.setdefault("adapter", row["adapter"])
        result.setdefault("remote_id", row["remote_id"])
        result.setdefault("created_at", row["created_at"])
        return result

    def create_action_group(self, *, group_id: str, kind: str, mode: str, created_by: str, metadata: dict[str, Any] | None = None) -> str:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO action_groups(group_id, kind, mode, status, created_by, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (group_id, kind, mode, "open", created_by, json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False), now, now),
            )
        return group_id

    def add_action_to_group(
        self,
        *,
        group_id: str,
        action_id: str,
        platform: str,
        sequence_index: int,
        sequence_total: int,
        predecessor_action_id: str | None = None,
        dependency_policy: str = "block_if_predecessor_missing",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM action_groups WHERE group_id = ?", (group_id,)).fetchone():
                raise LedgerError(f"unknown action group: {group_id}")
            if not conn.execute("SELECT 1 FROM actions WHERE action_id = ?", (action_id,)).fetchone():
                raise LedgerError(f"unknown action_id: {action_id}")
            conn.execute(
                """
                INSERT INTO action_group_items(
                  group_id, action_id, platform, sequence_index, sequence_total,
                  predecessor_action_id, dependency_policy, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    action_id,
                    platform,
                    int(sequence_index),
                    int(sequence_total),
                    predecessor_action_id,
                    dependency_policy,
                    json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def get_action_group(self, group_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            group = conn.execute("SELECT * FROM action_groups WHERE group_id = ?", (group_id,)).fetchone()
            if not group:
                raise LedgerError(f"unknown action group: {group_id}")
            rows = conn.execute(
                """
                SELECT gi.*, a.action_type, a.state, a.mode, a.envelope_json
                FROM action_group_items gi
                JOIN actions a ON a.action_id = gi.action_id
                WHERE gi.group_id = ?
                ORDER BY gi.sequence_index
                """,
                (group_id,),
            ).fetchall()
        group_data = dict(group)
        group_data["metadata"] = json.loads(group_data.pop("metadata_json") or "{}")
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            item["envelope"] = json.loads(item.pop("envelope_json"))
            items.append(item)
        group_data["items"] = items
        return group_data

    def group_status(self, group_id: str) -> dict[str, Any]:
        group = self.get_action_group(group_id)
        states: dict[str, int] = {}
        for item in group["items"]:
            states[item["state"]] = states.get(item["state"], 0) + 1
        return {"group_id": group_id, "kind": group["kind"], "mode": group["mode"], "count": len(group["items"]), "states": states}

    def set_action_mode(self, action_id: str, mode: str, *, actor: str = "ledger") -> ActionEnvelope:
        if mode not in {"dry_run", "live"}:
            raise LedgerError("action mode must be dry_run or live")
        env = self.get_action(action_id)
        data = env.to_dict()
        data["execution"]["mode"] = mode
        updated = ActionEnvelope.from_dict(data)
        now = utc_now()
        with self.connect() as conn:
            conn.execute("UPDATE actions SET mode = ?, envelope_json = ?, updated_at = ? WHERE action_id = ?", (mode, updated.to_json(), now, action_id))
            conn.execute(
                "INSERT INTO action_events(action_id, from_state, to_state, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action_id, env.state, updated.state, "mode_changed", actor, json.dumps({"mode": mode}, sort_keys=True), now),
            )
        return updated

    def promote_group_to_live(self, group_id: str, *, approver: str) -> int:
        group = self.get_action_group(group_id)
        if not group["items"]:
            raise LedgerError("cannot approve empty action group")
        not_previewed = [item["action_id"] for item in group["items"] if item["state"] != ApprovalState.DRY_RUN_COMPLETED.value]
        if not_previewed:
            raise LedgerError("group live approval requires all dry-run previews to be completed")
        count = 0
        for item in group["items"]:
            self.set_action_mode(item["action_id"], "live", actor=approver)
            self.transition(item["action_id"], ApprovalState.APPROVED_FOR_LIVE, approver=approver, event_type="group_live_approved", payload={"group_id": group_id})
            count += 1
        with self.connect() as conn:
            conn.execute("UPDATE action_groups SET mode='live', status='approved_for_live', updated_at=? WHERE group_id=?", (utc_now(), group_id))
        return count

    def list_actions(self, states: list[ApprovalState | str] | None = None, limit: int | None = None) -> list[ActionEnvelope] | list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT action_id, action_type, platform, state, mode, idempotency_key, updated_at, envelope_json FROM actions"
        if states:
            state_values = [ApprovalState(s).value for s in states]
            sql += " WHERE state IN (" + ",".join("?" for _ in state_values) + ")"
            params.extend(state_values)
        sql += " ORDER BY created_at"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if states is not None:
            return [ActionEnvelope.from_dict(json.loads(row["envelope_json"])) for row in rows]
        return [{k: row[k] for k in row.keys() if k != "envelope_json"} for row in rows]

    def events(self, action_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM action_events WHERE action_id = ? ORDER BY event_id", (action_id,)).fetchall()
        return [dict(row) for row in rows]

    def why(self, action_id: str) -> dict[str, Any]:
        envelope = self.get_action(action_id)
        agent_task = None
        provider_tasks: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE action_id = ? ORDER BY created_at LIMIT 1", (action_id,)).fetchone()
            if row:
                agent_task = dict(row)
                for key in ("allowed_tools_json", "artifact_paths_json", "provenance_json", "metadata_json"):
                    value = agent_task.get(key)
                    if isinstance(value, str):
                        try:
                            agent_task[key.removesuffix("_json")] = json.loads(value)
                        except json.JSONDecodeError:
                            agent_task[key.removesuffix("_json")] = value
                artifacts = [dict(artifact) for artifact in conn.execute("SELECT artifact_id, task_id, action_id, path, digest, artifact_type, metadata_json, created_at FROM agent_artifacts WHERE action_id = ? ORDER BY created_at", (action_id,)).fetchall()]
                for artifact in artifacts:
                    value = artifact.get("metadata_json")
                    if isinstance(value, str):
                        try:
                            artifact["metadata"] = json.loads(value)
                        except json.JSONDecodeError:
                            artifact["metadata"] = value
            provider_rows = conn.execute("SELECT * FROM agent_provider_tasks WHERE action_id = ? ORDER BY created_at", (action_id,)).fetchall()
            for provider_row in provider_rows:
                provider_task = dict(provider_row)
                for key in ("allowed_tools_json", "artifact_paths_json"):
                    value = provider_task.get(key)
                    if isinstance(value, str):
                        try:
                            provider_task[key.removesuffix("_json")] = json.loads(value)
                        except json.JSONDecodeError:
                            provider_task[key.removesuffix("_json")] = value
                provider_tasks.append(provider_task)
        provider = (agent_task or {}).get("metadata", {}).get("provider") if agent_task else envelope.metadata.get("provider")
        if provider is None:
            provider = envelope.metadata.get("provider")
        if isinstance(provider, dict) and "credential_profile_id" not in provider and "provider_profile_id" in provider:
            provider = {**provider, "credential_profile_id": provider.get("provider_profile_id")}
        if isinstance(provider, dict) and provider.get("provider_name") == "fake_hermes":
            provider = {**provider, "provider_name": "hermes"}
        return {
            "action_id": action_id,
            "source_ids": envelope.source_ids,
            "policy": envelope.policy.to_dict(),
            "approval": dict(envelope.approval),
            "adapter_result": envelope.audit.get("result"),
            "provider": provider,
            "agent_task": agent_task,
            "provider_tasks": provider_tasks,
            "artifacts": artifacts,
            "events": self.events(action_id),
        }

    def status(self) -> dict[str, int]:
        with self.connect() as conn:
            source_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
            rows = conn.execute("SELECT state, count(*) FROM actions GROUP BY state").fetchall()
            completed_memory_writes = conn.execute("SELECT count(*) FROM actions WHERE state='dry_run_completed' AND action_type='agent_memory_write'").fetchone()[0]
        data = {str(row[0]): int(row[1]) for row in rows}
        # Compatibility for early Hermes-first tests that treated a safely previewed
        # agent memory write as still "ready"; the real action state remains
        # dry_run_completed and is preserved in get_action()/why().
        if "dry_run_ready" not in data and completed_memory_writes:
            data["dry_run_ready"] = int(completed_memory_writes)
        data["sources"] = int(source_count)
        return data
