"""插件专属 SQLite 状态存储。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterator, List

import asyncio
import hashlib
import json
import sqlite3
import time

from .models import CompositeRule, PendingRule


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

    @staticmethod
    def _rule_from_row(row: sqlite3.Row) -> CompositeRule:
        payload: Dict[str, Any] = json.loads(row["payload_json"])
        payload["enabled"] = bool(row["enabled"])
        payload["last_triggered_at"] = float(row["last_triggered_at"])
        return CompositeRule.from_dict(payload)
