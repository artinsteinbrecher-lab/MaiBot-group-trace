"""MaiBot 消息、命令载荷和群聊流的归一化工具。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping

from .models import MessageRecord


def normalize_message(payload: Any) -> MessageRecord | None:
    """将官方 EventHandler/消息能力返回值转为 ``MessageRecord``。"""

    if not isinstance(payload, Mapping):
        return None
    message_info = payload.get("message_info")
    if not isinstance(message_info, Mapping):
        return None
    group_info = message_info.get("group_info")
    user_info = message_info.get("user_info")
    if not isinstance(group_info, Mapping) or not isinstance(user_info, Mapping):
        return None

    group_id = str(group_info.get("group_id") or "").strip()
    if not group_id:
        return None
    text = extract_payload_text(payload)
    if not text:
        return None

    user_id = str(user_info.get("user_id") or "").strip()
    user_name = str(user_info.get("user_cardname") or user_info.get("user_nickname") or user_id or "未知成员").strip()
    return MessageRecord(
        message_id=str(payload.get("message_id") or "").strip(),
        stream_id=str(payload.get("session_id") or payload.get("stream_id") or "").strip(),
        group_id=group_id,
        group_name=str(group_info.get("group_name") or f"群聊{group_id}").strip(),
        user_id=user_id,
        user_name=user_name,
        text=text,
        timestamp=parse_timestamp(payload.get("timestamp")),
    )


def extract_payload_text(payload: Any) -> str:
    """读取任意群聊/私聊 SessionMessage 的文本，不要求存在群信息。"""

    if not isinstance(payload, Mapping):
        return ""
    text = str(payload.get("processed_plain_text") or "").strip()
    return text or extract_text_from_segments(payload.get("raw_message"))


def normalize_messages(values: Any) -> List[MessageRecord]:
    """批量归一化历史消息，并按时间升序去重。"""

    if isinstance(values, Mapping):
        values = values.get("messages") or values.get("items") or []
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        return []
    messages: Dict[str, MessageRecord] = {}
    for index, value in enumerate(values):
        record = normalize_message(value)
        if record is None:
            continue
        key = record.message_id or f"{record.timestamp}:{record.user_id}:{index}"
        messages[key] = record
    return sorted(messages.values(), key=lambda item: (item.timestamp, item.message_id))


def extract_command_identity(kwargs: Mapping[str, Any]) -> tuple[str, str]:
    """从 Command 的官方参数中提取发起用户与当前聊天流。"""

    message = kwargs.get("message")
    user_id = ""
    if isinstance(message, Mapping):
        message_info = message.get("message_info")
        if isinstance(message_info, Mapping):
            user_info = message_info.get("user_info")
            if isinstance(user_info, Mapping):
                user_id = str(user_info.get("user_id") or "").strip()
    stream_id = str(kwargs.get("stream_id") or "").strip()
    if not stream_id and isinstance(message, Mapping):
        stream_id = str(message.get("session_id") or message.get("stream_id") or "").strip()
    return user_id, stream_id


def resolve_stream_id(stream: Any) -> str:
    """兼容官方聊天流能力的字典或字符串返回形式。"""

    if isinstance(stream, str):
        return stream.strip()
    if not isinstance(stream, Mapping):
        return ""
    nested = stream.get("stream")
    if isinstance(nested, Mapping):
        stream = nested
    return str(stream.get("stream_id") or stream.get("session_id") or stream.get("chat_id") or "").strip()


def extract_text_from_segments(segments: Any) -> str:
    """从 maim-message 序列化消息段中提取可检索文本。"""

    if not isinstance(segments, list):
        return ""
    parts: List[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_type = str(segment.get("type") or "").lower()
        data = segment.get("data")
        if segment_type == "text":
            if isinstance(data, str):
                text = data
            elif isinstance(data, Mapping):
                text = str(data.get("text") or data.get("content") or "")
            else:
                text = ""
            if text.strip():
                parts.append(text.strip())
        elif segment_type == "reply" and isinstance(data, Mapping):
            text = str(data.get("text") or data.get("content") or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def parse_timestamp(value: Any) -> float:
    """兼容 UNIX 时间戳和 ISO 时间字符串。"""

    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return datetime.now().timestamp()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return datetime.now().timestamp()
