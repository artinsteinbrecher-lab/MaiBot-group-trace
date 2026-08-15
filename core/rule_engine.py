"""无模型依赖的复合关键词规则引擎。"""

from __future__ import annotations

from collections import defaultdict, deque
from re import IGNORECASE, Pattern, compile as compile_pattern, error as RegexError
from time import time
from typing import Deque, Dict, Iterable, List, Sequence, Tuple

import unicodedata

from .models import CompositeRule, MatchDecision, MessageRecord


class RuleValidationError(ValueError):
    """规则无法安全执行时抛出。"""


class CompositeRuleEngine:
    """支持 AND/OR/NOT、正则和跨消息时间窗的本地规则引擎。"""

    def __init__(self, max_messages_per_group: int = 200) -> None:
        self._buffers: Dict[str, Deque[MessageRecord]] = defaultdict(
            lambda: deque(maxlen=max_messages_per_group)
        )
        self._regex_cache: Dict[Tuple[str, ...], List[Pattern[str]]] = {}

    def push(self, message: MessageRecord) -> None:
        """把新消息加入对应群聊的有界内存窗口。"""

        self._buffers[message.group_id].append(message)

    def recent_for_rule(self, rule: CompositeRule, now: float | None = None) -> List[MessageRecord]:
        """返回规则时间窗内的同群消息。"""

        current_time = time() if now is None else now
        oldest = current_time - max(10, rule.window_seconds)
        records: List[MessageRecord] = []
        for group_id in rule.group_ids:
            records.extend(item for item in self._buffers.get(group_id, ()) if item.timestamp >= oldest)
        return sorted(records, key=lambda item: item.timestamp)

    def evaluate(self, rule: CompositeRule, messages: Sequence[MessageRecord]) -> MatchDecision:
        """针对一组连续消息判断是否满足复合规则。"""

        if not rule.enabled:
            return MatchDecision(False, reason="规则已暂停")
        if not messages:
            return MatchDecision(False, reason="没有可检查的消息")
        if rule.group_ids and not any(message.group_id in rule.group_ids for message in messages):
            return MatchDecision(False, reason="消息不属于规则指定群聊")

        texts = [normalize_text(message.text) for message in messages if message.text.strip()]
        combined = "\n".join(texts)
        if not combined:
            return MatchDecision(False, reason="消息没有文本内容")

        for term in rule.excluded_terms:
            normalized = normalize_text(term)
            if normalized and normalized in combined:
                return MatchDecision(False, excluded_term=term, reason=f"命中排除词：{term}")

        matched_terms: List[str] = []
        missing_required: List[str] = []
        for term in rule.required_terms:
            normalized = normalize_text(term)
            if normalized and normalized in combined:
                matched_terms.append(term)
            else:
                missing_required.append(term)
        if missing_required:
            return MatchDecision(False, matched_terms=matched_terms, reason="缺少必须词：" + "、".join(missing_required))

        if rule.any_terms:
            any_matches = [term for term in rule.any_terms if normalize_text(term) in combined]
            if not any_matches:
                return MatchDecision(False, matched_terms=matched_terms, reason="没有命中任意词")
            matched_terms.extend(any_matches)

        regex_matches: List[str] = []
        for raw_pattern, pattern in zip(rule.regex_patterns, self._compiled_patterns(rule.regex_patterns)):
            if pattern.search(combined):
                regex_matches.append(raw_pattern)
        if rule.regex_patterns and not regex_matches:
            return MatchDecision(False, matched_terms=matched_terms, reason="没有命中正则条件")
        matched_terms.extend(regex_matches)

        has_positive_condition = bool(
            rule.required_terms or rule.any_terms or rule.regex_patterns or rule.semantic_description
        )
        if not has_positive_condition:
            return MatchDecision(False, reason="规则没有任何正向条件")

        return MatchDecision(True, matched_terms=_unique(matched_terms), reason="本地复合条件已满足")

    def message_has_signal(self, rule: CompositeRule, message: MessageRecord) -> bool:
        """判断当前新消息是否贡献了正向条件，避免旧窗口反复触发。"""

        text = normalize_text(message.text)
        if any(normalize_text(term) in text for term in rule.required_terms if normalize_text(term)):
            return True
        if any(normalize_text(term) in text for term in rule.any_terms if normalize_text(term)):
            return True
        return any(pattern.search(text) for pattern in self._compiled_patterns(rule.regex_patterns))

    def validate(self, rule: CompositeRule) -> None:
        """在保存规则前检查群聊、条件和正则表达式。"""

        if not rule.group_ids or any(not group_id.isdigit() for group_id in rule.group_ids):
            raise RuleValidationError("监控群必须是一个或多个纯数字群号")
        if not (rule.required_terms or rule.any_terms or rule.regex_patterns):
            raise RuleValidationError("实时观察规则至少需要一个关键词或正则，不能让模型检查每一条群消息")
        if len(rule.required_terms) + len(rule.any_terms) + len(rule.excluded_terms) > 100:
            raise RuleValidationError("单条规则的关键词总数不能超过 100")
        if len(rule.regex_patterns) > 20:
            raise RuleValidationError("单条规则的正则表达式不能超过 20 条")
        self._compiled_patterns(rule.regex_patterns)

    def _compiled_patterns(self, patterns: Iterable[str]) -> List[Pattern[str]]:
        key = tuple(patterns)
        cached = self._regex_cache.get(key)
        if cached is not None:
            return cached
        output: List[Pattern[str]] = []
        for raw_pattern in key:
            if len(raw_pattern) > 300:
                raise RuleValidationError("单条正则表达式不能超过 300 个字符")
            try:
                output.append(compile_pattern(raw_pattern, IGNORECASE))
            except RegexError as exc:
                raise RuleValidationError(f"正则表达式无效：{raw_pattern}（{exc}）") from exc
        self._regex_cache[key] = output
        return output


def normalize_text(text: str) -> str:
    """统一全半角和英文大小写，保留中文语义。"""

    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _unique(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output
