"""证据回答和监控提醒的提示词与降级文本。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Sequence

import re

from .models import CompositeRule, MessageRecord, SearchResult

# 相邻证据超过这个间隔时插入分段标记，避免把不同时间的对话混为一谈。
EVIDENCE_GAP_SECONDS = 1800

_CITATION_PATTERN = re.compile(r"\[E(\d+)\]")


def render_evidence_lines(
    evidence: Sequence[MessageRecord],
    gap_seconds: int = EVIDENCE_GAP_SECONDS,
) -> List[str]:
    """渲染带编号的证据行，并在时间跳跃处插入分段标记。"""

    lines: List[str] = []
    previous_timestamp: float | None = None
    for index, message in enumerate(evidence):
        if previous_timestamp is not None and message.timestamp - previous_timestamp >= gap_seconds:
            gap_text = _format_gap(message.timestamp - previous_timestamp)
            lines.append(f"（间隔{gap_text}，以下可能是另一段对话）")
        lines.append(f"E{index + 1} {message.evidence_line()}")
        previous_timestamp = message.timestamp
    return lines


def build_answer_prompt(
    query: str,
    group_name: str,
    evidence: Sequence[MessageRecord],
    search_results: Sequence[SearchResult] = (),
) -> str:
    lines = "\n".join(render_evidence_lines(evidence))
    external_lines = "\n".join(
        f"S{index + 1} {result.title} | {result.published_at or '时间未知'} | {result.url} | {result.snippet}"
        for index, result in enumerate(search_results)
    )
    external_section = f"\n外部资料：\n{external_lines}" if external_lines else ""
    return f"""请根据给定的群聊证据回答用户问题。真实性优先。
用户问题：{query[:3000]}
目标群聊：{group_name}

证据（每行开头是证据编号和发言时间）：
{lines}
{external_section}

要求：
1. 只能使用证据中明确出现的信息，不得补充常识猜测。
2. 区分群友说法、外部资料和仍有争议的内容；外部资料不能倒推出群里说过相同内容。
3. 每个重要结论末尾标注证据编号，例如 [E2][E5]；并写明事情发生的大致时间（以证据行开头的时间为准）。
4. 注意分段标记：标记两侧的消息属于不同时间的对话，不要当成同一次讨论。
5. 引用外部资料时使用 [S1]；证据不足时直接说明“现有群聊记录不足以确认”。
6. 输出简体中文，先给结论，再分点列关键证据，保持简洁好读。
"""


def build_semantic_verify_prompt(rule: CompositeRule, evidence: Sequence[MessageRecord]) -> str:
    lines = "\n".join(message.evidence_line() for message in evidence[-20:])
    return f"""判断以下群聊片段是否真的在讨论指定主题。
主题：{rule.semantic_description or rule.name}
群聊片段：
{lines}

只输出 JSON：{{"relevant": true, "reason": "一句话理由"}}。
如果只是引用、玩笑、否定、同名无关内容或证据不足，relevant 必须为 false。
"""


def attach_evidence_footer(
    answer: str,
    evidence: Sequence[MessageRecord],
    max_lines: int = 10,
) -> str:
    """把回答中引用的 [En] 编号对应的原始消息附在末尾，让编号可以查证。"""

    if not evidence:
        return answer
    cited: List[int] = []
    for match in _CITATION_PATTERN.finditer(answer):
        index = int(match.group(1))
        if 1 <= index <= len(evidence) and index not in cited:
            cited.append(index)
    indices = sorted(cited)[:max_lines] if cited else list(range(1, min(len(evidence), max_lines) + 1))
    lines = [answer, "", "证据原文："]
    for index in indices:
        lines.append(f"[E{index}] {_display_line(evidence[index - 1])}")
    return "\n".join(lines)


def fallback_history_answer(query: str, group_name: str, evidence: Sequence[MessageRecord]) -> str:
    if not evidence:
        return f"没有在“{group_name}”当前可读取的历史消息中找到与“{query}”相关的证据。"
    lines = [
        f"在“{group_name}”找到 {len(evidence)} 条可能相关的消息。模型整理暂时不可用，先给你原始证据："
    ]
    lines.extend(render_evidence_lines(evidence))
    return "\n".join(lines)


def format_monitor_notification(
    rule: CompositeRule,
    group_name: str,
    matched_terms: Sequence[str],
    evidence: Sequence[MessageRecord],
    search_results: Sequence[SearchResult] = (),
) -> str:
    lines: List[str] = [
        f"检测到“{rule.name}”相关讨论",
        f"群聊：{group_name}",
        f"命中条件：{'、'.join(matched_terms) if matched_terms else '语义主题'}",
        f"规则编号：{rule.id}（可用 /监控暂停 {rule.id} 暂停提醒）",
        "",
        "相关聊天：",
    ]
    lines.extend(render_evidence_lines(evidence))
    if search_results:
        lines.extend(["", "外部资料（仅供核对）："])
        for index, result in enumerate(search_results, 1):
            date_text = f" · {result.published_at}" if result.published_at else ""
            lines.append(f"S{index} {result.title}{date_text}\n{result.url}")
    return "\n".join(lines)


def _display_line(message: MessageRecord) -> str:
    time_text = datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M")
    text = message.text if len(message.text) <= 120 else message.text[:120] + "……"
    return f"{time_text} {message.user_name}：{text}"


def _format_gap(seconds: float) -> str:
    if seconds >= 86400:
        return f"约 {int(seconds // 86400)} 天"
    if seconds >= 3600:
        return f"约 {int(seconds // 3600)} 小时"
    return f"约 {max(1, int(seconds // 60))} 分钟"
