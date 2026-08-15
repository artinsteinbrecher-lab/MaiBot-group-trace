from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from core.models import CompositeRule, PendingRule
from core.storage import StateStore


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
