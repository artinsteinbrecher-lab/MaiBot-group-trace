"""麦麦群聊寻迹插件入口。

本模块只使用 maibot-plugin-sdk 暴露的正式能力，不直接导入 MaiBot ``src.*``。
实时观察使用非阻塞 ON_MESSAGE 事件；所有长期数据写入 ``ctx.paths.data_dir``。
"""

from __future__ import annotations

from asyncio import Semaphore
from time import time
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple, cast

from maibot_sdk import Command, EventHandler, MaiBotPlugin
from maibot_sdk.types import EventType

from .config_models import GroupTraceConfig
from .core.intent_parser import IntentParseError, RuleIntentParser, extract_json_object, format_rule_draft
from .core.message_utils import (
    extract_command_identity,
    extract_payload_text,
    normalize_message,
    normalize_messages,
    resolve_stream_id,
)
from .core.models import CompositeRule, MessageRecord, PendingRule, QueryPlan, SearchResult
from .core.reporting import (
    attach_evidence_footer,
    build_answer_prompt,
    build_semantic_verify_prompt,
    fallback_history_answer,
    format_monitor_notification,
)
from .core.retrieval import expand_context, rank_lexically, rerank_with_embeddings
from .core.rule_engine import CompositeRuleEngine, RuleValidationError, normalize_text
from .core.search import JsonSearchClient, SearchError, SearchSettings
from .core.storage import StateStore


