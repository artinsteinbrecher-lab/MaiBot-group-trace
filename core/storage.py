"""插件专属 SQLite 状态存储。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterator, List, Sequence, Tuple

import asyncio
import hashlib
import json
import sqlite3
import time

from .models import CompositeRule, MessageRecord, PendingRule
from .rule_engine import normalize_text


class StateStore:
    """使用单个 SQLite 文件保存规则、待确认草稿和命中记录。"""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._lock = RLock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def save_rule(self, rule: CompositeRule) -> None:
        await asyncio.to_thread(self._save_rule_sync, rule)

    async def list_rules(self, owner_user_id: str = "") -> List[CompositeRule]:
        return await asyncio.to_thread(self._list_rules_sync, owner_user_id)

    async def get_rule(self, rule_id: str) -> CompositeRule | None:
        return await asyncio.to_thread(self._get_rule_sync, rule_id)

    async def set_rule_enabled(self, rule_id: str, enabled: bool, owner_user_id: str = "") -> bool:
        return await asyncio.to_thread(self._set_rule_enabled_sync, rule_id, enabled, owner_user_id)

    async def delete_rule(self, rule_id: str, owner_user_id: str = "") -> bool:
        return await asyncio.to_thread(self._delete_rule_sync, rule_id, owner_user_id)

    async def save_pending(self, pending: PendingRule) -> None:
        await asyncio.to_thread(self._save_pending_sync, pending)

    async def pop_pending(self, user_id: str) -> PendingRule | None:
        return await asyncio.to_thread(self._pop_pending_sync, user_id)

    async def record_candidate_and_should_trigger(
        self,
        rule: CompositeRule,
        *,
        message_id: str,
        group_id: str,
        matched_text: str,
        occurred_at: float,
    ) -> bool:
        """原子记录候选命中，并按次数阈值和冷却时间决定是否提醒。"""

        return await asyncio.to_thread(
            self._record_candidate_and_should_trigger_sync,
            rule,
            message_id,
            group_id,
            matched_text,
            occurred_at,
        )

    async def index_messages(self, records: Sequence[MessageRecord]) -> int:
        """把文本消息写入本地关键词索引，重复消息自动忽略。"""

        return await asyncio.to_thread(self._index_messages_sync, list(records))

    async def search_index(
        self,
        group_id: str,
        terms: Sequence[str],
        start_time: float,
        end_time: float,
        limit: int,
    ) -> List[MessageRecord]:
        """按关键词直查本地索引；terms 为空时返回时间范围内最新的消息。"""

        return await asyncio.to_thread(
            self._search_index_sync, group_id, list(terms), start_time, end_time, limit
        )

    async def index_neighbors(self, group_id: str, timestamp: float, radius: int) -> List[MessageRecord]:
        """返回索引中某时间点前后各 radius 条消息（含该时间点本身）。"""

        return await asyncio.to_thread(self._index_neighbors_sync, group_id, timestamp, radius)

    async def index_coverage(self, group_id: str) -> Tuple[float, float, int] | None:
        """返回索引对某群的覆盖情况（最早时间、最晚时间、条数），无记录时为 None。"""

        return await asyncio.to_thread(self._index_coverage_sync, group_id)

    async def prune_index(self, allowed_group_ids: Sequence[str], retention_days: int) -> int:
        """删除超过保留期或已移出白名单群的索引消息。"""

        return await asyncio.to_thread(self._prune_index_sync, list(allowed_group_ids), retention_days)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """提供会提交、回滚并确定关闭的短连接。"""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rules (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    last_triggered_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_rules_owner ON rules(owner_user_id);

                CREATE TABLE IF NOT EXISTS pending_rules (
                    user_id TEXT PRIMARY KEY,
                    source_stream_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    matched_text TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    UNIQUE(rule_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_hits_rule_time ON hits(rule_id, occurred_at);

                CREATE TABLE IF NOT EXISTS message_index (
                    message_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_message_index_group_time
                    ON message_index(group_id, timestamp);
                """
            )

    def _save_rule_sync(self, rule: CompositeRule) -> None:
        payload = json.dumps(rule.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO rules(id, owner_user_id, payload_json, enabled, created_at, last_triggered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,
                    payload_json=excluded.payload_json,
                    enabled=excluded.enabled,
                    created_at=excluded.created_at,
                    last_triggered_at=excluded.last_triggered_at
                """,
                (
                    rule.id,
                    rule.owner_user_id,
                    payload,
                    int(rule.enabled),
                    rule.created_at,
                    rule.last_triggered_at,
                ),
            )

    def _list_rules_sync(self, owner_user_id: str) -> List[CompositeRule]:
        query = "SELECT payload_json, enabled, last_triggered_at FROM rules"
        params: tuple[Any, ...] = ()
        if owner_user_id:
            query += " WHERE owner_user_id = ?"
            params = (owner_user_id,)
        query += " ORDER BY created_at ASC"
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._rule_from_row(row) for row in rows]

    def _get_rule_sync(self, rule_id: str) -> CompositeRule | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json, enabled, last_triggered_at FROM rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        return self._rule_from_row(row) if row else None

    def _set_rule_enabled_sync(self, rule_id: str, enabled: bool, owner_user_id: str) -> bool:
        query = "UPDATE rules SET enabled = ? WHERE id = ?"
        params: tuple[Any, ...] = (int(enabled), rule_id)
        if owner_user_id:
            query += " AND owner_user_id = ?"
            params += (owner_user_id,)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(query, params)
            if cursor.rowcount:
                row = connection.execute("SELECT payload_json FROM rules WHERE id = ?", (rule_id,)).fetchone()
                if row:
                    payload = json.loads(row["payload_json"])
                    payload["enabled"] = enabled
                    connection.execute(
                        "UPDATE rules SET payload_json = ? WHERE id = ?",
                        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), rule_id),
                    )
            return cursor.rowcount > 0

    def _delete_rule_sync(self, rule_id: str, owner_user_id: str) -> bool:
        query = "DELETE FROM rules WHERE id = ?"
        params: tuple[Any, ...] = (rule_id,)
        if owner_user_id:
            query += " AND owner_user_id = ?"
            params += (owner_user_id,)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(query, params)
            if cursor.rowcount:
                connection.execute("DELETE FROM hits WHERE rule_id = ?", (rule_id,))
            return cursor.rowcount > 0

    def _save_pending_sync(self, pending: PendingRule) -> None:
        payload = json.dumps(pending.rule.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO pending_rules(user_id, source_stream_id, payload_json, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    source_stream_id=excluded.source_stream_id,
                    payload_json=excluded.payload_json,
                    expires_at=excluded.expires_at
                """,
                (pending.user_id, pending.source_stream_id, payload, pending.expires_at),
            )

    def _pop_pending_sync(self, user_id: str) -> PendingRule | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT source_stream_id, payload_json, expires_at FROM pending_rules WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            connection.execute("DELETE FROM pending_rules WHERE user_id = ?", (user_id,))
        if row is None or float(row["expires_at"]) < time.time():
            return None
        return PendingRule(
            user_id=user_id,
            source_stream_id=str(row["source_stream_id"]),
            rule=CompositeRule.from_dict(json.loads(row["payload_json"])),
            expires_at=float(row["expires_at"]),
        )

    def _record_candidate_and_should_trigger_sync(
        self,
        rule: CompositeRule,
        message_id: str,
        group_id: str,
        matched_text: str,
        occurred_at: float,
    ) -> bool:
        text_digest = hashlib.sha256(matched_text.encode("utf-8")).hexdigest()[:16]
        stable_message_id = message_id or f"{group_id}:{occurred_at:.6f}:{text_digest}"
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO hits(rule_id, message_id, group_id, matched_text, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (rule.id, stable_message_id, group_id, matched_text[:2000], occurred_at),
            )
            if cursor.rowcount == 0:
                return False
            row = connection.execute(
                "SELECT last_triggered_at FROM rules WHERE id = ?",
                (rule.id,),
            ).fetchone()
            last_triggered = float(row["last_triggered_at"]) if row else rule.last_triggered_at
            if occurred_at - last_triggered < rule.cooldown_seconds:
                return False
            window_start = occurred_at - rule.window_seconds
            count_row = connection.execute(
                "SELECT COUNT(*) AS total FROM hits WHERE rule_id = ? AND occurred_at >= ?",
                (rule.id, window_start),
            ).fetchone()
            total = int(count_row["total"]) if count_row else 0
            if total < rule.min_occurrences:
                return False
            connection.execute(
                "UPDATE rules SET last_triggered_at = ? WHERE id = ?",
                (occurred_at, rule.id),
            )
            payload_row = connection.execute("SELECT payload_json FROM rules WHERE id = ?", (rule.id,)).fetchone()
            if payload_row:
                payload: Dict[str, Any] = json.loads(payload_row["payload_json"])
                payload["last_triggered_at"] = occurred_at
                connection.execute(
                    "UPDATE rules SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), rule.id),
                )
            return True

    def _index_messages_sync(self, records: List[MessageRecord]) -> int:
        rows = []
        for record in records:
            text = record.text.strip()
            if not text:
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            message_id = record.message_id or f"{record.group_id}:{record.timestamp:.6f}:{digest}"
            rows.append(
                (
                    message_id,
                    record.stream_id,
                    record.group_id,
                    record.group_name,
                    record.user_id,
                    record.user_name,
                    text[:4000],
                    normalize_text(text)[:4000],
                    record.timestamp,
                )
            )
        if not rows:
            return 0
        with self._lock, self._connection() as connection:
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO message_index(
                    message_id, stream_id, group_id, group_name,
                    user_id, user_name, text, normalized_text, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return max(0, cursor.rowcount)

    def _search_index_sync(
        self,
        group_id: str,
        terms: List[str],
        start_time: float,
        end_time: float,
        limit: int,
    ) -> List[MessageRecord]:
        query = (
            "SELECT * FROM message_index WHERE group_id = ? AND timestamp >= ? AND timestamp <= ?"
        )
        params: List[Any] = [group_id, start_time, end_time]
        like_clauses = []
        for term in terms[:20]:
            normalized = normalize_text(term)
            if not normalized:
                continue
            like_clauses.append("normalized_text LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(normalized)}%")
        if like_clauses:
            query += " AND (" + " OR ".join(like_clauses) + ")"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def _index_neighbors_sync(self, group_id: str, timestamp: float, radius: int) -> List[MessageRecord]:
        with self._lock, self._connection() as connection:
            before = connection.execute(
                """
                SELECT * FROM message_index WHERE group_id = ? AND timestamp < ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (group_id, timestamp, max(0, radius)),
            ).fetchall()
            after = connection.execute(
                """
                SELECT * FROM message_index WHERE group_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC LIMIT ?
                """,
                (group_id, timestamp, max(0, radius) + 1),
            ).fetchall()
        records = [self._message_from_row(row) for row in reversed(before)]
        records.extend(self._message_from_row(row) for row in after)
        return records

    def _index_coverage_sync(self, group_id: str) -> Tuple[float, float, int] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(timestamp) AS earliest, MAX(timestamp) AS latest, COUNT(*) AS total
                FROM message_index WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
        if row is None or row["total"] in (0, None):
            return None
        return float(row["earliest"]), float(row["latest"]), int(row["total"])

    def _prune_index_sync(self, allowed_group_ids: List[str], retention_days: int) -> int:
        cutoff = time.time() - max(1, retention_days) * 86400
        removed = 0
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM message_index WHERE timestamp < ?", (cutoff,))
            removed += max(0, cursor.rowcount)
            if allowed_group_ids:
                placeholders = ",".join("?" for _ in allowed_group_ids)
                cursor = connection.execute(
                    f"DELETE FROM message_index WHERE group_id NOT IN ({placeholders})",
                    allowed_group_ids,
                )
            else:
                cursor = connection.execute("DELETE FROM message_index")
            removed += max(0, cursor.rowcount)
        return removed

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            message_id=str(row["message_id"]),
            stream_id=str(row["stream_id"]),
            group_id=str(row["group_id"]),
            group_name=str(row["group_name"]),
            user_id=str(row["user_id"]),
            user_name=str(row["user_name"]),
            text=str(row["text"]),
            timestamp=float(row["timestamp"]),
        )

    @staticmethod
    def _rule_from_row(row: sqlite3.Row) -> CompositeRule:
        payload: Dict[str, Any] = json.loads(row["payload_json"])
        payload["enabled"] = bool(row["enabled"])
        payload["last_triggered_at"] = float(row["last_triggered_at"])
        return CompositeRule.from_dict(payload)


def _escape_like(term: str) -> str:
    """转义 LIKE 通配符，保证关键词按字面匹配。"""

    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
