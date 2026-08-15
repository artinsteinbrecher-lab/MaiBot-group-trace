"""插件内部使用的强类型业务模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass(slots=True)
class CompositeRule:
    """一条可以持久化的复合关键词观察规则。"""

    id: str
    name: str
    owner_user_id: str
    group_ids: List[str]
    required_terms: List[str] = field(default_factory=list)
    any_terms: List[str] = field(default_factory=list)
    excluded_terms: List[str] = field(default_factory=list)
    regex_patterns: List[str] = field(default_factory=list)
    semantic_description: str = ""
    window_seconds: int = 600
    min_occurrences: int = 1
    cooldown_seconds: int = 1800
    enabled: bool = True
    created_at: float = 0.0
    last_triggered_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转换为适合 JSON/SQLite 保存的字典。"""

        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompositeRule":
        """从不可信的持久化字典构造规则。"""

        return cls(
            id=str(data.get("id") or "").strip(),
            name=str(data.get("name") or "未命名规则").strip() or "未命名规则",
            owner_user_id=str(data.get("owner_user_id") or "").strip(),
            group_ids=_string_list(data.get("group_ids")),
            required_terms=_string_list(data.get("required_terms")),
            any_terms=_string_list(data.get("any_terms")),
            excluded_terms=_string_list(data.get("excluded_terms")),
            regex_patterns=_string_list(data.get("regex_patterns")),
            semantic_description=str(data.get("semantic_description") or "").strip(),
            window_seconds=_bounded_int(data.get("window_seconds"), 600, 10, 86400),
            min_occurrences=_bounded_int(data.get("min_occurrences"), 1, 1, 100),
            cooldown_seconds=_bounded_int(data.get("cooldown_seconds"), 1800, 0, 604800),
            enabled=bool(data.get("enabled", True)),
            created_at=_safe_float(data.get("created_at")),
            last_triggered_at=_safe_float(data.get("last_triggered_at")),
        )


@dataclass(slots=True)
class PendingRule:
    """等待用户确认的规则草稿。"""

    user_id: str
    source_stream_id: str
    rule: CompositeRule
    expires_at: float


@dataclass(slots=True)
class MessageRecord:
    """从 MaiBot SessionMessage 归一化后的文本消息。"""

    message_id: str
    stream_id: str
    group_id: str
    group_name: str
    user_id: str
    user_name: str
    text: str
    timestamp: float

    def evidence_line(self) -> str:
        """生成人和模型都容易读取的单行证据。"""

        from datetime import datetime

        time_text = datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return f"[{time_text}] {self.user_name}({self.user_id}): {self.text}"


@dataclass(slots=True)
class MatchDecision:
    """本地规则引擎的匹配结果。"""

    matched: bool
    matched_terms: List[str] = field(default_factory=list)
    excluded_term: str = ""
    reason: str = ""


@dataclass(slots=True)
class SearchResult:
    """外部资料查询的统一结果。"""

    title: str
    url: str
    snippet: str = ""
    published_at: str = ""
    source: str = ""


@dataclass(slots=True)
class QueryPlan:
    """模型对一次群聊寻迹需求的结构化理解。"""

    search_query: str
    keywords: List[str] = field(default_factory=list)
    excluded_terms: List[str] = field(default_factory=list)
    history_days: int = 30


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    output: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