class GroupTracePlugin(MaiBotPlugin):
    """指定群历史寻迹与复合关键词观察。"""

    config_model = GroupTraceConfig

    async def on_load(self) -> None:
        self._ready = False
        self._rules: List[CompositeRule] = []
        self._admin_user_ids: Set[str] = set()
        self._allowed_group_ids: Set[str] = set()
        self._notification_user_ids: Set[str] = set()
        self._buffer_limit = 0
        self._engine = CompositeRuleEngine()
        self._model_semaphore = Semaphore(2)
        self._search_client: JsonSearchClient | None = None
        self._store = StateStore(self.ctx.paths.data_dir / "group_trace.sqlite3")
        await self._store.initialize()
        await self._refresh_runtime_state(rebuild_engine=True)
        self._ready = True
        self.ctx.logger.info(
            "麦麦群聊寻迹已加载：%d 条规则，%d 个允许群聊",
            len(self._rules),
            len(self._allowed_group_ids),
        )

    async def on_unload(self) -> None:
        self._ready = False
        self._rules = []
        self.ctx.logger.info("麦麦群聊寻迹已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope != "self" or not hasattr(self, "_store"):
            return
        await self._refresh_runtime_state(rebuild_engine=False)
        self.ctx.logger.info("麦麦群聊寻迹配置已热更新：version=%s", version)

    @EventHandler(
        "group_trace_observer",
        description="旁路观察允许群聊中的复合关键词，不阻塞正常消息链",
        event_type=EventType.ON_MESSAGE,
        intercept_message=False,
        weight=-20,
    )
    async def observe_group_message(self, message: Any = None, **kwargs: Any) -> None:
        del kwargs
        if not self._ready or not self._config().plugin.enabled or not self._config().monitoring.enabled:
            return
        if isinstance(message, Mapping) and bool(message.get("is_command")):
            return
        record = normalize_message(message)
        if record is None or not self._is_group_allowed(record.group_id):
            return
        self._engine.push(record)

        for rule in tuple(self._rules):
            if not rule.enabled or record.group_id not in rule.group_ids:
                continue
            if not self._engine.message_has_signal(rule, record):
                continue
            context = self._engine.recent_for_rule(rule, now=record.timestamp)
            decision = self._engine.evaluate(rule, context)
            if not decision.matched:
                continue
            if self._config().monitoring.semantic_verify_enabled and rule.semantic_description:
                if not await self._semantic_verify(rule, context):
                    continue
            should_trigger = await self._store.record_candidate_and_should_trigger(
                rule,
                message_id=record.message_id,
                group_id=record.group_id,
                matched_text=record.text,
                occurred_at=record.timestamp,
            )
            if not should_trigger:
                continue
            evidence_limit = self._config().monitoring.evidence_messages
            evidence = context[-evidence_limit:]
            search_results: List[SearchResult] = []
            if self._config().monitoring.search_on_trigger:
                search_results = await self._search_external(rule.semantic_description or rule.name)
            notification = format_monitor_notification(
                rule,
                record.group_name,
                decision.matched_terms,
                evidence,
                search_results,
            )
            await self._notify_users(rule.owner_user_id, notification)

    @Command(
        "group_trace_search",
        description="根据文字或回复片段检索指定 QQ 群的历史消息",
        pattern=r"^/寻迹\s+(?P<group_id>\d+)(?:\s+(?P<query>[\s\S]+))?$",
    )
    async def handle_search(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        group_id = str(groups.get("group_id") or "").strip()
        if not self._is_group_allowed(group_id):
            return await self._reply(stream_id, "这个群不在插件允许访问的群聊名单中。", success=False)
        query = str(groups.get("query") or "").strip()
        if not query:
            query = await self._query_from_reply(kwargs)
        if not query:
            return await self._reply(
                stream_id,
                "请在群号后写出要找的线索，或者回复一条文字消息后发送 /寻迹 群号。",
                success=False,
            )

        await self.ctx.send.text("我开始查看这个群现有的历史消息，请稍等一下。", stream_id)
        answer = await self._search_group_history(group_id, query)
        return await self._reply(stream_id, answer, success=True)

    @Command(
        "group_trace_create_rule",
        description="用自然语言创建一条等待确认的复合关键词观察规则",
        pattern=r"^/监控创建\s+(?P<group_id>\d+)\s+(?P<description>[\s\S]+)$",
    )
    async def handle_create_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        group_id = str(groups.get("group_id") or "").strip()
        description = str(groups.get("description") or "").strip()
        if not self._is_group_allowed(group_id):
            return await self._reply(stream_id, "这个群不在插件允许访问的群聊名单中。", success=False)
        try:
            parser = RuleIntentParser(self._intent_generate)
            rule = await parser.parse_rule(description, group_id, user_id)
            self._engine.validate(rule)
        except (IntentParseError, RuleValidationError) as exc:
            return await self._reply(stream_id, f"我没能生成安全可执行的规则：{exc}", success=False)
        pending = PendingRule(
            user_id=user_id,
            source_stream_id=stream_id,
            rule=rule,
            expires_at=time() + 900,
        )
        await self._store.save_pending(pending)
        return await self._reply(stream_id, format_rule_draft(rule), success=True)

    @Command("group_trace_confirm_rule", description="确认最近生成的观察规则", pattern=r"^/监控确认\s*$")
    async def handle_confirm_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        pending = await self._store.pop_pending(user_id)
        if pending is None:
            return await self._reply(stream_id, "没有等待确认的规则，或者草稿已经超过15分钟。", success=False)
        try:
            self._engine.validate(pending.rule)
        except RuleValidationError as exc:
            return await self._reply(stream_id, f"规则没有保存：{exc}", success=False)
        if any(not self._is_group_allowed(group_id) for group_id in pending.rule.group_ids):
            return await self._reply(stream_id, "规则目标群已经不在允许名单中，因此没有保存。", success=False)
        await self._store.save_rule(pending.rule)
        await self._refresh_rules()
        return await self._reply(
            stream_id,
            f"规则已启用：{pending.rule.name}\n规则编号：{pending.rule.id}",
            success=True,
        )

    @Command("group_trace_cancel_rule", description="取消最近生成的观察规则", pattern=r"^/监控取消\s*$")
    async def handle_cancel_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        pending = await self._store.pop_pending(user_id)
        text = "已取消规则草稿。" if pending else "当前没有等待确认的规则草稿。"
        return await self._reply(stream_id, text, success=True)

    @Command("group_trace_list_rules", description="列出自己创建的观察规则", pattern=r"^/监控列表\s*$")
    async def handle_list_rules(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        rules = await self._store.list_rules(owner_user_id=user_id)
        if not rules:
            return await self._reply(stream_id, "你还没有保存任何观察规则。", success=True)
        lines = ["你的观察规则："]
        for rule in rules:
            state = "运行中" if rule.enabled else "已暂停"
            lines.append(f"- {rule.id}｜{rule.name}｜群 {'、'.join(rule.group_ids)}｜{state}")
        return await self._reply(stream_id, "\n".join(lines), success=True)

    @Command(
        "group_trace_pause_rule",
        description="暂停自己创建的观察规则",
        pattern=r"^/监控暂停\s+(?P<rule_id>[a-fA-F0-9]{6,32})\s*$",
    )
    async def handle_pause_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        return await self._set_rule_state(kwargs, enabled=False)

    @Command(
        "group_trace_resume_rule",
        description="恢复自己创建的观察规则",
        pattern=r"^/监控恢复\s+(?P<rule_id>[a-fA-F0-9]{6,32})\s*$",
    )
    async def handle_resume_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        return await self._set_rule_state(kwargs, enabled=True)

    @Command(
        "group_trace_delete_rule",
        description="删除自己创建的观察规则",
        pattern=r"^/监控删除\s+(?P<rule_id>[a-fA-F0-9]{6,32})\s*$",
    )
    async def handle_delete_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        rule_id = str(groups.get("rule_id") or "").lower()
        deleted = await self._store.delete_rule(rule_id, owner_user_id=user_id)
        if deleted:
            await self._refresh_rules()
        text = f"已删除规则 {rule_id}。" if deleted else "没有找到属于你的这条规则。"
        return await self._reply(stream_id, text, success=deleted)

    @Command("group_trace_groups", description="查看当前允许访问且 MaiBot 已知的群聊", pattern=r"^/寻迹群列表\s*$")
    async def handle_group_list(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        streams = await self.ctx.chat.get_group_streams(platform="qq")
        lines = ["当前可用于寻迹的群聊："]
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, Mapping):
                    continue
                group_id = str(stream.get("group_id") or "").strip()
                if not self._is_group_allowed(group_id):
                    continue
                group_name = str(stream.get("group_name") or f"群聊{group_id}").strip()
                lines.append(f"- {group_name}（{group_id}）")
        if len(lines) == 1:
            lines.append("暂时没有同时满足白名单和已建立聊天流条件的群。")
        return await self._reply(stream_id, "\n".join(lines), success=True)

    @Command("group_trace_help", description="查看麦麦群聊寻迹的简明帮助", pattern=r"^/寻迹帮助\s*$")
    async def handle_help(self, **kwargs: Any) -> Tuple[bool, str, int]:
        _user_id, stream_id = extract_command_identity(kwargs)
        text = (
            "麦麦群聊寻迹\n"
            "/寻迹 群号 线索 —— 查找指定群的历史消息\n"
            "也可以回复一条文字消息后发送 /寻迹 群号\n"
            "/监控创建 群号 需求 —— 生成复合关键词规则草稿\n"
            "/监控确认 或 /监控取消\n"
            "/监控列表\n"
            "/监控暂停 规则编号\n"
            "/监控恢复 规则编号\n"
            "/监控删除 规则编号\n"
            "/寻迹群列表"
        )
        return await self._reply(stream_id, text, success=True)

    async def _search_group_history(self, group_id: str, original_query: str) -> str:
        config = self._config()
        plan_note = ""
        try:
            parser = RuleIntentParser(self._intent_generate)
            plan = await parser.parse_query(original_query, config.retrieval.history_days)
        except IntentParseError as exc:
            self.ctx.logger.warning("寻迹需求理解失败，使用用户原文检索：%s", exc)
            plan = QueryPlan(search_query=original_query, history_days=config.retrieval.history_days)
            plan_note = "\n提示：需求理解模型暂时不可用，本次按原文完成检索。"

        stream = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform="qq")
        group_stream_id = resolve_stream_id(stream)
        if not group_stream_id:
            return "MaiBot 还没有建立这个群的聊天流，因此暂时无法读取该群历史消息。"
        end_time = time()
        start_time = end_time - plan.history_days * 86400
        raw_messages = await self.ctx.message.get_by_time_in_chat(
            group_stream_id,
            str(start_time),
            str(end_time),
            limit=config.retrieval.max_history_messages,
            limit_mode="latest",
            filter_mai=True,
            filter_command=True,
        )
        messages = normalize_messages(raw_messages)
        if plan.excluded_terms:
            messages = [
                message
                for message in messages
                if not any(normalize_text(term) in normalize_text(message.text) for term in plan.excluded_terms)
            ]
        if not messages:
            return f"这个群最近 {plan.history_days} 天没有可供查询的文本消息。"

        retrieval_query = " ".join([plan.search_query, *plan.keywords]).strip()
        candidates = rank_lexically(retrieval_query, messages, config.retrieval.lexical_candidates)
        seed_limit = max(
            3,
            config.retrieval.evidence_messages // max(1, config.retrieval.context_radius * 2 + 1),
        )
        selected: List[MessageRecord] = []
        embedding_succeeded = False
        if config.retrieval.use_embeddings:
            try:
                selected = await rerank_with_embeddings(
                    retrieval_query,
                    candidates,
                    self._embed_texts,
                    seed_limit,
                )
                embedding_succeeded = True
            except Exception as exc:
                self.ctx.logger.warning("嵌入语义重排失败，保留本地关键词结果：%s", exc)
        if not embedding_succeeded:
            selected = [message for message, score in candidates if score > 0][:seed_limit]
        evidence = expand_context(
            messages,
            selected,
            config.retrieval.context_radius,
            config.retrieval.evidence_messages,
        )
        group_name = evidence[0].group_name if evidence else f"群聊{group_id}"
        search_results = await self._search_external(plan.search_query)
        if not evidence and not search_results:
            return f"没有在“{group_name}”最近 {plan.history_days} 天的记录中找到足够相关的证据。{plan_note}"

        prompt = build_answer_prompt(original_query, group_name, evidence, search_results)
        result = await self._generate(prompt, self._config().models.verify_task, max_tokens=2400)
        if result.get("success") and str(result.get("response") or "").strip():
            answer = attach_evidence_footer(str(result["response"]).strip(), evidence)
        else:
            answer = fallback_history_answer(original_query, group_name, evidence)
        if search_results:
            answer += "\n\n外部资料链接：\n" + "\n".join(
                f"- {item.title}：{item.url}" for item in search_results
            )
        return (answer + plan_note)[:14000]

    async def _semantic_verify(self, rule: CompositeRule, evidence: Sequence[MessageRecord]) -> bool:
        prompt = build_semantic_verify_prompt(rule, evidence)
        result = await self._generate(prompt, self._config().models.verify_task, max_tokens=300)
        if not result.get("success"):
            self.ctx.logger.warning("规则 %s 的语义复核失败，本次不提醒", rule.id)
            return False
        try:
            payload = extract_json_object(str(result.get("response") or ""))
        except IntentParseError as exc:
            self.ctx.logger.warning("规则 %s 的语义复核结果无效，本次不提醒：%s", rule.id, exc)
            return False
        return payload.get("relevant") is True

    async def _search_external(self, query: str) -> List[SearchResult]:
        if self._search_client is None:
            return []
        try:
            return await self._search_client.search(query)
        except SearchError as exc:
            self.ctx.logger.warning("外部资料查询失败，不影响群聊检索：%s", exc)
            return []

    async def _notify_users(self, owner_user_id: str, text: str) -> None:
        recipients = set(self._notification_user_ids)
        if owner_user_id:
            recipients.add(owner_user_id)
        for user_id in sorted(recipients):
            try:
                session = await self.ctx.chat.open_session(
                    platform="qq",
                    chat_type="private",
                    user_id=user_id,
                )
                stream_id = resolve_stream_id(session)
                if not stream_id:
                    raise RuntimeError("没有获得私聊聊天流")
                sent = await self.ctx.send.text(text[:14000], stream_id)
                if not sent:
                    raise RuntimeError("发送能力返回失败")
            except Exception as exc:
                self.ctx.logger.error("向用户 %s 发送观察提醒失败：%s", user_id, exc)

    async def _query_from_reply(self, kwargs: Mapping[str, Any]) -> str:
        message = kwargs.get("message")
        if not isinstance(message, Mapping):
            return ""
        reply_to = str(message.get("reply_to") or "").strip()
        if not reply_to:
            return ""
        current_stream = str(message.get("session_id") or kwargs.get("stream_id") or "").strip()
        replied = await self.ctx.message.get_by_id(reply_to, stream_id=current_stream)
        return extract_payload_text(replied)

    async def _set_rule_state(self, kwargs: Mapping[str, Any], enabled: bool) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        rule_id = str(groups.get("rule_id") or "").lower()
        changed = await self._store.set_rule_enabled(rule_id, enabled, owner_user_id=user_id)
        if changed:
            await self._refresh_rules()
        action = "恢复" if enabled else "暂停"
        text = f"已{action}规则 {rule_id}。" if changed else "没有找到属于你的这条规则。"
        return await self._reply(stream_id, text, success=changed)

    async def _intent_generate(self, prompt: str) -> Dict[str, Any]:
        return await self._generate(prompt, self._config().models.intent_task, max_tokens=1200)

    async def _embed_texts(self, texts: List[str]) -> Dict[str, Any]:
        async with self._model_semaphore:
            return await self.ctx.llm.embed(
                texts=texts,
                task_name=self._config().models.embedding_task,
                max_concurrent=4,
            )

    async def _generate(self, prompt: str, task_name: str, max_tokens: int) -> Dict[str, Any]:
        async with self._model_semaphore:
            return await self.ctx.llm.generate(
                prompt=prompt,
                model=task_name.strip() or "utils",
                temperature=self._config().models.temperature,
                max_tokens=max_tokens,
            )

    async def _refresh_runtime_state(self, *, rebuild_engine: bool) -> None:
        config = self._config()
        self._admin_user_ids = _normalize_numeric_ids(config.access.admin_user_ids)
        self._allowed_group_ids = _normalize_numeric_ids(config.access.allowed_group_ids)
        self._notification_user_ids = _normalize_numeric_ids(config.access.notification_user_ids)
        new_buffer_limit = config.monitoring.max_buffer_messages
        if rebuild_engine or new_buffer_limit != self._buffer_limit:
            self._engine = CompositeRuleEngine(max_messages_per_group=new_buffer_limit)
            self._buffer_limit = new_buffer_limit
        await self._refresh_rules()
        self._search_client = None
        if config.search.enabled and config.search.endpoint.strip():
            self._search_client = JsonSearchClient(
                SearchSettings(
                    endpoint=config.search.endpoint,
                    api_key=config.search.api_key,
                    authorization_header=config.search.authorization_header,
                    authorization_prefix=config.search.authorization_prefix,
                    query_parameter=config.search.query_parameter,
                    results_path=config.search.results_path,
                    title_field=config.search.title_field,
                    url_field=config.search.url_field,
                    snippet_field=config.search.snippet_field,
                    date_field=config.search.date_field,
                    timeout_seconds=config.search.timeout_seconds,
                    max_results=config.search.max_results,
                )
            )
        if not self._admin_user_ids:
            self.ctx.logger.warning("管理员 QQ 名单为空，寻迹和监控管理命令将拒绝执行")
        if not self._allowed_group_ids:
            self.ctx.logger.warning("允许访问的群聊名单为空，插件不会读取或观察任何群")

    async def _refresh_rules(self) -> None:
        rules = await self._store.list_rules()
        valid_rules: List[CompositeRule] = []
        for rule in rules:
            try:
                self._engine.validate(rule)
            except RuleValidationError as exc:
                self.ctx.logger.error("规则 %s 无效，已跳过：%s", rule.id, exc)
                continue
            valid_rules.append(rule)
        self._rules = valid_rules

    def _config(self) -> GroupTraceConfig:
        return cast(GroupTraceConfig, self.config)

    def _command_denied_reason(self, user_id: str) -> str:
        if not self._config().plugin.enabled:
            return "麦麦群聊寻迹当前已在 WebUI 中关闭。"
        if not user_id or user_id not in self._admin_user_ids:
            return "你不在麦麦群聊寻迹的管理员名单中，不能执行这个操作。"
        return ""

    def _is_group_allowed(self, group_id: str) -> bool:
        return bool(group_id and group_id in self._allowed_group_ids)

    async def _reply(self, stream_id: str, text: str, *, success: bool) -> Tuple[bool, str, int]:
        if not stream_id:
            return False, text, 1
        sent = await self.ctx.send.text(text[:14000], stream_id)
        return success and sent, text, 2 if success else 1


def _normalize_numeric_ids(values: Sequence[str]) -> Set[str]:
    return {str(value).strip() for value in values if str(value).strip().isdigit()}


def create_plugin() -> GroupTracePlugin:
    """MaiBot Host 的标准插件工厂入口。"""

    return GroupTracePlugin()
