from __future__ import annotations

from unittest import IsolatedAsyncioTestCase, TestCase

from core.models import MessageRecord
from core.retrieval import expand_context, rank_lexically, rerank_with_embeddings


def record(index: int, text: str) -> MessageRecord:
    return MessageRecord(
        message_id=f"m{index}",
        stream_id="s",
        group_id="g",
        group_name="群",
        user_id="u",
        user_name="成员",
        text=text,
        timestamp=float(index),
    )


class RetrievalTests(TestCase):
    def test_lexical_rank_prefers_technical_evidence(self) -> None:
        messages = [record(1, "今天天气不错"), record(2, "DSV4F 默认输出限制是 64K")]
        ranked = rank_lexically("DSV4F 输出 64K", messages, 10)
        self.assertEqual(ranked[0][0].message_id, "m2")
        self.assertGreater(ranked[0][1], 0)

    def test_lexical_rank_ignores_query_filler_words(self) -> None:
        messages = [
            record(1, "有没有推荐的免费账号"),
            record(2, "今天群里出现了一只猫娘"),
        ]
        ranked = rank_lexically("看看今天有没有猫娘出没", messages, 10)
        self.assertEqual(ranked[0][0].message_id, "m2")
        self.assertGreater(ranked[0][1], 0)

    def test_lexical_rank_does_not_double_count_subterms(self) -> None:
        # 旧实现会把“输出限制”拆出的所有 2/3 字片段重复计分，
        # 让只命中一个词组的消息压过命中多个不同关键词的消息。
        messages = [
            record(1, "输出限制"),
            record(2, "DSV4F 需要配置 64K"),
        ]
        ranked = rank_lexically("DSV4F 输出限制 64K", messages, 10)
        self.assertEqual(ranked[0][0].message_id, "m2")

    def test_context_expansion_preserves_time_order(self) -> None:
        messages = [record(index, f"消息{index}") for index in range(8)]
        expanded = expand_context(messages, [messages[4]], radius=2, limit=10)
        self.assertEqual([item.message_id for item in expanded], ["m2", "m3", "m4", "m5", "m6"])

    def test_context_trimming_keeps_messages_near_hits(self) -> None:
        # 超出上限裁剪时应保留离命中消息最近的上下文，
        # 而不是保留时间最早的消息导致证据位置偏移。
        messages = [record(index, f"消息{index}") for index in range(30)]
        expanded = expand_context(messages, [messages[20], messages[5]], radius=3, limit=9)
        self.assertEqual(
            [item.message_id for item in expanded],
            ["m3", "m4", "m5", "m6", "m7", "m18", "m19", "m20", "m21"],
        )


class EmbeddingTests(IsolatedAsyncioTestCase):
    async def test_embedding_rerank(self) -> None:
        candidates = [(record(1, "甲"), 1.0), (record(2, "乙"), 1.0)]

        async def embed(_texts):
            return {
                "success": True,
                "results": [
                    {"embedding": [1.0, 0.0]},
                    {"embedding": [0.0, 1.0]},
                    {"embedding": [1.0, 0.0]},
                ],
            }

        ranked = await rerank_with_embeddings("目标", candidates, embed, 2)
        self.assertEqual(ranked[0].message_id, "m2")
