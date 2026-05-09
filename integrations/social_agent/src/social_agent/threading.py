from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .ledger import ActionLedger
from .models import ActionEnvelope, ActionType, Platform, Sink, SourceProvenance, SourceType, TrustLevel, make_action, new_id


@dataclass(frozen=True)
class PlannedSocialAction:
    action_id: str
    platform: str
    sequence_index: int
    sequence_total: int
    predecessor_action_id: str | None


@dataclass(frozen=True)
class SocialThreadPlan:
    group_id: str
    kind: str
    platform: str
    total: int
    items: tuple[PlannedSocialAction, ...]


class SocialThreadPlanner:
    """Create ledger-first social threads/campaigns.

    Every post in a thread is a separate ActionEnvelope so approvals, previews,
    idempotency keys, and remote IDs are independently auditable. Later posts
    reference the predecessor action ID instead of guessing the remote post ID;
    live adapters resolve that predecessor to the recorded adapter remote_id at
    execution time.
    """

    def __init__(self, ledger: ActionLedger) -> None:
        self.ledger = ledger

    def create_thread(
        self,
        *,
        platform: Platform | str,
        account_id: str,
        texts: Iterable[str],
        source_ids: Iterable[str],
        created_by: str = "agent",
        mode: str = "dry_run",
        group_id: str | None = None,
        group_kind: str = "social_thread",
    ) -> SocialThreadPlan:
        platform_value = Platform(platform).value
        parts = [part.strip() for part in texts if part and part.strip()]
        if not parts:
            raise ValueError("thread requires at least one non-empty post")
        source_list = list(source_ids)
        if not source_list:
            raise ValueError("thread requires at least one source id")
        group_id = group_id or new_id("grp")
        self.ledger.create_action_group(
            group_id=group_id,
            kind=group_kind,
            mode=mode,
            created_by=created_by,
            metadata={
                "platform": platform_value,
                "publish_strategy": "serial_thread",
                "partial_failure_policy": "block_dependents_keep_completed_posts",
            },
        )
        return self._append_thread_to_group(
            group_id=group_id,
            platform=platform_value,
            account_id=account_id,
            texts=parts,
            source_ids=source_list,
            created_by=created_by,
            mode=mode,
            global_start_index=0,
            group_kind=group_kind,
        )

    def create_campaign(
        self,
        *,
        posts: dict[str, Iterable[str]],
        account_ids: dict[str, str],
        source_ids: Iterable[str],
        created_by: str = "agent",
        mode: str = "dry_run",
    ) -> SocialThreadPlan:
        group_id = new_id("grp")
        normalized = {Platform(platform).value: [text for text in texts] for platform, texts in posts.items()}
        total = sum(len([text for text in texts if text and str(text).strip()]) for texts in normalized.values())
        if total <= 0:
            raise ValueError("campaign requires at least one post")
        self.ledger.create_action_group(
            group_id=group_id,
            kind="social_campaign",
            mode=mode,
            created_by=created_by,
            metadata={
                "platforms": sorted(normalized),
                "publish_strategy": "parallel_platforms_serial_threads",
                "partial_failure_policy": "block_dependents_keep_completed_posts",
            },
        )
        all_items: list[PlannedSocialAction] = []
        offset = 0
        first_platform = next(iter(normalized))
        for platform, texts in normalized.items():
            if platform not in account_ids:
                raise ValueError(f"missing account id for platform {platform}")
            plan = self._append_thread_to_group(
                group_id=group_id,
                platform=platform,
                account_id=account_ids[platform],
                texts=[str(text).strip() for text in texts if text and str(text).strip()],
                source_ids=list(source_ids),
                created_by=created_by,
                mode=mode,
                global_start_index=offset,
                group_kind="social_campaign",
            )
            all_items.extend(plan.items)
            offset += len(plan.items)
        return SocialThreadPlan(group_id, "social_campaign", first_platform, len(all_items), tuple(all_items))

    def _append_thread_to_group(
        self,
        *,
        group_id: str,
        platform: str,
        account_id: str,
        texts: list[str],
        source_ids: list[str],
        created_by: str,
        mode: str,
        global_start_index: int,
        group_kind: str,
    ) -> SocialThreadPlan:
        provenance = SourceProvenance(SourceType.PUBLIC_WEB, TrustLevel.VERIFIED, (Sink.MEMORY, Sink.DRAFT, Sink.PUBLIC_POST))
        items: list[PlannedSocialAction] = []
        predecessor: ActionEnvelope | None = None
        total = len(texts)
        for local_index, text in enumerate(texts):
            action_type = ActionType.PUBLISH_POST if local_index == 0 else ActionType.REPLY_TO_POST
            predecessor_id = predecessor.action_id if predecessor else None
            metadata = {
                "thread_group_id": group_id,
                "thread_kind": group_kind,
                "thread_index": local_index,
                "thread_total": total,
                "sequence_index": global_start_index + local_index,
                "sequence_total": total,
                "predecessor_action_id": predecessor_id,
                "dependency_policy": "block_if_predecessor_missing",
                "publish_strategy": "serial_thread",
            }
            envelope = make_action(
                action_type=action_type,
                platform=platform,
                text=text,
                source_ids=source_ids,
                provenance=provenance,
                account_or_channel_id=account_id,
                reply_to_post_id=f"ledger:{predecessor_id}" if predecessor_id else None,
                mode=mode,
                created_by=created_by,
                metadata=metadata,
            )
            self.ledger.create_action(envelope, actor=created_by)
            self.ledger.add_action_to_group(
                group_id=group_id,
                action_id=envelope.action_id,
                platform=platform,
                sequence_index=global_start_index + local_index,
                sequence_total=total,
                predecessor_action_id=predecessor_id,
                metadata=metadata,
            )
            items.append(PlannedSocialAction(envelope.action_id, platform, local_index, total, predecessor_id))
            predecessor = envelope
        return SocialThreadPlan(group_id, group_kind, platform, total, tuple(items))
