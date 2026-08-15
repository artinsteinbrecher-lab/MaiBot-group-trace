"""自然语言监控规则和查询意图解析。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Dict, List

import json
import re
import time
import uuid

from .models import CompositeRule, QueryPlan

GenerateFn = Callable[[str], Awaitable[Dict[str, Any]]]


class IntentParseError(ValueError):
    """模型没有返回可验证的结构化意图。"""


class RuleIntentParser:
    """要求模型只生成草稿，保存动作由用户确认命令完成。"""

    def __init__(self, generate: GenerateFn) -> None:
        self._generate = generate

    async def parse_rule(self, description: str, group_id: str, owner_user_id: str) -> CompositeRule:
        prompt = build_rule_prompt(description, group_id)
        result = await self._generate(prompt)
        if not result.get("success"):
            raise IntentParseError(str(result.get("error") or "需求理解模型调用失败"))
        payload = extract_json_object(str(result.get("response") or ""))
        rule = CompositeRule.from_dict(
            {
                **payload,
                "id": uuid.uuid4().hex[:10],
                "owner_user_id": owner_user_id,
                "group_ids": [group_id],
                "created_at": time.time(),
                "enabled": True,
                "last_triggered_at": 0.0,
            }
        )
        if not (rule.required_terms or rule.any_terms or rule.regex_patterns):
            raise IntentParseError("模型没有提取出可执行的本地关键词，请把需要观察的词说得更具体")
        return rule

    async def parse_query(self, description: str, default_history_days: int) -> QueryPlan:
        """理解寻迹片段，提取用于本地和语义检索的线索。"""

        result = await self._generate(build_query_prompt(description, default_history_days))
        if not result.get("success"):
            raise IntentParseError(str(result.get("error") or "需求理解模型调用失败"))
        payload = extract_json_object(str(result.get("response") or ""))
        search_query = str(payload.get("search_query") or description).strip()[:2000]
        keywords = _string_list(payload.get("keywords"))[:30]
        excluded_terms = _string_list(payload.get("excluded_terms"))[:30]
        try:
            history_days = int(payload.get("history_days", default_history_days))
        except (TypeError, ValueError):
            history_days = default_history_days
        return QueryPlan(
            search_query=search_query or description.strip()[:2000],
            keywords=keywords,
            excluded_terms=excluded_terms,
            history_days=max(1, min(365, history_days)),
        )


def build_rule_prompt(description: str, group_id: str) -> str:
    """构建严格 JSON 输出的规则解析提示词。"""

    safe_description = description.strip()[:4000]
    return f"""你正在把用户需求转换为群聊观察规则草稿，不执行规则，也不补充用户没有表达的事实。
目标群号：{group_id}
用户需求：{safe_description}

只输出一个 JSON 对象，不要输出 Markdown。字段必须完整：
{{
  "name": "20字以内规则名",
  "required_terms": ["必须全部出现的词"],
  "any_terms": ["至少出现一个的词或常见别名"],
  "excluded_terms": ["出现后排除的词"],
  "regex_patterns": [],
  "semantic_description": "用一句话准确描述真正需要关注的讨论",
  "window_seconds": 600,
  "min_occurrences": 1,
  "cooldown_seconds": 1800
}}

规则：
1. 用户明确说“并且/同时/必须”时才放 required_terms。
2. 同义词、缩写和“或者”关系放 any_terms。
3. 用户没有要求正则时 regex_patterns 必须为空。
4. window_seconds 取 10 到 86400；min_occurrences 取 1 到 100；cooldown_seconds 取 0 到 604800。
5. 不得把群号、QQ号、时间和通知方式放入关键词。
"""


def build_query_prompt(description: str, default_history_days: int) -> str:
    """构建群聊寻迹需求理解提示词。"""

    return f"""请把用户提供的群聊线索整理为检索计划，不回答问题，不编造关键词。
用户线索：{description.strip()[:4000]}

只输出 JSON：
{{
  "search_query": "保留原意的简洁检索问题",
  "keywords": ["原文词、实体、技术名、常见别名"],
  "excluded_terms": ["用户明确要求排除的内容"],
  "history_days": {default_history_days}
}}

history_days 只能根据用户明确说出的时间修改，范围 1 到 365；没有时间要求时使用 {default_history_days}。
关键词应有区分度，不要加入“这个、相关、信息、讨论”等泛词。
"""


def extract_json_object(text: str) -> Dict[str, Any]:
    """从模型回复中提取首个完整 JSON 对象。"""

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise IntentParseError("模型没有返回 JSON 规则草稿")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise IntentParseError(f"模型返回的规则 JSON 无法解析：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise IntentParseError("模型返回的规则必须是 JSON 对象")
    return value


def format_rule_draft(rule: CompositeRule) -> str:
    """把规则草稿转换为适合用户确认的通俗文本。"""

    def show(values: List[str]) -> str:
        return "、".join(values) if values else "（无）"

    return "\n".join(
        [
            "我理解成下面这条观察规则：",
            f"名称：{rule.name}",
            f"群号：{'、'.join(rule.group_ids)}",
            f"必须同时出现：{show(rule.required_terms)}",
            f"出现任意一个：{show(rule.any_terms)}",
            f"排除：{show(rule.excluded_terms)}",
            f"语义主题：{rule.semantic_description or '（无）'}",
            f"观察窗口：{rule.window_seconds} 秒",
            f"触发次数：{rule.min_occurrences} 次",
            f"提醒冷却：{rule.cooldown_seconds} 秒",
            "\n确认无误请发送 /监控确认；不保存请发送 /监控取消。",
        ]
    )


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    output: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in output:
            output.append(text)
    return output
