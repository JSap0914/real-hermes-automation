import json
from pathlib import Path

from tools.social_automation_tool import parse_social_command_args, social_automation
from toolsets import resolve_toolset


def _call(**kwargs):
    return json.loads(social_automation(kwargs))


def test_social_automation_status_uses_active_hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = _call(action="status")

    assert result["success"] is True
    assert result["database"] == str(home / "social_automation" / "social_agent.sqlite3")
    assert Path(result["database"]).exists()
    assert result["live_enabled"] is False
    assert result["status"]["sources"] == 0


def test_social_automation_propose_preview_why_and_approve_are_ledgered(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    proposed = _call(
        action="propose",
        text="Hermes automation integration milestone",
        source_url="https://example.com/hermes-automation",
        title="Hermes automation",
        platform="x",
    )

    assert proposed["success"] is True
    assert proposed["action_id"]
    assert proposed["network_write"] is False
    assert proposed["preview"]["dry_run"] is True
    assert proposed["preview"]["network_write"] is False

    audit = _call(action="why", action_id=proposed["action_id"])
    assert audit["success"] is True
    assert audit["why"]["action_id"] == proposed["action_id"]
    assert audit["why"]["approval"]["state"] == "dry_run_completed"

    approved = _call(action="approve", action_id=proposed["action_id"], approver="tester")
    assert approved["success"] is True
    assert approved["item"]["state"] == "approved_for_live"
    assert "Live execution remains blocked" in approved["note"]


def test_social_automation_run_once_renders_unpreviewed_dry_run(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    proposed = _call(
        action="propose",
        text="Run once should render this dry-run preview",
        platform="threads",
        render_preview=False,
    )
    assert proposed["state"] == "dry_run_ready"
    assert proposed["preview"] is None

    run = _call(action="run_once")
    assert run["success"] is True
    assert run["network_write"] is False
    assert run["results"]
    assert run["results"][0]["state"] == "dry_run_completed"
    assert run["results"][0]["result"]["network_write"] is False


def test_social_automation_is_in_default_hermes_toolset():
    assert "social_automation" in resolve_toolset("hermes-cli")


def test_social_command_parser_maps_gateway_friendly_subcommands():
    assert parse_social_command_args("") == {"action": "status"}
    assert parse_social_command_args("run-once") == {"action": "run_once"}
    assert parse_social_command_args("why act_123") == {"action": "why", "action_id": "act_123"}
    assert parse_social_command_args("approve act_123 owner") == {
        "action": "approve",
        "action_id": "act_123",
        "approver": "owner",
    }
    assert parse_social_command_args("propose threads hello world") == {
        "action": "propose",
        "platform": "threads",
        "text": "hello world",
        "render_preview": True,
    }
