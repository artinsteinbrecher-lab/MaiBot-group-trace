from __future__ import annotations

from unittest import TestCase

from core.models import CompositeRule, MessageRecord
from core.rule_engine import CompositeRuleEngine, RuleValidationError


def message(text: str, timestamp: float = 100.0, group_id: str = "123") -> MessageRecord:
    return MessageRecord(
        message_id=f"m-{timestamp}-{text}",
        stream_id="stream-1",
        group_id=group_id,
        group_name="测试群",
        user_id="42",
        user_name="测试成员",
        text=text,
        timestamp=timestamp,
    )


def rule(**overrides) -> CompositeRule:
    values = {
        "id": "rule-1",
        "name": "输出限制",
        "owner_user_id": "1",
        "group_ids": ["123"],
        "required_terms": ["DSV4F"],
        "any_terms": ["64K", "max_tokens"],
        "excluded_terms": ["价格"],
        "window_seconds": 600,
    }
    values.update(overrides)
    return CompositeRule(**values)


class RuleEngineTests(TestCase):
    def setUp(self) -> None:
        self.engine = CompositeRuleEngine()

    def test_cross_message_and_or_match(self) -> None:
        first = message("DSV4F 默认有什么限制", 100)
        second = message("听说输出只有 64K", 120)
        self.engine.push(first)
        self.engine.push(second)
        decision = self.engine.evaluate(rule(), self.engine.recent_for_rule(rule(), now=120))
        self.assertTrue(decision.matched)
        self.assertEqual(decision.matched_terms, ["DSV4F", "64K"])

    def test_excluded_term_wins(self) -> None:
        value = message("DSV4F 64K 的价格", 100)
        decision = self.engine.evaluate(rule(), [value])
        self.assertFalse(decision.matched)
        self.assertEqual(decision.excluded_term, "价格")

    def test_current_message_must_contribute_signal(self) -> None:
        current = message("今天下雨了", 130)
        self.assertFalse(self.engine.message_has_signal(rule(), current))
        self.assertTrue(self.engine.message_has_signal(rule(), message("max_tokens 怎么开", 140)))

    def test_regex_validation(self) -> None:
        invalid = rule(required_terms=[], any_terms=[], regex_patterns=["("])
        with self.assertRaises(RuleValidationError):
            self.engine.validate(invalid)

    def test_semantic_only_rule_is_rejected(self) -> None:
        invalid = rule(required_terms=[], any_terms=[], semantic_description="某个主题")
        with self.assertRaises(RuleValidationError):
            self.engine.validate(invalid)
