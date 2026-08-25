from __future__ import annotations

from unittest import TestCase

from core.models import CompositeRule, MessageRecord
from core.reporting import (
    attach_evidence_footer,
    fallback_history_answer,
    format_monitor_notification,
    render_evidence_lines,
)


def record(index: int, text: str, timestamp: float) -> MessageRecord:
    return MessageRecord(
        message_id=f"m{index}",
        stream_id="s",
        group_id="g",
        group_name="群",
        user_id="u",
        user_name="成员",
        text=text,
        timestamp=timestamp,
    )


class EvidenceRenderTests(TestCase):
    def test_gap_marker_between_conversations(self) -> None:
        evidence = [record(1, "第一段", 1000.0), record(2, "第二段", 1000.0 + 7200)]
        lines = render_evidence_lines(evidence)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("E1 "))
        self.assertIn("另一段对话", lines[1])
        self.assertIn("约 2 小时", lines[1])
        self.assertTrue(lines[2].startswith("E2 "))

    def test_no_gap_marker_for_continuous_conversation(self) -> None:
        evidence = [record(1, "第一句", 1000.0), record(2, "第二句", 1060.0)]
        lines = render_evidence_lines(evidence)
        self.assertEqual(len(lines), 2)


class EvidenceFooterTests(TestCase):
    def test_footer_lists_only_cited_evidence(self) -> None:
        evidence = [record(index, f"内容{index}", 1000.0 + index) for index in (1, 2, 3)]
        output = attach_evidence_footer("结论一 [E2]，结论二 [E3]", evidence)
        self.assertIn("证据原文：", output)
        self.assertIn("[E2]", output)
        self.assertIn("内容2", output)
        self.assertIn("内容3", output)
        self.assertNotIn("内容1", output)

    def test_footer_without_citations_shows_evidence(self) -> None:
        evidence = [record(index, f"内容{index}", 1000.0 + index) for index in (1, 2)]
        output = attach_evidence_footer("模型没有标注编号的结论", evidence)
        self.assertIn("内容1", output)
        self.assertIn("内容2", output)

    def test_footer_removes_out_of_range_citations(self) -> None:
        evidence = [record(1, "内容1", 1000.0)]
        output = attach_evidence_footer("结论 [E9]", evidence)
        self.assertIn("内容1", output)
        # 无效编号应从回答正文中清除，不留悬空引用
        self.assertNotIn("[E9]", output)

    def test_footer_attaches_all_cited_evidence_without_cap(self) -> None:
        evidence = [record(index, f"内容{index}", 1000.0 + index) for index in range(1, 16)]
        answer = " ".join(f"结论{index} [E{index}]" for index in range(1, 16))
        output = attach_evidence_footer(answer, evidence, max_lines=10)
        # 模型引用了 15 条时应全部附原文，max_lines 只限制未标注引用的情况
        for index in range(1, 16):
            self.assertIn(f"[E{index}] ", output.split("证据原文：")[1])

    def test_footer_skipped_without_evidence(self) -> None:
        self.assertEqual(attach_evidence_footer("结论", []), "结论")


class FallbackAnswerTests(TestCase):
    def test_fallback_includes_numbered_evidence(self) -> None:
        evidence = [record(1, "原始消息", 1000.0)]
        output = fallback_history_answer("线索", "测试群", evidence)
        self.assertIn("模型整理暂时不可用", output)
        self.assertIn("E1 ", output)
        self.assertIn("原始消息", output)


class MonitorNotificationTests(TestCase):
    def test_notification_contains_rule_id_and_pause_hint(self) -> None:
        rule = CompositeRule(
            id="abcdef1234",
            name="输出限制",
            owner_user_id="10001",
            group_ids=["20001"],
            required_terms=["DSV4F"],
        )
        evidence = [record(1, "DSV4F 输出限制是 64K", 1000.0)]
        output = format_monitor_notification(rule, "模型群", ["DSV4F"], evidence)
        self.assertIn("规则编号：abcdef1234", output)
        self.assertIn("/监控暂停 abcdef1234", output)
        self.assertIn("E1 ", output)
