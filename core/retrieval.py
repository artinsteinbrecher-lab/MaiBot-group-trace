"""群聊历史的本地召回、嵌入重排和上下文扩展。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from math import sqrt
from typing import Any, Dict, List, Tuple

import re

from .models import MessageRecord
from .rule_engine import normalize_text

EmbedFn = Callable[[List[str]], Awaitable[Dict[str, Any]]]

_QUERY_FILLER_PHRASES = tuple(
    sorted(
        {
            "帮我查一下",
            "帮我找一下",
            "帮我看一下",
            "查看一下",
            "查询一下",
            "搜索一下",
            "查找一下",
            "有没有人",
            "有没有",
            "是否有",
            "看一看",
            "查一查",
            "找一找",
            "看一下",
            "查一下",
            "找一下",
            "看看",
            "查查",
            "找找",
            "今天",
            "昨天",
            "最近",
            "之前",
            "相关信息",
            "相关消息",
            "聊天记录",
        },
        key=len,
        reverse=True,
    )
)

_QUERY_STOP_TERMS = {
    "这个",
    "那个",
    "相关",
    "信息",
    "消息",
    "记录",
    "聊天",
    "群聊",
    "讨论",
    "一下",
    "是否",
}

# 中文连接字和语气字：滑窗片段不应跨越这些字，否则会产生
# “溃和”“池的”这类无意义检索词，抬高无关消息的得分。
_CHINESE_SPLIT_CHARS = "的了和是在有就都也还把被给对与及或等吗呢吧啊呀么嘛"


def rank_lexically(query: str, messages: Sequence[MessageRecord], limit: int) -> List[Tuple[MessageRecord, float]]:
    """使用确定性的字符/单词重叠分数召回候选消息。"""

    # 长词优先匹配：命中长词后跳过其子片段，避免同一处文字被拆成多个
    # 二三字滑窗重复计分，导致只含常见短词的无关消息得分虚高。
    query_terms = sorted(extract_query_terms(query), key=len, reverse=True)
    normalized_query = normalize_text(query)
    ranked: List[Tuple[MessageRecord, float]] = []
    for message in messages:
        text = normalize_text(message.text)
        if not text:
            continue
        score = 0.0
        if normalized_query and normalized_query in text:
            score += 12.0
        matched_terms: List[str] = []
        for term in query_terms:
            if any(term in longer for longer in matched_terms):
                continue
            occurrences = text.count(term)
            if occurrences:
                matched_terms.append(term)
                score += min(4.0, 1.0 + len(term) * 0.35) * min(3, occurrences)
        if len(matched_terms) > 1:
            # 覆盖多个不同查询条件的消息更可信
            score *= 1.0 + 0.15 * (len(matched_terms) - 1)
        if score > 0:
            ranked.append((message, score))

    if not ranked:
        ranked = [(message, 0.0) for message in messages[-limit:]]
    ranked.sort(key=lambda item: (item[1], item[0].timestamp), reverse=True)
    return ranked[:limit]


async def rerank_with_embeddings(
    query: str,
    candidates: Sequence[Tuple[MessageRecord, float]],
    embed: EmbedFn,
    limit: int,
) -> List[MessageRecord]:
    """调用 MaiBot 嵌入任务重排；结果不完整时抛错，由调用方决定降级。"""

    if not candidates:
        return []
    texts = [query] + [message.text[:2000] for message, _score in candidates]
    result = await embed(texts)
    vectors = _extract_vectors(result)
    if len(vectors) != len(texts):
        raise ValueError("嵌入服务返回的向量数量与输入不一致")
    query_vector = vectors[0]
    lexical_max = max((score for _message, score in candidates), default=1.0) or 1.0
    combined: List[Tuple[MessageRecord, float]] = []
    for (message, lexical_score), vector in zip(candidates, vectors[1:]):
        semantic_score = cosine_similarity(query_vector, vector)
        normalized_lexical = lexical_score / lexical_max
        combined.append((message, semantic_score * 0.72 + normalized_lexical * 0.28))
    combined.sort(key=lambda item: (item[1], item[0].timestamp), reverse=True)
    return [message for message, _score in combined[:limit]]


def expand_context(
    all_messages: Sequence[MessageRecord],
    selected: Sequence[MessageRecord],
    radius: int,
    limit: int,
) -> List[MessageRecord]:
    """为高相关消息补齐前后对话，同时保持时间顺序和上限。"""

    if not selected:
        return []
    index_by_key = {
        (message.message_id or f"{message.timestamp}:{index}"): index
        for index, message in enumerate(all_messages)
    }
    selected_indices: List[int] = []
    for selected_message in selected:
        if selected_message.message_id and selected_message.message_id in index_by_key:
            selected_indices.append(index_by_key[selected_message.message_id])
            continue
        for index, message in enumerate(all_messages):
            if message is selected_message:
                selected_indices.append(index)
                break
    # selected 按相关度从高到低排列，保序去重后超限时优先保留最相关的命中。
    seed_indices = list(dict.fromkeys(selected_indices))
    expanded_indices = set()
    for index in seed_indices:
        expanded_indices.update(range(max(0, index - radius), min(len(all_messages), index + radius + 1)))
    if len(expanded_indices) > limit:
        kept_seeds = seed_indices[:limit]
        seed_set = set(kept_seeds)
        # 裁剪时按“离最近命中消息的距离”保留上下文，而不是保留时间最早的，
        # 否则证据会偏离真正命中的位置。
        context_indices = sorted(
            (index for index in expanded_indices if index not in seed_set),
            key=lambda index: (min(abs(index - seed) for seed in seed_set), index),
        )
        remaining = max(0, limit - len(kept_seeds))
        expanded_indices = seed_set | set(context_indices[:remaining])
    return [all_messages[index] for index in sorted(expanded_indices)]


def extract_query_terms(query: str) -> List[str]:
    """提取英文技术词、数字和中文二至四字片段。"""

    normalized = normalize_text(query)
    focused_query = normalized
    for phrase in _QUERY_FILLER_PHRASES:
        focused_query = focused_query.replace(phrase, " ")
    output: List[str] = []
    for token in re.findall(r"[a-z0-9_+.-]{2,}", focused_query):
        _append_unique(output, token)
    for chinese_run in re.findall(r"[\u4e00-\u9fff]+", focused_query):
        for segment in re.split(f"[{_CHINESE_SPLIT_CHARS}]", chinese_run):
            if len(segment) < 2:
                continue
            if len(segment) <= 8:
                _append_unique(output, segment)
            for width in (2, 3, 4):
                for index in range(0, max(0, len(segment) - width + 1)):
                    _append_unique(output, segment[index : index + width])
    return [term for term in output if term not in _QUERY_STOP_TERMS][:80]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _extract_vectors(result: Dict[str, Any]) -> List[List[float]]:
    if not result.get("success", True):
        raise ValueError(str(result.get("error") or "嵌入服务调用失败"))
    raw_results = result.get("results")
    if not isinstance(raw_results, list):
        raw_embedding = result.get("embedding")
        raw_results = [{"embedding": raw_embedding}] if isinstance(raw_embedding, list) else []
    vectors: List[List[float]] = []
    for item in raw_results:
        raw_vector = item.get("embedding") if isinstance(item, dict) else item
        if not isinstance(raw_vector, list):
            raise ValueError("嵌入服务返回了无效向量")
        try:
            vectors.append([float(value) for value in raw_vector])
        except (TypeError, ValueError) as exc:
            raise ValueError("嵌入向量包含非数字内容") from exc
    return vectors


def _append_unique(output: List[str], value: str) -> None:
    if value and value not in output:
        output.append(value)
