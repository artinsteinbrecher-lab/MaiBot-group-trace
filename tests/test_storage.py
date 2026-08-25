from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest import IsolatedAsyncioTestCase

from core.models import CompositeRule, MessageRecord, PendingRule
from core.storage import StateStore


def make_message(index: int, text: str, timestamp: float, group_id: str = "20001") -> MessageRecord:
    return MessageRecord(
        message_id=f"m{index}",
        stream_id="s",
        group_id=group_id,
        group_name="群",
        user_id="u",
        user_name="成员",
        text=text,
        timestamp=timestamp,
    )


class StorageTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.sqlite3")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def make_rule(**overrides) -> CompositeRule:
        values = {
            "id": "abc123",
            "name": "测试规则",
            "owner_user_id": "10001",
            "group_ids": ["20001"],
            "required_terms": ["DSV4F"],
            "min_occurrences": 2,
            "window_seconds": 60,
            "cooldown_seconds": 120,
            "created_at": 1.0,
        }
        values.update(overrides)
        return CompositeRule(**values)

    async def test_rule_roundtrip_and_state(self) -> None:
        rule = self.make_rule()
        await self.store.save_rule(rule)
        loaded = await self.store.get_rule(rule.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.required_terms, ["DSV4F"])
        self.assertTrue(await self.store.set_rule_enabled(rule.id, False, owner_user_id="10001"))
        loaded = await self.store.get_rule(rule.id)
        self.assertFalse(loaded.enabled)
        self.assertFalse(await self.store.delete_rule(rule.id, owner_user_id="wrong-owner"))
        self.assertTrue(await self.store.delete_rule(rule.id, owner_user_id="10001"))

    async def test_occurrence_threshold_and_cooldown_are_atomic(self) -> None:
        rule = self.make_rule()
        await self.store.save_rule(rule)
        first = await self.store.record_candidate_and_should_trigger(
            rule,
            message_id="m1",
            group_id="20001",
            matched_text="DSV4F",
            occurred_at=1000,
        )
        second = await self.store.record_candidate_and_should_trigger(
            rule,
            message_id="m2",
            group_id="20001",
            matched_text="DSV4F",
            occurred_at=1010,
        )
        duplicate = await self.store.record_candidate_and_should_trigger(
            rule,
            message_id="m2",
            group_id="20001",
            matched_text="DSV4F",
            occurred_at=1010,
        )
        cooldown = await self.store.record_candidate_and_should_trigger(
            rule,
            message_id="m3",
            group_id="20001",
            matched_text="DSV4F",
            occurred_at=1020,
        )
        self.assertFalse(first)
        self.assertTrue(second)
        self.assertFalse(duplicate)
        self.assertFalse(cooldown)

    async def test_pending_rule_is_one_time(self) -> None:
        pending = PendingRule("10001", "stream", self.make_rule(), expires_at=9999999999)
        await self.store.save_pending(pending)
        self.assertIsNotNone(await self.store.pop_pending("10001"))
        self.assertIsNone(await self.store.pop_pending("10001"))


class MessageIndexTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.sqlite3")
        await self.store.initialize()
        self.now = time()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_index_search_by_keyword(self) -> None:
        await self.store.index_messages(
            [
                make_message(1, "DSV4F 输出限制是 64K", self.now - 100),
                make_message(2, "今天吃什么", self.now - 90),
                make_message(3, "输出限制的后续讨论", self.now - 50),
            ]
        )
        hits = await self.store.search_index("20001", ["输出限制"], self.now - 3600, self.now, 10)
        self.assertEqual([item.message_id for item in hits], ["m1", "m3"])
        # terms 为空时返回时间范围内最新消息
        latest = await self.store.search_index("20001", [], self.now - 3600, self.now, 2)
        self.assertEqual([item.message_id for item in latest], ["m2", "m3"])

    async def test_index_deduplicates_and_reports_coverage(self) -> None:
        record = make_message(1, "重复消息", self.now - 10)
        await self.store.index_messages([record])
        await self.store.index_messages([record])
        coverage = await self.store.index_coverage("20001")
        self.assertIsNotNone(coverage)
        self.assertEqual(coverage[2], 1)
        self.assertIsNone(await self.store.index_coverage("99999"))

    async def test_index_neighbors_returns_adjacent_messages(self) -> None:
        await self.store.index_messages(
            [make_message(index, f"消息{index}", self.now - 100 + index) for index in range(5)]
        )
        neighbors = await self.store.index_neighbors("20001", self.now - 100 + 2, 1)
        self.assertEqual([item.message_id for item in neighbors], ["m1", "m2", "m3"])

    async def test_index_like_wildcards_are_literal(self) -> None:
        await self.store.index_messages(
            [
                make_message(1, "进度100%了", self.now - 20),
                make_message(2, "进度100了", self.now - 10),
            ]
        )
        hits = await self.store.search_index("20001", ["100%"], self.now - 3600, self.now, 10)
        self.assertEqual([item.message_id for item in hits], ["m1"])

    async def test_prune_removes_expired_and_unlisted_groups(self) -> None:
        await self.store.index_messages(
            [
                make_message(1, "过期消息", self.now - 400 * 86400),
                make_message(2, "近期消息", self.now - 100),
                make_message(3, "其他群消息", self.now - 100, group_id="30001"),
            ]
        )
        removed = await self.store.prune_index(["20001"], 180)
        self.assertEqual(removed, 2)
        coverage = await self.store.index_coverage("20001")
        self.assertEqual(coverage[2], 1)
        self.assertIsNone(await self.store.index_coverage("30001"))
