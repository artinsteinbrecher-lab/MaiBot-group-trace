from __future__ import annotations

from unittest import IsolatedAsyncioTestCase, TestCase

from core.intent_parser import IntentParseError, RuleIntentParser, extract_json_object


class IntentParserTests(IsolatedAsyncioTestCase):
    async def test_rule_parser_forces_explicit_group_and_owner(self) -> None:
        async def generate(_prompt: str):
            return {
                "success": True,
                "response": """{
                    "name": "输出限制",
                    "required_terms": ["DSV4F"],
                    "any_terms": ["64K", "max_tokens"],
                    "excluded_terms": ["价格"],
                    "regex_patterns": [],
                    "semantic_description": "讨论 DSV4F 输出限制",
                    "window_seconds": 600,
                    "min_occurrences": 2,
                    "cooldown_seconds": 1800
                }""",
            }

        parser = RuleIntentParser(generate)
        rule = await parser.parse_rule("帮我盯着输出限制", "123456", "9988")
        self.assertEqual(rule.group_ids, ["123456"])
        self.assertEqual(rule.owner_user_id, "9988")
        self.assertEqual(rule.min_occurrences, 2)

    async def test_query_plan_is_bounded(self) -> None:
        async def generate(_prompt: str):
            return {
                "success": True,
                "response": '{"search_query":"输出限制","keywords":["DSV4F"],"excluded_terms":[],"history_days":999}',
            }

        plan = await RuleIntentParser(generate).parse_query("线索", 30)
        self.assertEqual(plan.history_days, 365)
        self.assertEqual(plan.keywords, ["DSV4F"])


class JsonExtractionTests(TestCase):
    def test_markdown_fence_is_accepted(self) -> None:
        self.assertEqual(extract_json_object('```json\n{"ok": true}\n```'), {"ok": True})

    def test_non_json_is_rejected(self) -> None:
        with self.assertRaises(IntentParseError):
            extract_json_object("没有结构")
