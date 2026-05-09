from tools.social_automation_tool import _ensure_social_agent_path


_ensure_social_agent_path()

from social_agent.adapters import FakeSocialHttpTransport, OfficialThreadsAdapter, OfficialXAdapter
from social_agent.config import AdapterConfig
from social_agent.ledger import ActionLedger
from social_agent.models import ActionEnvelope, ActionType, ApprovalState, Content, Platform, Sink, SourceProvenance, SourceType, Target, TrustLevel


def _readiness() -> dict[str, bool]:
    return {
        "docs_reviewed": True,
        "scopes_documented": True,
        "mocked_tests_passed": True,
        "dry_run_preview_passed": True,
        "policy_wired": True,
        "rate_limits_configured": True,
        "idempotency_tests_passed": True,
        "manual_live_enable": True,
    }


def _config() -> AdapterConfig:
    return AdapterConfig(enabled=True, live_enabled=True, credential_profile_id="automation-profile", readiness=_readiness())


def _envelope(
    *,
    platform: Platform,
    action_type: ActionType = ActionType.PUBLISH_POST,
    text: str = "Official payload smoke test",
    account_id: str = "account-1",
    post_id: str | None = None,
    mode: str = "dry_run",
    state: ApprovalState = ApprovalState.DRY_RUN_READY,
    action_id: str = "act_payload",
) -> ActionEnvelope:
    return ActionEnvelope(
        action_id=action_id,
        action_type=action_type,
        target=Target(platform, account_or_channel_id=account_id, post_id=post_id),
        content=Content(text),
        source_ids=["src_payload"],
        source_provenance=SourceProvenance(SourceType.PUBLIC_WEB, TrustLevel.VERIFIED, [Sink.PUBLIC_POST]),
        approval={"state": state.value, "approver": "tester", "approved_at": None},
        execution={"mode": mode, "idempotency_key": f"idem-{action_id}"},
    )


def test_x_reply_dry_run_uses_official_post_payload_shape():
    envelope = _envelope(
        platform=Platform.X,
        action_type=ActionType.REPLY_TO_POST,
        text="Reply using the official X shape",
        post_id="tweet-123",
    )

    preview = OfficialXAdapter(readiness=True).dry_run(envelope)

    assert preview["endpoint"] == "/2/tweets"
    assert preview["request_payload"] == {
        "text": "Reply using the official X shape",
        "reply": {"in_reply_to_tweet_id": "tweet-123"},
    }
    assert "account_id" not in preview["request_payload"]
    assert "correlation_id" not in preview["request_payload"]
    assert "content_hash" not in preview["request_payload"]
    assert "reply_to_post_id" not in preview["request_payload"]
    assert preview["network_write"] is False


def test_threads_dry_run_previews_container_then_publish_sequence():
    envelope = _envelope(platform=Platform.THREADS, account_id="threads-user-1", action_id="act_threads_preview")

    preview = OfficialThreadsAdapter(readiness=True).dry_run(envelope)

    assert preview["endpoint"] == "/threads-user-1/threads"
    assert preview["publish_endpoint"] == "/threads-user-1/threads_publish"
    assert preview["request_payload"] == {"media_type": "TEXT", "text": "Official payload smoke test"}
    assert preview["network_write"] is False

    sequence = preview["request_sequence"]
    assert sequence == [
        {
            "step": "create_container",
            "method": "POST",
            "endpoint": "/threads-user-1/threads",
            "payload": {"media_type": "TEXT", "text": "Official payload smoke test"},
        },
        {
            "step": "publish_container",
            "method": "POST",
            "endpoint": "/threads-user-1/threads_publish",
            "payload": {"creation_id": "container:act_threads_preview"},
        },
    ]


def test_threads_live_execution_creates_container_then_publishes(tmp_path):
    ledger = ActionLedger(tmp_path / "social.sqlite3")
    envelope = _envelope(
        platform=Platform.THREADS,
        account_id="threads-user-1",
        mode="live",
        state=ApprovalState.APPROVED_FOR_LIVE,
        action_id="act_threads_live",
    )
    ledger.create_action(envelope)
    transport = FakeSocialHttpTransport(json_bodies=[{"id": "container-1"}, {"id": "thread-1"}])

    result = OfficialThreadsAdapter(ledger=ledger, config=_config(), transport=transport).execute(envelope)

    assert result["remote_id"] == "thread-1"
    assert result["container_id"] == "container-1"
    assert result["endpoint"] == "/threads-user-1/threads_publish"
    assert result["network_write"] is True
    assert [call["endpoint"] for call in transport.calls] == [
        "/threads-user-1/threads",
        "/threads-user-1/threads_publish",
    ]
    assert transport.calls[0]["payload"] == {"media_type": "TEXT", "text": "Official payload smoke test"}
    assert transport.calls[1]["payload"] == {"creation_id": "container-1"}
    assert ledger.adapter_result(envelope.action_id)["remote_id"] == "thread-1"


def test_threads_live_reply_uses_reply_to_id_in_container_payload(tmp_path):
    ledger = ActionLedger(tmp_path / "social.sqlite3")
    envelope = _envelope(
        platform=Platform.THREADS,
        action_type=ActionType.REPLY_TO_POST,
        text="Reply as a Threads container",
        account_id="threads-user-1",
        post_id="thread-root-1",
        mode="live",
        state=ApprovalState.APPROVED_FOR_LIVE,
        action_id="act_threads_reply",
    )
    ledger.create_action(envelope)
    transport = FakeSocialHttpTransport(json_bodies=[{"id": "container-2"}, {"id": "thread-2"}])

    result = OfficialThreadsAdapter(ledger=ledger, config=_config(), transport=transport).execute(envelope)

    assert result["remote_id"] == "thread-2"
    assert transport.calls[0]["payload"] == {
        "media_type": "TEXT",
        "text": "Reply as a Threads container",
        "reply_to_id": "thread-root-1",
    }
    assert "reply_to_post_id" not in transport.calls[0]["payload"]
