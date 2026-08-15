"""证据回答和监控提醒的提示词与降级文本。"""

from __future__ import annotations

from typing import List, Sequence

from .models import CompositeRule, MessageRecord, SearchResult


def build_answer_prompt(
    query: str,
    group_name: str,
    evidence: Sequence[MessageRecord],
    search_results: Sequence[SearchResult] = (),
) -> str:
    lines = "\n".join(f"E{index + 1} {message.evidence_line()}" for index, message in enumerate(evidence))
    external_lines = "\n".join(
        f"S{index + 1} {result.title} | {result.published_at or '时间未知'} | {result.url} | {result.snippet}"
        for index, result in enumerate(search_results)
    )
    external_section = f"\n外部资料：\n{external_lines}" if external_lines else ""
    return f"""请根据给定的群聊证据回答用户问题。真实性优先。
用户问题：{query[:3000]}
目标群聊：{group_name}

证据：
{lines}
{external_section}

要求：
1. 只能使用证据中明确出现的信息，不得补充常识猜测。
2. 区分群友说法、外部资料和仍有争议的内容；外部资料不能倒推出群里说过相同内容。
3. 每个重要结论末尾标注证据编号，例如 [E2][E5]。
4. 引用外部资料时使用 [S1]；证据不足时直接说明“现有群聊记录不足以确认”。
5. 输出简体中文，先给结论，再列关键证据，保持简洁好读。
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


def fallback_history_answer(query: str, group_name: str, evidence: Sequence[MessageRecord]) -> str:
    if not evidence:
        return f"没有在“{group_name}”当前可读取的历史消息中找到与“{query}”相关的证据。"
    lines = [
        f"在“{group_name}”找到 {len(evidence)} 条可能相关的消息。模型整理暂时不可用，先给你原始证据："
    ]
    lines.extend(f"E{index + 1} {message.evidence_line()}" for index, message in enumerate(evidence))
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
        "",
        "相关聊天：",
    ]
    lines.extend(f"E{index + 1} {message.evidence_line()}" for index, message in enumerate(evidence))
    if search_results:
        lines.extend(["", "外部资料（仅供核对）："])
        for index, result in enumerate(search_results, 1):
            date_text = f" · {result.published_at}" if result.published_at else ""
            lines.append(f"S{index} {result.title}{date_text}\n{result.url}")
    return "\n".join(lines)
